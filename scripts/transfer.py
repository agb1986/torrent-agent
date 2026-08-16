#!/usr/bin/env python3
"""Transfer media to the CASAOS server via rsync, then trigger a Jellyfin scan.

Server, destinations, and Jellyfin settings come from config.toml ([server] and
[jellyfin] sections); the Jellyfin API key comes from the JELLYFIN_API_KEY env
var (or [jellyfin].api_key).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent.config import load_config

CONFIG = load_config()
SERVER = CONFIG["server"]
JELLYFIN = CONFIG["jellyfin"]
DESTINATIONS = SERVER["destinations"]


def build_remote(path: str) -> str:
    return f"{SERVER['user']}@{SERVER['host']}:{path}"


def transfer(source: str, remote_dest: str) -> int:
    source = source.rstrip("/")
    rsh = (
        f"ssh -i {SERVER['ssh_key']} -o PubkeyAuthentication=yes "
        f"-o PasswordAuthentication=no"
    )
    cmd = [
        "rsync",
        "--archive",       # preserves permissions, timestamps, symlinks, etc.
        "--verbose",
        "--progress",
        "--human-readable",
        "--rsh", rsh,
        source,
        remote_dest,
    ]
    print(f"\nTransferring: {source} -> {remote_dest}")
    print(f"Command: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    return result.returncode


def to_jellyfin_path(host_path: str) -> str | None:
    """Translate a server path into the path the Jellyfin container sees.

    Longest prefix wins — the container mounts tv and film separately
    (/mnt/data/tv -> /media/tv, /mnt/data/film -> /media/movies), so this is
    not a single-prefix rewrite like the old Plex setup.
    """
    best = None
    for host_prefix, jf_prefix in JELLYFIN.get("path_map", {}).items():
        if host_path == host_prefix or host_path.startswith(host_prefix + "/"):
            if best is None or len(host_prefix) > len(best[0]):
                best = (host_prefix, jf_prefix)
    if best is None:
        return None
    return best[1] + host_path[len(best[0]):]


def _jellyfin_post(path: str, api_key: str, body: dict | None = None) -> int:
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        f"{JELLYFIN['url']}{path}",
        data=data,
        method="POST",
        headers={
            "X-Emby-Token": api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.status


def scan_jellyfin(host_path: str) -> None:
    """Tell Jellyfin about the path we just transferred into.

    Uses /Library/Media/Updated (the targeted endpoint the *arr apps use) and
    falls back to a full /Library/Refresh. Best-effort: the files are already
    on the server, so a failure here is reported but never fails the transfer.
    """
    api_key = os.environ.get("JELLYFIN_API_KEY") or JELLYFIN.get("api_key")
    if not api_key:
        print("[WARN] JELLYFIN_API_KEY not set — skipping Jellyfin scan")
        return

    jf_path = to_jellyfin_path(host_path)
    if jf_path is None:
        print(f"[WARN] No Jellyfin path mapping for {host_path} — skipping scan")
        return

    try:
        status = _jellyfin_post(
            "/Library/Media/Updated",
            api_key,
            {"Updates": [{"Path": jf_path, "UpdateType": "Created"}]},
        )
        print(f"Jellyfin: notified of new media at {jf_path} (HTTP {status})")
    except (urllib.error.URLError, OSError) as exc:
        print(f"[WARN] Targeted Jellyfin update failed ({exc}) — trying full scan")
        try:
            status = _jellyfin_post("/Library/Refresh", api_key)
            print(f"Jellyfin: full library scan started (HTTP {status})")
        except (urllib.error.URLError, OSError) as exc2:
            print(f"[WARN] Jellyfin scan failed ({exc2}) — transfer itself was fine")


def main():
    parser = argparse.ArgumentParser(
        description="Transfer files to the local server via rsync over SSH."
    )
    parser.add_argument(
        "source",
        help="Path to the files/directory to transfer",
    )

    # Category flags — driven by [server.destinations] in config.toml
    for flag in DESTINATIONS:
        parser.add_argument(
            f"--{flag}",
            action="store_true",
            help=f"Transfer to the {flag} destination ({DESTINATIONS[flag]})",
        )

    args = parser.parse_args()

    # Collect which flags were set
    selected = [flag for flag in DESTINATIONS if getattr(args, flag)]

    if not selected:
        parser.error(
            "Specify at least one destination flag: "
            + ", ".join(f"--{f}" for f in DESTINATIONS)
        )

    start = datetime.now()

    exit_codes = []
    transferred = []
    for flag in selected:
        remote_dest = build_remote(DESTINATIONS[flag])
        code = transfer(args.source, remote_dest)
        exit_codes.append(code)
        if code != 0:
            print(f"[ERROR] Transfer to --{flag} failed (exit code {code})")
        else:
            transferred.append(DESTINATIONS[flag])

    # Scan once everything has landed, so Jellyfin sees the finished files.
    for dest in transferred:
        source = args.source.rstrip("/")
        # rsync puts a directory inside the destination; scan just that
        # subdirectory.  A file lands loose, so its directory is the target.
        if os.path.isdir(source):
            scan_jellyfin(f"{dest}/{os.path.basename(source)}")
        else:
            scan_jellyfin(dest)

    end = datetime.now()
    duration = end - start
    total_seconds = int(duration.total_seconds())
    h, remainder = divmod(total_seconds, 3600)
    m, s = divmod(remainder, 60)
    print(f"\nStarted:  {start.strftime('%H:%M:%S')}")
    print(f"Ended:    {end.strftime('%H:%M:%S')}")
    print(f"Duration: {h:02d}:{m:02d}:{s:02d}")

    sys.exit(1 if any(c != 0 for c in exit_codes) else 0)


if __name__ == "__main__":
    main()
