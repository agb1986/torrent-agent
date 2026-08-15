"""Show the status of all torrents in Deluge: state, progress, speed, ETA."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent import deluge
from torrent_agent.config import load_config
from torrent_agent.deluge import DelugeError

_FIELDS = [
    "name",
    "state",
    "progress",
    "eta",
    "download_payload_rate",
    "total_wanted",
]


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _fmt_eta(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def main() -> None:
    config = load_config()

    with deluge.connect(config) as client:
        try:
            torrents: dict = client.call("core.get_torrents_status", {}, _FIELDS)
        except Exception as exc:
            raise DelugeError(f"Failed to list torrents: {exc}") from exc

    if not torrents:
        print("No torrents in Deluge.")
        return

    rows = []
    for info in torrents.values():
        info = {_decode(k): v for k, v in info.items()}
        rows.append(
            {
                "name": _decode(info.get("name", "<unknown>")),
                "state": _decode(info.get("state", "?")),
                "progress": float(info.get("progress", 0.0)),
                "eta": _fmt_eta(int(info.get("eta", 0))),
                "rate": _fmt_size(float(info.get("download_payload_rate", 0))) + "/s",
                "size": _fmt_size(float(info.get("total_wanted", 0))),
            }
        )

    # Active downloads first, then by name, so the interesting rows lead.
    order = {"Downloading": 0, "Checking": 1, "Queued": 2, "Seeding": 3, "Paused": 4}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["name"].lower()))

    name_w = min(max(len(r["name"]) for r in rows), 60)
    header = f"{'NAME':<{name_w}}  {'STATE':<12} {'PROG':>6}  {'RATE':>11}  {'ETA':>7}  {'SIZE':>9}"
    print(header)
    print("-" * len(header))
    for r in rows:
        name = r["name"][:name_w]
        print(
            f"{name:<{name_w}}  {r['state']:<12} {r['progress']:>5.1f}%"
            f"  {r['rate']:>11}  {r['eta']:>7}  {r['size']:>9}"
        )

    downloading = sum(1 for r in rows if r["state"] == "Downloading")
    print(f"\n{len(rows)} torrent(s), {downloading} downloading.")


if __name__ == "__main__":
    try:
        main()
    except DelugeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
