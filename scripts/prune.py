"""Reclaim disk by removing torrents that have finished paying their dues.

The one thing in this repo that deletes a user's media, so the whole design is
about not doing that by accident:

  * It is **off** until `[prune] enabled = true`. Installed and running is not
    the same as armed — the timer can sit there reporting for weeks first.
  * It does nothing while there is space. Pruning is for reclaiming disk, not
    for enforcing a seed policy, so a half-empty disk loses nothing.
  * A candidate must have seeded long enough **and** reached the ratio. Either
    test alone is wrong: a popular release hits 2.0 within the hour, and an
    unpopular one never gets there however long it sits.
  * It stops the moment there is enough room again, oldest-seeded first. The
    goal is a number of free bytes, not an empty session.

    python scripts/prune.py             # report only, whatever the config says
    python scripts/prune.py --apply     # actually remove (needs enabled=true)
    python scripts/prune.py --json

Exit 0 always unless a removal fails — "nothing to do" is the common answer
and is not a problem.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent import deluge  # noqa: E402
from torrent_agent.config import load_config  # noqa: E402
from torrent_agent.deluge import DelugeError, fmt_size  # noqa: E402

GB = 1024 ** 3

# Only Seeding. Not Paused, not Error, not Queued: those states each mean
# something is unresolved, and "unresolved" is the last thing that should be
# resolved by deleting the files.
_PRUNABLE_STATE = "Seeding"

_FIELDS = ["name", "state", "ratio", "seeding_time", "total_wanted", "is_finished"]


class Policy:
    """The rules, resolved from config once so the selector stays pure."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.enabled = bool(cfg.get("enabled", False))
        self.min_free = float(cfg.get("min_free_gb", 200)) * GB
        self.min_seed_seconds = float(cfg.get("min_seed_hours", 72)) * 3600
        self.min_ratio = float(cfg.get("min_ratio", 1.0))
        self.delete_data = bool(cfg.get("delete_data", True))

    @property
    def refusal(self) -> str:
        """Why this policy must not be applied, or "" if it is sane.

        A zero threshold is almost always an unset value rather than an
        intent, and either one here would make every finished torrent a
        candidate the instant it completed.
        """
        if self.min_seed_seconds <= 0:
            return "min_seed_hours is 0 — that would prune torrents the moment they finish"
        if self.min_free <= 0:
            return "min_free_gb is 0 — that target can never be met, so nothing would stop"
        return ""


def _decode(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


def torrent_rows(client) -> list[dict[str, Any]]:
    """Everything prune needs to decide, decoded out of Deluge's bytes."""
    try:
        torrents: dict = client.call("core.get_torrents_status", {}, _FIELDS)
    except Exception as exc:
        raise DelugeError(f"Failed to list torrents: {exc}") from exc

    rows = []
    for tid, info in (torrents or {}).items():
        info = {_decode(k): v for k, v in (info or {}).items()}
        rows.append(
            {
                "id": _decode(tid),
                "name": _decode(info.get("name", "<unknown>")),
                "state": _decode(info.get("state", "?")),
                "ratio": float(info.get("ratio", 0.0) or 0.0),
                "seeding_time": int(info.get("seeding_time", 0) or 0),
                "size": float(info.get("total_wanted", 0) or 0),
                "finished": bool(info.get("is_finished", False)),
            }
        )
    return rows


def select(rows: list[dict[str, Any]], policy: Policy, free_bytes: float) -> tuple[list[dict], str]:
    """Which torrents to remove, and a one-line reason for the answer.

    Returns ([], reason) far more often than not — that is the healthy case.
    """
    if free_bytes >= policy.min_free:
        return [], (
            f"{fmt_size(free_bytes)} free, target {fmt_size(policy.min_free)} — nothing to do"
        )

    need = policy.min_free - free_bytes
    eligible = [
        r for r in rows
        if r["state"] == _PRUNABLE_STATE
        and r["finished"]
        and r["seeding_time"] >= policy.min_seed_seconds
        and r["ratio"] >= policy.min_ratio
    ]
    if not eligible:
        return [], (
            f"{fmt_size(free_bytes)} free, {fmt_size(need)} short — but nothing has "
            f"seeded {policy.min_seed_seconds / 3600:.0f}h at ratio {policy.min_ratio}"
        )

    # Longest-seeded first: that torrent has given the swarm the most and is
    # the least likely to still be wanted. Size is deliberately not the sort
    # key — reclaiming a season in one go is tempting and exactly the greedy
    # behaviour that deletes the thing you were about to watch.
    eligible.sort(key=lambda r: r["seeding_time"], reverse=True)

    chosen: list[dict[str, Any]] = []
    freed = 0.0
    for row in eligible:
        if freed >= need:
            break
        chosen.append(row)
        freed += row["size"]

    short = "" if freed >= need else f" (still {fmt_size(need - freed)} short)"
    return chosen, (
        f"{fmt_size(free_bytes)} free, want {fmt_size(policy.min_free)}: "
        f"removing {len(chosen)} to reclaim {fmt_size(freed)}{short}"
    )


def prune(config: dict[str, Any], apply: bool) -> dict[str, Any]:
    """Run one pass. `apply` is ignored unless the policy is enabled."""
    policy = Policy(config.get("prune", {}))
    refusal = policy.refusal
    if refusal:
        return {"acted": False, "reason": f"refusing: {refusal}", "candidates": [], "removed": []}

    with deluge.connect(config) as client:
        try:
            free = float(client.call("core.get_free_space") or 0)
        except Exception as exc:
            raise DelugeError(f"Deluge could not report free space: {exc}") from exc

        rows = torrent_rows(client)
        chosen, reason = select(rows, policy, free)

        acting = apply and policy.enabled and bool(chosen)
        if not acting:
            if chosen and apply and not policy.enabled:
                reason += " — but [prune] enabled = false, so nothing was removed"
            elif chosen and not apply:
                reason += " — report only, pass --apply to remove"
            return {"acted": False, "reason": reason,
                    "candidates": [_summary(c) for c in chosen], "removed": []}

        removed, failed = [], []
        for row in chosen:
            try:
                client.call("core.remove_torrent", row["id"], policy.delete_data)
                removed.append(_summary(row))
            except Exception as exc:
                failed.append({"name": row["name"], "error": str(exc)})

    return {
        "acted": True,
        "reason": reason,
        "delete_data": policy.delete_data,
        "candidates": [_summary(c) for c in chosen],
        "removed": removed,
        "failed": failed,
    }


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "size": row["size"],
        "ratio": round(row["ratio"], 2),
        "seed_hours": round(row["seeding_time"] / 3600, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="Remove, rather than report. Needs [prune] enabled = true.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    result = prune(config, apply=args.apply)

    if args.json:
        print(json.dumps(result, indent=2))
        return 1 if result.get("failed") else 0

    print(result["reason"])
    shown = result["removed"] or result["candidates"]
    verb = "removed" if result["acted"] else "would remove"
    if shown:
        if result["acted"]:
            print(f"  data {'deleted' if result.get('delete_data') else 'kept on disk'}")
        for row in shown:
            print(f"  {verb}: {row['name']}  "
                  f"({fmt_size(row['size'])}, ratio {row['ratio']}, {row['seed_hours']}h)")
    for bad in result.get("failed", []):
        print(f"  FAILED: {bad['name']}: {bad['error']}", file=sys.stderr)

    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DelugeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
