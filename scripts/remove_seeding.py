"""Remove torrents from Deluge (data is always kept on disk).

Two modes:

  * no arguments — remove every torrent in the **Seeding** state, the
    housekeeping sweep behind the `remove-seeding-torrents` skill.
  * ``--id`` — remove exactly the given torrents, whatever state they are in.
    Used by the `fetch-to-jellyfin` pipeline to clean up the torrent it just
    added, without touching anything else the user is deliberately seeding.
    State is ignored rather than filtered on: the caller named these ids, and
    a torrent that finished can still be Paused, Checking, or in Error (tidying
    moves files out from under Deluge) — a `Seeding` filter would miss those.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent import deluge
from torrent_agent.config import load_config
from torrent_agent.deluge import DelugeError


def _decode(value, default: str = "") -> str:
    if isinstance(value, bytes):
        return value.decode()
    if value is None:
        return default
    return str(value)


def select_targets(client, ids: list[str] | None = None) -> dict[str, str]:
    """Map torrent id -> name for the torrents that should be removed.

    With `ids`, state is ignored — the caller named these explicitly, and a
    tidied torrent is usually no longer Seeding. Ids that Deluge does not know
    about are simply absent from the result; removing something already gone is
    not an error.
    """
    state_filter = {} if ids else {"state": "Seeding"}
    try:
        torrents: dict = client.call(
            "core.get_torrents_status", state_filter, ["name"]
        )
    except Exception as exc:
        raise DelugeError(f"Failed to list torrents: {exc}") from exc

    wanted = {i.lower() for i in ids} if ids else None
    targets = {}
    for tid, info in (torrents or {}).items():
        tid_str = _decode(tid)
        if wanted is not None and tid_str.lower() not in wanted:
            continue
        name = info.get(b"name", info.get("name")) if isinstance(info, dict) else None
        targets[tid_str] = _decode(name, "<unknown>")
    return targets


def _write_artifact(removed: list[str], failed: list[tuple[str, str]], missing: list[str]) -> str:
    tmp_dir = _REPO_ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_path = tmp_dir / f"removed_seeding_{timestamp}.json"
    with open(artifact_path, "w") as f:
        json.dump(
            {
                "removed": removed,
                "failed": [{"name": name, "error": err} for name, err in failed],
                "not_found": missing,
            },
            f,
            indent=2,
        )
    return os.path.abspath(artifact_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--id",
        dest="ids",
        action="append",
        metavar="TORRENT_ID",
        help="Remove this torrent id regardless of state; repeatable. "
        "Without it, every Seeding torrent is removed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed and exit without touching Deluge.",
    )
    args = parser.parse_args(argv)

    config = load_config(_REPO_ROOT / "config.toml")
    noun = "torrent(s)" if args.ids else "seeding torrent(s)"

    with deluge.connect(config) as client:
        targets = select_targets(client, args.ids)
        missing = (
            [i for i in args.ids if i.lower() not in {t.lower() for t in targets}]
            if args.ids
            else []
        )

        if not targets:
            if args.ids:
                print(f"No matching torrents in Deluge (already removed?): {', '.join(missing)}")
            else:
                print("No seeding torrents found.")
            return

        if args.dry_run:
            print(f"Would remove {len(targets)} {noun}:")
            for name in targets.values():
                print(f"  - {name}")
            return

        removed, failed = [], []
        for tid, name in targets.items():
            try:
                client.call("core.remove_torrent", tid, False)
                removed.append(name)
            except Exception as exc:
                failed.append((name, str(exc)))

    if removed:
        print(_write_artifact(removed, failed, missing))

    print(f"Removed {len(removed)} {noun}:")
    for name in removed:
        print(f"  - {name}")

    if missing:
        print(f"\nNot in Deluge (already removed?): {', '.join(missing)}")

    if failed:
        print(f"\nFailed to remove {len(failed)}:")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except DelugeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
