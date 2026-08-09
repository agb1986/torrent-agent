"""Command-line entrypoint for the torrent agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agent import build_agent
from .config import load_config

# Anchor artifacts to the repo, not the cwd — the ai-data-store hook watches
# this repo's tmp/ directory, and the CLI may be invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "request"


def _write_added_artifact(request: str, added: list[dict]) -> str:
    tmp_dir = _REPO_ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = tmp_dir / f"added_{_slugify(request)}_{timestamp}.json"
    with open(path, "w") as f:
        json.dump({"request": request, "added": added}, f, indent=2)
    return os.path.abspath(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="torrent-agent",
        description="Find a torrent with Claude and add it to Deluge.",
    )
    parser.add_argument(
        "request",
        nargs="*",
        help="What to fetch, e.g. 'the bear season 3 1080p'. Omit to be prompted.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="Path to config file (default: config.toml).",
    )
    args = parser.parse_args(argv)

    request = " ".join(args.request).strip()
    if not request:
        try:
            request = input("What should I fetch? ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not request:
        parser.error("no request given")

    config = load_config(args.config)
    try:
        agent = build_agent(config)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        result = agent.run(request)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if agent.added:
        print(_write_added_artifact(request, agent.added))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
