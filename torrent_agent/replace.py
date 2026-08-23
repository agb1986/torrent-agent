"""Swap out torrents that are never going to finish.

Three steps, same as the `replace` command's spec:

  1. Look at what Deluge is running and pick out the lost causes — stalled
     past a grace period, no seeders, not already close to done.
  2. Remove them (data included: a stalled torrent's partial bytes are of no
     use to a fresh search that will very likely land a different release).
  3. Re-fetch each one through the exact same path `get` uses — search, rank,
     check_vpn, add_torrent — telling the model a lower resolution than the
     original is fine this time, since availability is what killed it.

A "lost cause" is judged from what `deluge.list_torrents` already reports:
state, progress, seeders, rate and time_added. There is no persisted history
of when progress last moved, so the rule is conservative by construction —
no seeds AND no meaningful rate AND past the grace period AND not already
near complete — rather than trying to infer stall from a single snapshot.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guessit import guessit

from . import deluge
from .agent import build_agent
from .config import load_config

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Queued and Error both mean "not moving" as much as Downloading does — a
# tracker error with zero peers is exactly the case this exists to catch.
# Seeding/Checking/Paused are excluded: seeding is done, checking is
# transient, and paused is the user's own choice, not a stall.
_CANDIDATE_STATES = {"Downloading", "Queued", "Error"}


def find_lost_causes(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Torrents worth replacing, oldest-stalled first."""
    cfg = config.get("replace", {})
    stall_minutes = float(cfg.get("stall_minutes", 30))
    rate_threshold = float(cfg.get("rate_threshold_bytes", 1024))
    near_complete = float(cfg.get("near_complete_progress", 95.0))

    now = time.time()
    out = []
    for row in deluge.list_torrents(config):
        if row["state"] not in _CANDIDATE_STATES:
            continue
        if row["progress"] >= near_complete:
            continue
        if row["seeds"] > 0 or row["rate"] >= rate_threshold:
            continue
        added = row.get("time_added") or 0
        if added <= 0:
            # No timestamp to judge age from — skip rather than guess it's old.
            continue
        age_minutes = (now - added) / 60
        if age_minutes < stall_minutes:
            continue
        out.append({**row, "age_minutes": round(age_minutes, 1)})
    out.sort(key=lambda r: -r["age_minutes"])
    return out


def remove_lost_cause(torrent_id: str, config: dict[str, Any]) -> None:
    """Pull a stalled torrent out of Deluge, data and all.

    True (remove data) is deliberate here, unlike `remove_seeding.py`'s
    default: that removes *finished* torrents whose data is the whole point,
    this removes a partial download of a release that was going nowhere —
    keeping the fragment serves nobody.
    """
    with deluge.connect(config) as client:
        client.call("core.remove_torrent", torrent_id, True)


def replacement_query(name: str) -> tuple[str, str] | None:
    """What to search for instead, derived from the stalled torrent's name.

    None means guessit couldn't read a title — refuse rather than guess, the
    same rule `tidy.py` follows for naming.
    """
    guess = guessit(name)
    title = str(guess.get("title") or "").strip()
    if not title:
        return None

    year = guess.get("year")
    base = f"{title} {year}" if year else title

    kind = guess.get("type")
    if kind == "movie":
        return base, "movie"

    if kind == "episode":
        # The year is the only thing that tells a same-titled reboot/remake
        # apart (the "Frasier" bug, again: dropping it here once let a stalled
        # 2023-reboot episode search resolve to the 1993 original's episode of
        # the same number instead). Carry it through exactly as the movie
        # branch does whenever guessit found one in the release name.
        season = guess.get("season")
        episode = guess.get("episode")
        if isinstance(episode, list):  # "S01E01E02"-style multi-episode file
            episode = episode[0] if episode else None
        if isinstance(season, int) and isinstance(episode, int):
            return f"{base} S{season:02d}E{episode:02d}", "tv"
        if isinstance(season, int):
            return f"{base} S{season:02d}", "tv"
        return base, "tv"

    return None


DOWNGRADE_NOTE = (
    " The previous release for this stalled with no seeders and was removed "
    "— a lower resolution than usual is fine this time, prioritize seeders "
    "and availability over resolution."
)


def _write_artifact(removed: list[dict[str, Any]]) -> str:
    tmp_dir = _REPO_ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = tmp_dir / f"replaced_{timestamp}.json"
    with path.open("w") as fh:
        json.dump({"replaced": removed}, fh, indent=2)
    return os.path.abspath(path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="torrent-agent replace",
        description=(
            "Remove stalled, seederless torrents and re-fetch each from a "
            "different source, a quality downgrade allowed."
        ),
    )
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be replaced; touch nothing.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    candidates = find_lost_causes(config)
    if not candidates:
        print("No stalled torrents found.")
        return 0

    print(f"{len(candidates)} stalled torrent(s):")
    for c in candidates:
        print(
            f"  - {c['name']} — {c['progress']:.1f}%, {c['seeds']} seeds, "
            f"stalled {c['age_minutes']:.0f}m"
        )
    if args.dry_run:
        return 0

    exit_code = 0
    results: list[dict[str, Any]] = []
    for c in candidates:
        query = replacement_query(c["name"])
        entry: dict[str, Any] = {"was": c["name"]}
        if query is None:
            print(f"\nSkipping {c['name']!r} — could not read a title to search for.")
            entry["skipped"] = "no readable title"
            results.append(entry)
            continue

        text, media_type = query
        print(f"\nRemoving: {c['name']}")
        try:
            remove_lost_cause(c["id"], config)
        except deluge.DelugeError as exc:
            print(f"  error removing: {exc}")
            entry["error"] = str(exc)
            results.append(entry)
            exit_code = 1
            continue

        print(f"Replacing with: {text} ({media_type})")
        entry["query"] = text
        try:
            agent = build_agent(config)
            summary = agent.run(text + DOWNGRADE_NOTE)
        except RuntimeError as exc:
            print(f"  error: {exc}")
            entry["error"] = str(exc)
            results.append(entry)
            exit_code = 1
            continue

        entry["added"] = agent.added
        entry["summary"] = summary
        print(summary)
        results.append(entry)

    print("\n" + _write_artifact(results))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
