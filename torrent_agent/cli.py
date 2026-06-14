"""Command-line entrypoint for the torrent agent."""

from __future__ import annotations

import argparse
import sys

from .agent import build_agent
from .config import load_config


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
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
