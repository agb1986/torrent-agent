"""Telegram bot: authorisation, serialisation, and the spend cap.

These are the properties that are expensive to get wrong exactly once — an
open bot, a runaway API bill, or a queue that silently wedges. Everything here
runs offline against fakes; nothing touches Telegram or spawns the agent.
"""

import json
import threading
import time

import pytest

from server.bot import Bot, Job
from server.runner import DailyCap, RunResult


class FakeClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    def texts_for(self, chat_id):
        return [t for c, t in self.sent if c == chat_id]


class FakeRunner:
    """Stands in for AgentRunner. Records every invocation."""

    def __init__(self, cap, result=None, block=None):
        self.cap = cap
        self.calls: list[str] = []
        self.result = result or RunResult(ok=True, summary="Added: something")
        self.block = block          # optional Event to hold the run open
        self.started = threading.Event()
        self.cancelled = False

    def run(self, request):
        self.calls.append(request)
        self.started.set()
        if self.block is not None:
            self.block.wait(timeout=5)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def cancel(self):
        self.cancelled = True
        return True


def make_bot(tmp_path, allowed=(42,), runner=None, limit=10):
    cap = DailyCap(limit=limit, state_path=tmp_path / "cap.json")
    runner = runner or FakeRunner(cap)
    client = FakeClient()
    bot = Bot(
        client=client,
        runner=runner,
        allowed_chat_ids=set(allowed),
        log_path=tmp_path / "audit.jsonl",
    )
    return bot, client, runner


def message(chat_id, text, user="someone", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"username": user, "id": chat_id},
            "text": text,
        },
    }


def audit(bot):
    path = bot.log_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# --- authorisation --------------------------------------------------------

def test_unauthorised_chat_is_refused_logged_and_never_spawns_the_agent(tmp_path):
    bot, client, runner = make_bot(tmp_path, allowed=(42,))
    bot.handle_update(message(9999, "/get something expensive", user="stranger"))

    assert runner.calls == [], "the agent must not run for an unknown chat id"
    assert client.texts_for(9999) == ["Not authorised."]
    events = audit(bot)
    assert [e["event"] for e in events] == ["refused"]
    assert events[0]["chat_id"] == 9999 and events[0]["user"] == "stranger"


def test_unauthorised_chat_cannot_consume_the_daily_cap(tmp_path):
    # Otherwise a stranger who cannot use the bot can still deny it to you.
    bot, _, runner = make_bot(tmp_path, allowed=(42,), limit=3)
    for i in range(5):
        bot.handle_update(message(9999, "/get x", update_id=i))
    assert runner.cap.remaining() == 3


def test_authorised_chat_is_accepted(tmp_path):
    bot, client, _ = make_bot(tmp_path, allowed=(42,))
    bot.handle_update(message(42, "/get the bear s03"))
    assert bot.jobs.qsize() == 1
    assert "Looking for: the bear s03" in client.texts_for(42)[0]


# --- command surface ------------------------------------------------------

def test_unknown_command_gets_usage_not_a_search(tmp_path):
    bot, client, _ = make_bot(tmp_path)
    bot.handle_update(message(42, "/destroy everything"))
    assert bot.jobs.qsize() == 0
    assert "Unknown command" in client.texts_for(42)[0]


def test_bare_get_asks_for_a_title(tmp_path):
    bot, client, _ = make_bot(tmp_path)
    bot.handle_update(message(42, "/get"))
    assert bot.jobs.qsize() == 0
    assert "What should I fetch?" in client.texts_for(42)[0]


def test_group_style_command_suffix_is_stripped(tmp_path):
    # In a group chat Telegram delivers "/get@MyBot the bear".
    bot, _, _ = make_bot(tmp_path)
    bot.handle_update(message(42, "/get@TorrentAgentBot the bear"))
    assert bot.jobs.qsize() == 1


def test_non_text_updates_are_ignored(tmp_path):
    bot, client, _ = make_bot(tmp_path)
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 42}}})
    assert client.sent == []


# --- serialisation --------------------------------------------------------

def test_two_rapid_gets_run_one_at_a_time(tmp_path):
    gate = threading.Event()
    cap = DailyCap(limit=10, state_path=tmp_path / "cap.json")
    runner = FakeRunner(cap, block=gate)
    bot, client, _ = make_bot(tmp_path, runner=runner)

    worker = threading.Thread(target=bot._worker, daemon=True)
    worker.start()
    try:
        bot.handle_update(message(42, "/get first", update_id=1))
        assert runner.started.wait(timeout=3), "worker never picked up the first job"

        bot.handle_update(message(42, "/get second", update_id=2))
        time.sleep(0.2)
        # The second must still be waiting while the first is held open.
        assert runner.calls == ["first"]
        assert "Queued" in client.texts_for(42)[-1]

        gate.set()
        deadline = time.time() + 5
        while runner.calls != ["first", "second"] and time.time() < deadline:
            time.sleep(0.05)
        assert runner.calls == ["first", "second"]
    finally:
        gate.set()
        bot.stop()


def test_worker_survives_a_run_that_raises(tmp_path):
    # A wedged queue looks identical to a dead bot, so the slot must be
    # released even when the run explodes.
    cap = DailyCap(limit=10, state_path=tmp_path / "cap.json")
    runner = FakeRunner(cap, result=RuntimeError("boom"))
    bot, client, _ = make_bot(tmp_path, runner=runner)

    bot.work_once(Job(chat_id=42, user="me", request="thing"))

    assert bot.current is None, "queue slot must be released after a failure"
    assert "Something went wrong" in client.texts_for(42)[-1]
    assert [e["event"] for e in audit(bot)] == ["error"]


def test_failed_run_is_reported_and_logged(tmp_path):
    cap = DailyCap(limit=10, state_path=tmp_path / "cap.json")
    runner = FakeRunner(cap, result=RunResult(ok=False, summary="Agent failed: nope"))
    bot, client, _ = make_bot(tmp_path, runner=runner)

    bot.work_once(Job(chat_id=42, user="me", request="thing"))

    assert "Agent failed" in client.texts_for(42)[-1]
    assert [e["event"] for e in audit(bot)] == ["failed"]


def test_completion_logs_the_torrent_id(tmp_path):
    cap = DailyCap(limit=10, state_path=tmp_path / "cap.json")
    result = RunResult(
        ok=True,
        summary="Added: The Bear S03",
        added=[{"title": "The Bear S03", "torrent_id": "abc123"}],
        artifact="/tmp/added_x.json",
    )
    bot, _, _ = make_bot(tmp_path, runner=FakeRunner(cap, result=result))

    bot.work_once(Job(chat_id=42, user="me", request="the bear"))

    entry = audit(bot)[-1]
    assert entry["event"] == "completed"
    assert entry["torrent_ids"] == ["abc123"]


# --- cancel ---------------------------------------------------------------

def test_cancel_clears_the_queue_and_kills_the_run(tmp_path):
    bot, client, runner = make_bot(tmp_path)
    bot.handle_update(message(42, "/get one", update_id=1))
    bot.handle_update(message(42, "/get two", update_id=2))
    assert bot.jobs.qsize() == 2

    bot.handle_update(message(42, "/cancel", update_id=3))

    assert bot.jobs.qsize() == 0
    assert runner.cancelled is True
    assert [e["event"] for e in audit(bot)][-1] == "cancelled"


# --- spend cap ------------------------------------------------------------

def test_cap_refuses_before_anything_is_queued(tmp_path):
    bot, client, runner = make_bot(tmp_path, limit=2)
    runner.cap.claim()
    runner.cap.claim()

    bot.handle_update(message(42, "/get expensive"))

    assert bot.jobs.qsize() == 0, "must refuse before queueing, not after"
    assert runner.calls == []
    assert "Daily limit" in client.texts_for(42)[-1]
    assert [e["event"] for e in audit(bot)] == ["cap_refused"]


def test_cap_persists_across_restart(tmp_path):
    state = tmp_path / "cap.json"
    first = DailyCap(limit=3, state_path=state)
    assert first.claim() and first.claim()
    # A crash-loop must not hand itself a fresh budget on every restart.
    second = DailyCap(limit=3, state_path=state)
    assert second.remaining() == 1


def test_cap_resets_on_a_new_day(tmp_path):
    cap = DailyCap(limit=2, state_path=tmp_path / "cap.json")
    assert cap.claim(today="2026-08-15")
    assert cap.claim(today="2026-08-15")
    assert not cap.claim(today="2026-08-15")
    assert cap.claim(today="2026-08-16")


def test_zero_limit_refuses_everything(tmp_path):
    cap = DailyCap(limit=0, state_path=tmp_path / "cap.json")
    assert not cap.claim()


def test_cap_is_atomic_under_concurrent_claims(tmp_path):
    # Two threads must not both win the last slot.
    cap = DailyCap(limit=50, state_path=tmp_path / "cap.json")
    wins = []
    lock = threading.Lock()

    def grab():
        for _ in range(25):
            got = cap.claim()
            with lock:
                wins.append(got)

    threads = [threading.Thread(target=grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(wins) == 50, "cap must hand out exactly its limit, no more"


# --- status ---------------------------------------------------------------

def test_status_reports_idle_and_remaining_budget(tmp_path):
    bot, client, _ = make_bot(tmp_path, limit=7)
    bot.handle_update(message(42, "/status"))
    text = client.texts_for(42)[-1]
    assert "Idle." in text and "Requests left today: 7" in text


# --- reply formatting -----------------------------------------------------


def test_status_lists_torrents_with_progress():
    from server.bot import _format_torrents

    out = _format_torrents(
        [
            {"name": "Toast S01", "state": "Downloading", "progress": 21.7,
             "eta": 873, "rate": 5138022.0, "size": 5798205849.0,
             "seeds": 32, "peers": 40},
            {"name": "debian.iso", "state": "Seeding", "progress": 100.0,
             "eta": 0, "rate": 0.0, "size": 791674880.0, "seeds": 0, "peers": 2},
        ]
    )
    assert "2 torrent(s), 1 downloading" in out
    assert "21.7%" in out and "Toast S01" in out
    assert "32 seeds" in out
    # ETA and rate only make sense for something actually downloading.
    assert "ETA 14m33s" in out
    assert out.count("ETA") == 1


def test_status_handles_an_empty_session():
    from server.bot import _format_torrents

    assert _format_torrents([]) == "Deluge is empty."


def test_status_truncates_a_long_list():
    from server.bot import MAX_LISTED, _format_torrents

    rows = [
        {"name": f"t{i}", "state": "Seeding", "progress": 100.0, "eta": 0,
         "rate": 0.0, "size": 1.0, "seeds": 1, "peers": 1}
        for i in range(MAX_LISTED + 4)
    ]
    out = _format_torrents(rows)
    assert "and 4 more" in out
    # Telegram hard-rejects over 4096 characters; a long session must not
    # turn a status reply into a silent delivery failure.
    assert len(out) < 4000


def test_added_confirmation_reports_size_and_seeds():
    from server.bot import _format_added

    out = _format_added(
        [{"title": "Toast of London S01", "size_gb": 5.38, "seeders": 32,
          "resolution": "1080p"}]
    )
    assert "Added to Deluge (1)" in out
    assert "5.38 GB" in out and "32 seeds" in out and "1080p" in out


def test_added_confirmation_is_empty_when_nothing_was_added():
    # A run that adds nothing must not append a misleading empty header.
    from server.bot import _format_added

    assert _format_added([]) == ""
