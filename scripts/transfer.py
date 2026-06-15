#!/usr/bin/env python3

import argparse
import subprocess
import sys
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
        "--rsh", "ssh -o PubkeyAuthentication=no -o PasswordAuthentication=yes",
        source,
        remote_dest,
    ]
    print(f"\nTransferring: {source} -> {remote_dest}")
    print(f"Command: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    return result.returncode


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
    for flag in selected:
        remote_dest = build_remote(DESTINATIONS[flag])
        code = transfer(args.source, remote_dest)
        exit_codes.append(code)
        if code != 0:
            print(f"[ERROR] Transfer to --{flag} failed (exit code {code})")

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
