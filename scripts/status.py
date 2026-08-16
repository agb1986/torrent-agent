"""Show the status of all torrents in Deluge: state, progress, speed, ETA."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent.config import load_config
from torrent_agent.deluge import fmt_eta, fmt_size, list_torrents


def main() -> None:
    rows = list_torrents(load_config())
    if not rows:
        print("No torrents in Deluge.")
        return

    name_w = min(max(len(r["name"]) for r in rows), 60)
    header = (
        f"{'NAME':<{name_w}}  {'STATE':<12} {'PROG':>6}  "
        f"{'RATE':>11}  {'ETA':>7}  {'SIZE':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['name'][:name_w]:<{name_w}}  {r['state']:<12} {r['progress']:>5.1f}%"
            f"  {fmt_size(r['rate']) + '/s':>11}  {fmt_eta(r['eta']):>7}"
            f"  {fmt_size(r['size']):>9}"
        )

    active = sum(1 for r in rows if r["state"] == "Downloading")
    print(f"\n{len(rows)} torrent(s), {active} downloading.")


if __name__ == "__main__":
    main()
