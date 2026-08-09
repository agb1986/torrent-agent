"""Remove all torrents in the Seeding state from Deluge (data is kept)."""

from __future__ import annotations

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


def main() -> None:
    config = load_config(_REPO_ROOT / "config.toml")

    with deluge.connect(config) as client:
        try:
            torrents: dict = client.call(
                "core.get_torrents_status",
                {"state": "Seeding"},
                ["name"],
            )
        except Exception as exc:
            raise DelugeError(f"Failed to list torrents: {exc}") from exc

        if not torrents:
            print("No seeding torrents found.")
            return

        removed, failed = [], []
        for tid, info in torrents.items():
            tid_str = tid.decode() if isinstance(tid, bytes) else str(tid)
            name = info.get(b"name", info.get("name", b"<unknown>"))
            if isinstance(name, bytes):
                name = name.decode()
            try:
                client.call("core.remove_torrent", tid_str, False)
                removed.append(name)
            except Exception as exc:
                failed.append((name, str(exc)))

    if removed:
        tmp_dir = _REPO_ROOT / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact_path = tmp_dir / f"removed_seeding_{timestamp}.json"
        with open(artifact_path, "w") as f:
            json.dump(
                {
                    "removed": removed,
                    "failed": [{"name": name, "error": err} for name, err in failed],
                },
                f,
                indent=2,
            )
        print(os.path.abspath(artifact_path))

    print(f"Removed {len(removed)} seeding torrent(s):")
    for name in removed:
        print(f"  - {name}")

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
