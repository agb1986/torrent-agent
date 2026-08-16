"""Rename a finished download into the layout Jellyfin matches on.

    python scripts/tidy.py --dry-run "/mnt/data/downloads/Some.Release"
    python scripts/tidy.py "/mnt/data/downloads/Some.Release"

Prints the plan and refuses to act on anything it is not sure about — see
torrent_agent/tidy.py for what "sure" means. Exit 2 means "needs a human",
which is what the automated pipeline escalates on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent.tidy import execute, plan_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", help="Downloaded file or directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and change nothing.",
    )
    args = parser.parse_args(argv)

    plan = plan_for(args.source)
    print(plan.describe())

    if plan.left_behind:
        print(f"\nleaving {len(plan.left_behind)} non-media file(s) in place")

    if not plan.confident:
        print("\nNot confident — nothing changed. This needs a human.")
        return 2
    if args.dry_run:
        print("\n(dry run — nothing changed)")
        return 0

    execute(plan)
    print(f"\nTidied into: {plan.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
