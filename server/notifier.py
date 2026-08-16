"""Tell Telegram when a download finishes.

The bot says what it added and then goes quiet for the hours the download
actually takes. Nothing else watches: the `fetch-to-jellyfin` skill polls for
completion, but only while a session is open and driving it, and Deluge's own
Execute plugin is disabled. So a torrent finishes, moves itself to
`complete/`, and sits there with nobody told.

Deliberately notify-only. It reports; it does not tidy, transfer, or delete.
Automating delivery means writing into a real media library on a guess from
TMDB matching, which wants watching by hand a few times first.

    python -m server.notifier            # poll forever
    python -m server.notifier --once     # single pass, for testing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from torrent_agent.config import load_config
from torrent_agent.deluge import DelugeError, fmt_size, list_torrents

from .runner import REPO_ROOT
from .telegram import TelegramClient, TelegramError

log = logging.getLogger("server.notifier")

# Downloads run for minutes to hours, so a slow poll is plenty and keeps the
# RPC quiet. The cost of being a minute late is nil.
POLL_SECONDS = 60


class CompletionNotifier:
    def __init__(
        self,
        client: TelegramClient,
        chat_ids: list[int],
        config: dict[str, Any],
        state_path: Path,
        interval: int = POLL_SECONDS,
    ) -> None:
        self.client = client
        self.chat_ids = list(chat_ids)
        self.config = config
        self.state_path = Path(state_path)
        self.interval = interval

    # --- state ------------------------------------------------------------
    # Which torrents have already been announced. Persisted so a restart is
    # silent: without this, every finished torrent in the session would be
    # re-announced on each start, which trains you to ignore the messages.
    def _load_seen(self) -> set[str]:
        try:
            return set(json.loads(self.state_path.read_text()))
        except (OSError, ValueError):
            return set()

    def _save_seen(self, seen: set[str]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(seen)))
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("could not persist notifier state: %s", exc)

    # --- polling ----------------------------------------------------------
    def poll_once(self) -> list[dict[str, Any]]:
        """Announce anything newly finished. Returns what was announced."""
        rows = list_torrents(self.config)
        finished = {r["id"]: r for r in rows if r.get("finished")}

        seen = self._load_seen()
        first_run = not self.state_path.exists()
        if first_run:
            # Adopt the current state instead of announcing it. Otherwise the
            # first start after installing this shouts about every torrent
            # that completed weeks ago.
            self._save_seen(set(finished))
            log.info("first run — adopting %d finished torrent(s)", len(finished))
            return []

        fresh = [r for tid, r in finished.items() if tid not in seen]
        for row in fresh:
            self._announce(row)

        # Drop ids that are no longer present, so removing and re-adding a
        # torrent is announced again, and the file cannot grow without bound.
        self._save_seen(set(finished))
        return fresh

    def _announce(self, row: dict[str, Any]) -> None:
        text = (
            f"✅ Finished: {row['name']}\n"
            f"   {fmt_size(row['size'])}  •  now in complete/\n\n"
            f"Not yet tidied or sent to Jellyfin — that stays manual."
        )
        for chat_id in self.chat_ids:
            try:
                self.client.send_message(chat_id, text)
            except TelegramError as exc:
                log.warning("could not notify %s: %s", chat_id, exc)
        log.info("announced %s", row["name"])

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except DelugeError as exc:
                # Deluge restarting, or the container bouncing. Not fatal —
                # the next pass picks up whatever finished meanwhile.
                log.warning("could not reach Deluge: %s", exc)
            except Exception:
                log.exception("poll failed")
            time.sleep(self.interval)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="Single pass, then exit.")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS)
    args = parser.parse_args(argv)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    chat_ids = [int(p) for p in raw_ids.replace(",", " ").split() if p.strip("-").isdigit()]
    if not token or not chat_ids:
        print(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS must both be set "
            "(see .env.bot.example) — there is nobody to notify otherwise.",
            file=sys.stderr,
        )
        return 1

    notifier = CompletionNotifier(
        client=TelegramClient(token),
        chat_ids=chat_ids,
        config=load_config(os.environ.get("TORRENT_AGENT_CONFIG")),
        state_path=REPO_ROOT / "tmp" / "notified_torrents.json",
        interval=args.interval,
    )
    if args.once:
        announced = notifier.poll_once()
        print(f"announced {len(announced)} newly finished torrent(s)")
        return 0
    notifier.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
