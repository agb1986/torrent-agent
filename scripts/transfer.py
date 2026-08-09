#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# --- Configuration --
SERVER_USER = "casaos"
SERVER_HOST = "casaos.local"

# sudo chown casaos:casaos /media/local/manga
DESTINATIONS = {
    "film": "/mnt/data/film",
    "tv": "/mnt/data/tv",
    "book": "/media/local/books",
    "manga": "/media/local/manga"
}

PLEX_URL = "http://casaos.local:32400"
PLEX_TOKEN_ENV = "PLEX_TOKEN"

# The plex container bind-mounts /mnt/data as /Media, so paths Plex reports are
# not the paths we rsync to.  Scans silently no-op on an untranslated path.
PLEX_PATH_MAP = {"/mnt/data": "/Media"}
# ---------------------


def build_remote(path: str) -> str:
    return f"{SERVER_USER}@{SERVER_HOST}:{path}"


def transfer(source: str, remote_dest: str) -> int:
    source = source.rstrip("/")
    cmd = [
        "rsync",
        "--archive",       # preserves permissions, timestamps, symlinks, etc.
        "--verbose",
        "--progress",
        "--human-readable",
        "--rsh", "ssh -i ~/.ssh/id_rsa_ha -o PubkeyAuthentication=yes -o PasswordAuthentication=no",
        source,
        remote_dest,
    ]
    print(f"\nTransferring: {source} -> {remote_dest}")
    print(f"Command: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    return result.returncode


def to_plex_path(host_path: str) -> str | None:
    """Translate a host path into the path the plex container sees."""
    for host_prefix, plex_prefix in PLEX_PATH_MAP.items():
        if host_path == host_prefix or host_path.startswith(host_prefix + "/"):
            return plex_prefix + host_path[len(host_prefix):]
    return None


def plex_get(path: str, token: str) -> bytes:
    sep = "&" if "?" in path else "?"
    url = f"{PLEX_URL}{path}{sep}X-Plex-Token={urllib.parse.quote(token)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def find_plex_section(plex_path: str, token: str) -> str | None:
    """Find the library section whose location contains plex_path.

    Looked up rather than hardcoded so the scan still works if the libraries
    are ever rebuilt with different section keys.
    """
    root = ET.fromstring(plex_get("/library/sections", token))
    for directory in root.findall("Directory"):
        for location in directory.findall("Location"):
            location_path = location.get("path", "")
            if plex_path == location_path or plex_path.startswith(location_path + "/"):
                return directory.get("key")
    return None


def scan_plex(host_path: str) -> None:
    """Ask Plex to scan just the directory we transferred into.

    Best-effort: the files are already on the server, so a failure here is
    reported but never fails the transfer.
    """
    token = os.environ.get(PLEX_TOKEN_ENV)
    if not token:
        print(f"[WARN] ${PLEX_TOKEN_ENV} not set — skipping Plex scan")
        return

    plex_path = to_plex_path(host_path)
    if plex_path is None:
        print(f"[WARN] No Plex path mapping for {host_path} — skipping Plex scan")
        return

    try:
        section = find_plex_section(plex_path, token)
        if section is None:
            print(f"[WARN] No Plex library covers {plex_path} — skipping scan")
            return
        # A partial scan walks only this directory instead of the whole library.
        # Note Plex answers 200 even for a path it does not recognise, so this
        # confirms the request was accepted, not that anything was found.
        plex_get(
            f"/library/sections/{section}/refresh"
            f"?path={urllib.parse.quote(plex_path)}",
            token,
        )
        print(f"Plex: scanning section {section} at {plex_path}")
    except (urllib.error.URLError, ET.ParseError, OSError) as exc:
        print(f"[WARN] Plex scan failed ({exc}) — transfer itself was fine")


def main():
    parser = argparse.ArgumentParser(
        description="Transfer files to the local server via rsync over SSH."
    )
    parser.add_argument(
        "source",
        help="Path to the files/directory to transfer",
    )

    # Category flags — add new ones here as needed
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
            f"Specify at least one destination flag: "
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

    # Scan once everything has landed, so Plex sees the finished files.
    for dest in transferred:
        source = args.source.rstrip("/")
        # rsync puts a directory inside the destination; scan just that
        # subdirectory.  A file lands loose, so its directory is the target.
        if os.path.isdir(source):
            scan_plex(f"{dest}/{os.path.basename(source)}")
        else:
            scan_plex(dest)

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
