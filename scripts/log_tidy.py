"""Log a completed tidy-files run as a JSON artifact for ai-data-store.

Reads a JSON object from stdin:

    {"kind": "tv"|"film", "name": "<show/film name>",
     "mapping": [{"from": "...", "to": "..."}, ...]}

Writes it to tmp/tidy_<slug>_<timestamp>.json and prints only that path — the
ai-data-store PostToolUse hook picks up the path from stdout, so this script
must not print anything else.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "tidy"


def main() -> None:
    data = json.load(sys.stdin)

    tmp_dir = _REPO_ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slugify(data.get("name", ""))
    path = tmp_dir / f"tidy_{slug}_{timestamp}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    print(os.path.abspath(path))


if __name__ == "__main__":
    main()
