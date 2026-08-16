"""Telegram control bot: search and add, nothing else.

Shape of the thing:

  poll loop (main thread)  ->  allowlist  ->  queue  ->  one worker thread

The poll loop never runs the agent itself, so /status and /cancel still answer
while a fetch is in flight. The worker is single — two concurrent agent runs
would race on the same Deluge session and the same tmp/ artifacts, which is an
avoidable class of bug rather than a throughput problem worth solving.

Run it:  python -m server.bot
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import REPO_ROOT, AgentRunner, DailyCap
from .telegram import TelegramClient, TelegramError

log = logging.getLogger("server.bot")

USAGE = (
    "Commands:\n"
    "  /get <title>  — find a torrent and add it to Deluge\n"
    "  /status       — what the bot is doing right now\n"
    "  /cancel       — stop the current run and clear the queue"
)


@dataclass
class Job:
    chat_id: int
    user: str
    request: str


# Telegram renders in a proportional font, so column alignment is wasted
# effort — two short lines per torrent read better on a phone than a table
# that wraps.
MAX_LISTED = 15


def _format_torrents(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Deluge is empty."

    from torrent_agent.deluge import fmt_eta, fmt_size

    active = sum(1 for r in rows if r["state"] == "Downloading")
    head = f"Deluge — {len(rows)} torrent(s), {active} downloading"

    out = [head, ""]
    for r in rows[:MAX_LISTED]:
        marker = "⬇" if r["state"] == "Downloading" else "•"
        out.append(f"{marker} {r['name'][:60]}")
        bits = [f"{r['progress']:.1f}%", r["state"]]
        if r["state"] == "Downloading":
            bits.append(f"{fmt_size(r['rate'])}/s")
            bits.append(f"ETA {fmt_eta(r['eta'])}")
        bits.append(fmt_size(r["size"]))
        bits.append(f"{r['seeds']} seeds")
        out.append("   " + "  ".join(bits))
    if len(rows) > MAX_LISTED:
        out.append(f"\n…and {len(rows) - MAX_LISTED} more.")
    return "\n".join(out)


def _format_added(added: list[dict[str, Any]]) -> str:
    """Confirmation built from the run's artifact, not the model's prose.

    The summary is model-authored and will drift; these fields come from what
    add_torrent actually recorded, so "added" here means it reached Deluge.
    """
    if not added:
        return ""
    out = [f"\nAdded to Deluge ({len(added)}):"]
    for a in added:
        title = str(a.get("title", "?"))[:60]
        bits = []
        if a.get("size_gb"):
            bits.append(f"{a['size_gb']} GB")
        if a.get("seeders") is not None:
            bits.append(f"{a['seeders']} seeds")
        if a.get("resolution"):
            bits.append(str(a["resolution"]))
        out.append(f"• {title}")
        if bits:
            out.append("   " + "  ".join(bits))
    return "\n".join(out)


class Bot:
    def __init__(
        self,
        client: TelegramClient,
        runner: AgentRunner,
        allowed_chat_ids: set[int],
        log_path: Path,
    ) -> None:
        self.client = client
        self.runner = runner
        self.allowed = set(allowed_chat_ids)
        self.log_path = Path(log_path)
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.current: Job | None = None
        self._offset: int | None = None
        self._stop = threading.Event()

    # --- durable log ------------------------------------------------------
    def record(self, event: str, **fields: Any) -> None:
        """Append one JSON line. Who, what, which torrent, what happened.

        Appended rather than rewritten, and flushed per line, so a kill -9
        leaves every completed request on disk.
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.warning("could not write audit log: %s", exc)
        log.info("%s %s", event, fields)

    def say(self, chat_id: int, text: str) -> None:
        try:
            self.client.send_message(chat_id, text)
        except TelegramError as exc:
            log.warning("send failed: %s", exc)

    # --- update handling --------------------------------------------------
    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        who = (message.get("from") or {}).get("username") or str(
            (message.get("from") or {}).get("id", "?")
        )
        if chat_id is None or not text:
            return

        # The allowlist is the first thing that happens to any message, before
        # parsing and before anything can be spawned. A stranger who finds the
        # bot token's handle gets a refusal and an audit line, nothing else.
        if chat_id not in self.allowed:
            self.record("refused", chat_id=chat_id, user=who, text=text[:120])
            self.say(chat_id, "Not authorised.")
            return

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()   # /get@BotName in groups
        argument = argument.strip()

        if command == "/get":
            self.cmd_get(chat_id, who, argument)
        elif command == "/status":
            self.cmd_status(chat_id)
        elif command == "/cancel":
            self.cmd_cancel(chat_id, who)
        elif command in ("/start", "/help"):
            self.say(chat_id, USAGE)
        else:
            self.say(chat_id, f"Unknown command.\n\n{USAGE}")

    def cmd_get(self, chat_id: int, who: str, request: str) -> None:
        if not request:
            self.say(chat_id, "What should I fetch?\ne.g. /get the bear season 3")
            return
        remaining = self.runner.cap.remaining()
        if remaining <= 0:
            self.record("cap_refused", chat_id=chat_id, user=who, request=request)
            self.say(
                chat_id,
                f"Daily limit of {self.runner.cap.limit} requests already reached. "
                f"Nothing sent to the API.",
            )
            return

        self.jobs.put(Job(chat_id=chat_id, user=who, request=request))
        self.record("queued", chat_id=chat_id, user=who, request=request)
        depth = self.jobs.qsize()
        if self.current is not None or depth > 1:
            self.say(chat_id, f"Queued — {depth} ahead of it. Working through them.")
        else:
            self.say(chat_id, f"Looking for: {request}")

    def cmd_status(self, chat_id: int) -> None:
        lines = []
        if self.current is None:
            lines.append("Idle.")
        else:
            lines.append(f"Working on: {self.current.request}")
        pending = self.jobs.qsize()
        if pending:
            lines.append(f"Queued: {pending}")
        lines.append(f"Requests left today: {self.runner.cap.remaining()}")

        # What Deluge is actually doing matters more than what the bot is, and
        # is the question people mean when they ask. Failure here must not take
        # the whole reply down — the bot's own state is still worth reporting.
        try:
            rows = self.runner.torrents()
        except Exception as exc:
            lines.append(f"\nCould not reach Deluge: {exc}")
        else:
            lines.append("")
            lines.append(_format_torrents(rows))
        self.say(chat_id, "\n".join(lines))

    def cmd_cancel(self, chat_id: int, who: str) -> None:
        dropped = 0
        while True:
            try:
                self.jobs.get_nowait()
                self.jobs.task_done()
                dropped += 1
            except queue.Empty:
                break
        killed = self.runner.cancel()
        self.record("cancelled", chat_id=chat_id, user=who, dropped=dropped,
                    killed_running=killed)
        if killed or dropped:
            self.say(
                chat_id,
                f"Stopped. {'Killed the running job. ' if killed else ''}"
                f"Dropped {dropped} queued.",
            )
        else:
            self.say(chat_id, "Nothing to cancel.")

    # --- worker -----------------------------------------------------------
    def work_once(self, job: Job) -> None:
        self.current = job
        try:
            result = self.runner.run(job.request)
            self.record(
                "completed" if result.ok else "failed",
                chat_id=job.chat_id,
                user=job.user,
                request=job.request,
                torrent_ids=result.torrent_ids,
                artifact=result.artifact,
            )
            self.say(job.chat_id, result.summary + _format_added(result.added))
        except Exception as exc:  # never let one bad run kill the worker
            log.exception("run blew up")
            self.record("error", chat_id=job.chat_id, user=job.user,
                        request=job.request, error=str(exc))
            self.say(job.chat_id, f"Something went wrong: {exc}")
        finally:
            # Released in finally so a crash cannot wedge the queue — a bot
            # that looks alive but never accepts another job is the worst
            # failure shape here.
            self.current = None

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.work_once(job)
            finally:
                self.jobs.task_done()

    # --- main loop --------------------------------------------------------
    def poll_once(self) -> int:
        updates = self.client.get_updates(self._offset)
        for update in updates:
            self._offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception:
                log.exception("update handling failed")
        return len(updates)

    def run_forever(self) -> None:
        worker = threading.Thread(target=self._worker, name="agent-worker", daemon=True)
        worker.start()
        self.record("started", allowed=sorted(self.allowed))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except TelegramError as exc:
                log.warning("poll failed, backing off: %s", exc)
                time.sleep(10)

    def stop(self) -> None:
        self._stop.set()


def _allowed_ids(raw: str) -> set[int]:
    out = set()
    for part in raw.replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            log.warning("ignoring non-numeric chat id %r", part)
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = _allowed_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set (see .env.bot.example)", file=sys.stderr)
        return 1
    if not allowed:
        # Refusing to start beats starting open to the world. An empty
        # allowlist is almost always a misconfiguration, not an intent.
        print(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty — refusing to start a bot "
            "anyone could drive. Set it to your numeric chat id.",
            file=sys.stderr,
        )
        return 1

    cap = DailyCap(
        limit=int(os.environ.get("TELEGRAM_DAILY_LIMIT", "20")),
        state_path=REPO_ROOT / "tmp" / "bot_daily_count.json",
    )
    runner = AgentRunner(cap=cap, config_path=os.environ.get("TORRENT_AGENT_CONFIG"))
    bot = Bot(
        client=TelegramClient(token),
        runner=runner,
        allowed_chat_ids=allowed,
        log_path=REPO_ROOT / "tmp" / "bot_audit.jsonl",
    )
    bot.client.delete_webhook()
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        bot.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
