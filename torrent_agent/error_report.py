"""A durable, append-only log of failures for an agent to read and act on.

Same shape and rationale as `server/bot.py`'s `record()`: one JSON object per
line, flushed immediately, so a `kill -9` mid-write leaves every prior line
intact rather than corrupting a single rewritten file. Gitignored, at the repo
root, so it survives across runs of whatever consumes it without living in
version control.

This is a superset log, not a replacement for `doctor_alert.py`'s Telegram
alert (which fires only on a *change* in what's failing, to avoid a daily "2
checks still failing" that trains you to ignore it) or
`server/notifier.py`'s completion messages — those are for a human, this is
for an agent, and every failure is worth a line here even if nobody was
paged about it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("torrent_agent.error_report")

_REPO_ROOT = Path(__file__).resolve().parent.parent
PATH = _REPO_ROOT / "error-report.json"


def record_error(component: str, summary: str, detail: str = "") -> None:
    """Append one failure. Best-effort — must never raise."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "component": component,
        "summary": summary,
        "detail": detail,
    }
    try:
        with PATH.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("could not write error-report.json: %s", exc)
