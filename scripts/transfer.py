#!/usr/bin/env python3
"""Deliver media to the media server, then trigger a Jellyfin scan.

Two modes, chosen automatically:

- **Local move** when the configured server is this machine. Since the stack
  moved onto CasaOS, downloads land on the same box as /mnt/data/tv, and both
  live on one filesystem — so delivery is a rename, not a 16 GB copy over SSH
  to ourselves. Set `[server] local` to override the detection.
- **rsync over SSH** otherwise, which is still how a laptop delivers.

Server, destinations, and Jellyfin settings come from config.toml ([server] and
[jellyfin] sections); the Jellyfin API key comes from the JELLYFIN_API_KEY env
var (or [jellyfin].api_key).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
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


def _local_addresses() -> set[str]:
    addrs = {"127.0.0.1", "::1"}
    for name in {socket.gethostname(), socket.getfqdn()}:
        try:
            addrs.update(info[4][0] for info in socket.getaddrinfo(name, None))
        except OSError:
            pass
    return addrs


def server_is_local(server: dict | None = None) -> bool:
    """True when the media server is this machine.

    Since the stack moved to CasaOS, downloads land on the same box as
    /mnt/data/tv, and rsyncing over SSH to ourselves would copy 16 GB to
    reach a directory two levels away. Set `[server] local` to force the
    answer; otherwise it is inferred from the configured host.
    """
    server = SERVER if server is None else server
    explicit = server.get("local")
    if explicit is not None:
        return bool(explicit)

    host = str(server.get("host") or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.split(".")[0] == socket.gethostname().split(".")[0].lower():
        return True
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        # Unresolvable means we certainly cannot claim it is us.
        return False
    return bool(resolved & _local_addresses())


def _merge_move(source: str, target: str) -> None:
    """Move `source` into `target`, merging instead of nesting.

    shutil.move onto an existing directory puts the source *inside* it —
    `Show/Show/Season 01` — which is not what rsync does. Adding a second
    season to a show already on the server is the ordinary case here, so the
    merge is walked file by file.
    """
    for root, _dirs, files in os.walk(source):
        rel = os.path.relpath(root, source)
        dest_dir = target if rel == "." else os.path.join(target, rel)
        os.makedirs(dest_dir, exist_ok=True)
        for name in files:
            shutil.move(os.path.join(root, name), os.path.join(dest_dir, name))
    shutil.rmtree(source, ignore_errors=True)


def transfer_local(source: str, dest: str) -> int:
    """Move into place on this machine, rather than copying over the network.

    A move, not a copy: on the server both paths are the same filesystem, so
    this is a rename — instant, and it does not leave a second copy of a whole
    season behind. That does mean the torrent must already be removed from
    Deluge, exactly as the remote path required.
    """
    source = source.rstrip("/")
    target = os.path.join(dest, os.path.basename(source))
    print(f"\nMoving (same host): {source} -> {target}")
    try:
        os.makedirs(dest, exist_ok=True)
        if os.path.isdir(source) and os.path.isdir(target):
            _merge_move(source, target)
        else:
            shutil.move(source, target)
    except OSError as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


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


# Never through a proxy. The bot routes Telegram via gluetun's HTTP proxy, and
# urllib honours HTTP_PROXY for every request in the process — so the library
# scan was being sent out through the VPN and back at a LAN address, which
# fails in a way nothing reports. NO_PROXY can express this, but only if
# whoever wrote it thought of every hostname; this cannot be forgotten.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
    with _DIRECT.open(req, timeout=30) as response:
        return response.status


def media_server_kind() -> str:
    """Which media server to tell, or "none".

    Explicit [media_server] kind wins. Otherwise it is inferred: a configured
    url means Jellyfin (how this was always set up), and no url means there is
    no media server — which is a legitimate setup, not a misconfiguration, so
    it must not warn on every delivery.
    """
    kind = str(CONFIG.get("media_server", {}).get("kind") or "").lower()
    if kind:
        return kind
    return "jellyfin" if JELLYFIN.get("url") else "none"


def notify_library(host_path: str) -> None:
    """Tell the media server about the path we just delivered into.

    Uses /Library/Media/Updated (the targeted endpoint the *arr apps use) and
    falls back to a full /Library/Refresh. Best-effort: the files are already
    in place, so a failure here is reported but never fails the transfer.

    Emby shares Jellyfin's API for both endpoints and its X-Emby-Token header,
    so one path serves both. Plex differs enough to need its own client and is
    not supported — say so rather than failing obscurely.
    """
    kind = media_server_kind()
    if kind == "none":
        return
    if kind not in {"jellyfin", "emby"}:
        print(f"[WARN] media_server kind {kind!r} is not supported — skipping scan")
        return

    api_key = os.environ.get("JELLYFIN_API_KEY") or JELLYFIN.get("api_key")
    if not api_key:
        print(f"[WARN] no API key for {kind} — skipping library scan")
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


# Old name, still used by server/pipeline.py and the skills.
scan_jellyfin = notify_library


def main():
    parser = argparse.ArgumentParser(
        description="Transfer media to the server: a local move when it is this machine, otherwise rsync over SSH."
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
    local = server_is_local()
    print(f"Mode: {'local move' if local else 'rsync over SSH'} "
          f"(server host: {SERVER['host'] or '(unset)'})")

    for flag in selected:
        if local:
            code = transfer_local(args.source, DESTINATIONS[flag])
        else:
            code = transfer(args.source, build_remote(DESTINATIONS[flag]))
        exit_codes.append(code)
        if code != 0:
            print(f"[ERROR] Transfer to --{flag} failed (exit code {code})")
        else:
            transferred.append(DESTINATIONS[flag])

    # Scan once everything has landed, so Jellyfin sees the finished files.
    for dest in transferred:
        source = args.source.rstrip("/")
        # Both modes put a directory inside the destination; scan just that
        # subdirectory. A file lands loose, so its directory is the target.
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
