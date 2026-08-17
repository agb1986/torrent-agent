"""Snapshot the small state that is expensive to lose.

Nothing here is large — a few hundred kilobytes — but each piece fails
silently when it goes:

  * `tmp/subscriptions.json` — lose it and `/sub` simply stops fetching. No
    error, no message, just a series that quietly never gets another episode.
  * `tmp/notified_torrents.json` — lose it and every finished torrent is
    announced again on the next start, which trains you to ignore the bot.
  * Deluge's `core.conf` and `state/` — lose those and the torrent list is
    gone along with the VPN interface binding, and Deluge starts up perfectly
    happy with an empty session.

Media is deliberately **not** backed up. It is terabytes, it is replaceable,
and a backup that cannot finish is not a backup.

    python scripts/backup.py            # write one, rotate old ones out
    python scripts/backup.py --list
    python scripts/backup.py --json

Restoring is a deliberate manual act, not a flag: `tar xzf` somewhere, look at
what is in it, then put files back with the services **stopped**. deluged
rewrites core.conf on shutdown, so restoring it under a running daemon writes
your restore straight back out again.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent.config import load_config  # noqa: E402
from torrent_agent.deluge import fmt_size  # noqa: E402

_PREFIX = "torrent-agent-state-"

# The service state worth keeping, by name under tmp/. Globs rather than a
# whole-directory sweep: tmp/ also collects run artifacts and removal receipts,
# which are logs of what happened rather than state anything reads back.
_STATE_GLOBS = ("subscriptions.json", "notified_torrents.json",
                "bot_daily_count.json", "bot_audit.jsonl")

# From Deluge's config directory. `state/` holds torrents.state plus a .torrent
# per entry — that is the session itself.
_DELUGE_ITEMS = ("core.conf", "state")

# Never: it is a credential, and a backup archive is a much easier thing to
# copy around carelessly than the file it came from. Deluge regenerates it.
_DELUGE_SKIP = {"auth"}


def deluge_config_dir(config: dict[str, Any]) -> Path:
    """Where Deluge keeps core.conf, for whichever daemon is configured.

    Derived from `[deluge] auth_file` when it is set, because that is already
    how a containerised daemon gets pointed at its own config directory — one
    setting to get right rather than two that can disagree.
    """
    explicit = config.get("backup", {}).get("deluge_config_dir", "")
    if explicit:
        return Path(explicit).expanduser()
    auth = config.get("deluge", {}).get("auth_file", "")
    if auth:
        return Path(auth).expanduser().resolve().parent
    return Path.home() / ".config" / "deluge"


def backup_dir(config: dict[str, Any]) -> Path:
    configured = config.get("backup", {}).get("dir", "")
    return Path(configured).expanduser() if configured else _REPO_ROOT / "tmp" / "backups"


def collect(config: dict[str, Any], repo_root: Path = _REPO_ROOT) -> list[tuple[Path, str]]:
    """(source path, name inside the archive) for everything that exists.

    Missing pieces are skipped rather than fatal: a machine that runs the bot
    but not Deluge is a legitimate setup, and so is a first run before any
    subscription exists.
    """
    items: list[tuple[Path, str]] = []
    for name in _STATE_GLOBS:
        path = repo_root / "tmp" / name
        if path.is_file():
            items.append((path, f"state/{name}"))

    deluge_dir = deluge_config_dir(config)
    for name in _DELUGE_ITEMS:
        path = deluge_dir / name
        if path.exists():
            items.append((path, f"deluge/{name}"))
    return items


def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if Path(info.name).name in _DELUGE_SKIP else info


def write_backup(config: dict[str, Any], repo_root: Path = _REPO_ROOT,
                 now: datetime | None = None) -> dict[str, Any]:
    items = collect(config, repo_root)
    if not items:
        return {"path": "", "items": [], "bytes": 0,
                "note": "nothing to back up — no state files and no Deluge config found"}

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir(config)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{_PREFIX}{stamp}.tar.gz"

    # Write to a temporary name and rename into place: a backup interrupted
    # half-written must not look like a complete one, since the only time
    # anyone reads these is when something has already gone wrong.
    tmp = path.with_suffix(".partial")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for source, arcname in items:
                tar.add(source, arcname=arcname, filter=_filter)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

    return {"path": str(path), "items": [a for _, a in items],
            "bytes": path.stat().st_size, "note": ""}


def existing(config: dict[str, Any]) -> list[Path]:
    """Backups on disk, newest first."""
    dest = backup_dir(config)
    if not dest.is_dir():
        return []
    return sorted(dest.glob(f"{_PREFIX}*.tar.gz"), reverse=True)


def rotate(config: dict[str, Any]) -> list[str]:
    """Delete all but the newest `keep`. Returns what went."""
    keep = int(config.get("backup", {}).get("keep", 14))
    if keep <= 0:
        return []
    removed = []
    for path in existing(config)[keep:]:
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--list", action="store_true", help="Show existing backups and exit.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.list:
        rows = [{"path": str(p), "bytes": p.stat().st_size} for p in existing(config)]
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print(f"No backups in {backup_dir(config)}")
        else:
            for row in rows:
                print(f"  {fmt_size(row['bytes']):>10}  {row['path']}")
        return 0

    result = write_backup(config)
    result["rotated"] = rotate(config)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["path"] else 1

    if not result["path"]:
        print(result["note"], file=sys.stderr)
        return 1
    print(f"{result['path']}  ({fmt_size(result['bytes'])})")
    for item in result["items"]:
        print(f"  + {item}")
    for gone in result["rotated"]:
        print(f"  - rotated out {Path(gone).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
