"""Run the doctor on a schedule and say something on Telegram when it changes.

`scripts/doctor.py` knows about every failure this stack has actually had, and
all of them share the property that nothing else reports them: a dead VPN
binding, a forwarded port Deluge is not listening on, an API key silently
overwritten with another service's. The checks exist — but until now they only
ran when a human thought to type them, which is exactly the moment they are
least likely to be needed.

Messages on **change**, not on state. A daily "2 checks failing" for a fault
you already know about is noise, and noise is how a monitor becomes something
you swipe away without reading. So:

  * a check that starts failing            -> message
  * a check that stops failing             -> message, saying so
  * the same failures as last time         -> silence
  * nothing failing, nothing changed       -> silence

Which means an unread inbox genuinely means healthy, and that is the only
property that makes it worth having.

    python scripts/doctor_alert.py             # normal, scheduled use
    python scripts/doctor_alert.py --force      # send even if nothing changed
    python scripts/doctor_alert.py --dry-run    # print, send nothing

Exit 1 if a check is failing, so `systemctl --user status` and the journal
show the fault too, for anyone looking there rather than at their phone.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from doctor import FAIL, run_checks  # noqa: E402

STATE_PATH = _REPO_ROOT / "tmp" / "doctor_alerts.json"


def load_previous(path: Path = STATE_PATH) -> set[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    return set(data.get("failing", [])) if isinstance(data, dict) else set()


def save_current(failing: set[str], path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"failing": sorted(failing)}, indent=2))
        tmp.replace(path)
    except OSError as exc:
        print(f"could not persist alert state: {exc}", file=sys.stderr)


def compose(checks, previous: set[str], host: str) -> tuple[str, set[str]]:
    """The message to send, and the failing set to remember.

    An empty message means nothing changed, which is the usual answer.
    """
    failing = {c.name: c for c in checks if c.status == FAIL}
    names = set(failing)
    new = sorted(names - previous)
    fixed = sorted(previous - names)

    if not new and not fixed:
        return "", names

    lines: list[str] = []
    if new:
        lines.append(f"⚠ torrent-agent on {host}: {len(new)} check(s) started failing")
        for name in new:
            check = failing[name]
            lines.append(f"\n{name} — {check.detail}")
            if check.fix:
                lines.append(f"  fix: {check.fix}")
    if fixed:
        if lines:
            lines.append("")
        lines.append(f"✅ recovered on {host}: " + ", ".join(fixed))

    still = sorted(names.intersection(previous))
    if still:
        lines.append("\nstill failing: " + ", ".join(still))

    return "\n".join(lines), names


def send(text: str) -> str:
    """Push to every allowed chat. Returns "" on success, else the reason.

    Deliberately reuses the bot's client, so the token scrubbing that keeps
    the credential out of the journal applies here too — this process runs
    unattended and its stderr goes straight to systemd.
    """
    from server.bot import _allowed_ids
    from server.telegram import TelegramClient, TelegramError

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = _allowed_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    if not token or not chat_ids:
        return "no TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_CHAT_IDS — cannot alert"

    client = TelegramClient(token)
    errors = []
    for chat_id in sorted(chat_ids):
        try:
            client.send_message(chat_id, text)
        except TelegramError as exc:
            errors.append(f"{chat_id}: {exc}")
    return "; ".join(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--force", action="store_true",
                        help="Send the current state even if nothing changed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the message instead of sending it, and do not "
                             "record the state.")
    args = parser.parse_args(argv)

    report = run_checks(args.config)
    host = socket.gethostname()
    previous = load_previous()
    text, failing = compose(report.checks, previous, host)

    if args.force and not text:
        text = (f"torrent-agent on {host}: "
                + (f"{len(failing)} failing — " + ", ".join(sorted(failing))
                   if failing else "all checks passing"))

    for check in report.failed:
        print(f"FAIL {check.name}: {check.detail}", file=sys.stderr)

    if text:
        if args.dry_run:
            print(text)
        else:
            problem = send(text)
            if problem:
                # Do NOT record the state: an alert that failed to send has
                # not been seen, and recording it here would mean the next run
                # considers it old news and stays quiet forever.
                print(f"alert not delivered: {problem}", file=sys.stderr)
                return 1
    elif args.dry_run:
        print("(no change — nothing would be sent)")

    if not args.dry_run:
        save_current(failing)
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
