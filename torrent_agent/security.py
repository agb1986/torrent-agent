"""Malware defenses for incoming torrents.

Two independent gates, deliberately not one:

- `flag_dangerous_files` runs in `torrent_agent.deluge.add_torrent`, right
  after Deluge has the file list but before the payload downloads. It only
  looks at names, so it is cheap enough to sit in the interactive add path,
  and it stops a fake release before a single byte of it lands on disk.
- `clamav_scan` runs in `server/pipeline.py`, after a download finishes and
  before it is tidied into the media library. It looks at contents, so it
  catches whatever a plausible filename got past — the backstop for a
  payload that didn't announce itself with a giveaway extension.

Neither is a substitute for the other: the first is fast but only as good as
its extension list, the second is thorough but only runs once the bytes are
already on disk.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

# Extensions a TV/movie release has no legitimate reason to carry. A real
# release group ships one video file plus subs/nfo; anything on this list
# showing up in the torrent's file list means it's either a fake release
# wrapped around a payload, or the payload with no wrapper at all.
DANGEROUS_SUFFIXES = {
    ".exe", ".scr", ".bat", ".cmd", ".com", ".msi", ".jar",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".ps1", ".lnk",
    ".apk", ".dmg", ".pkg",
}


def flag_dangerous_files(files: list[dict[str, Any]]) -> list[str]:
    """Names of any files in a Deluge file list carrying a dangerous suffix.

    `files` is what `core.get_torrent_status(tid, ["files"])` returns — a
    list of dicts with a "path" key, bytes or str depending on how
    deluge-client happened to decode that response.
    """
    flagged = []
    for f in files:
        raw = f.get(b"path") if isinstance(f, dict) and b"path" in f else f.get("path")
        path = raw.decode() if isinstance(raw, bytes) else (raw or "")
        if Path(path).suffix.lower() in DANGEROUS_SUFFIXES:
            flagged.append(path)
    return flagged


class ClamAVUnavailable(RuntimeError):
    pass


def clamav_scan(path: Path) -> list[str]:
    """Run clamscan over `path`. Returns names of infected files, [] if clean.

    Raises ClamAVUnavailable if clamscan is not installed — callers should
    treat that as "couldn't check", not "clean", and decide for themselves
    whether a missing scanner should block delivery.
    """
    binary = shutil.which("clamscan")
    if binary is None:
        raise ClamAVUnavailable("clamscan is not installed")
    proc = subprocess.run(
        [binary, "-r", "--infected", "--no-summary", str(path)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    # clamscan exit codes: 0 clean, 1 infected found, 2 error.
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"clamscan failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return [
        line.rsplit(":", 1)[0].strip()
        for line in proc.stdout.splitlines()
        if line.strip().endswith("FOUND")
    ]
