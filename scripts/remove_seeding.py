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

from torrent_agent.config import load_config
from torrent_agent.deluge import DelugeError, _read_localclient_auth

from deluge_client import DelugeRPCClient


def _connect(cfg: dict) -> DelugeRPCClient:
    dc = cfg.get("deluge", {})
    username = dc.get("username") or ""
    password = dc.get("password") or ""
    if not (username and password):
        auth = _read_localclient_auth()
        if auth:
            username, password = auth
        else:
            raise DelugeError(
                "No Deluge credentials found — ensure ~/.config/deluge/auth exists."
            )
    client = DelugeRPCClient(
        dc.get("host", "127.0.0.1"),
        int(dc.get("port", 58846)),
        username,
        password,
    )
    try:
        client.connect()
    except Exception as exc:
        raise DelugeError(f"Could not connect to deluged: {exc}") from exc
    return client


def main() -> None:
    config = load_config()
    client = _connect(config)

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

    try:
        client.disconnect()
    except Exception:
        pass

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
    main()
