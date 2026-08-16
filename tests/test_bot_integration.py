"""End-to-end: real HTTP, real threads, real subprocess.

The unit tests substitute the Telegram client and the runner. This one keeps
both: a local HTTP server stands in for api.telegram.org, and the runner
spawns an actual process. What it proves is the wiring the unit tests cannot —
that the client's request shape is one the server understands, that the poll
loop advances its offset, and that a message travels all the way from an
inbound update to an outbound reply.

The stand-in serves the same JSON envelope Telegram does ({"ok", "result"}),
so the client code under test is unmodified.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from server.bot import Bot
from server.runner import AgentRunner, DailyCap
from server.telegram import TelegramClient

TOKEN = "test-token"


class FakeTelegram(HTTPServer):
    """Serves queued updates once, then empty lists. Records sent messages."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.pending: list[dict] = []
        self.sent: list[dict] = []
        self.webhook_deleted = False

    @property
    def base(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep pytest output clean

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        method = self.path.rsplit("/", 1)[-1]
        server: FakeTelegram = self.server  # type: ignore[assignment]

        if method == "getUpdates":
            result, server.pending = server.pending, []
        elif method == "sendMessage":
            server.sent.append(body)
            result = {"message_id": len(server.sent)}
        elif method == "deleteWebhook":
            server.webhook_deleted = True
            result = True
        else:
            result = None

        payload = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def telegram():
    server = FakeTelegram()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def update(uid, chat_id, text):
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat_id},
            "from": {"username": "tester", "id": chat_id},
            "text": text,
        },
    }


class StubAgent(AgentRunner):
    """A runner whose 'agent' is a python snippet writing a real artifact."""

    def __init__(self, artifact_path, **kwargs):
        super().__init__(**kwargs)
        self.artifact_path = artifact_path

    def _command(self, request):
        script = (
            "import json,sys\n"
            f"p = {str(self.artifact_path)!r}\n"
            "json.dump({'request': sys.argv[1], 'added': ["
            "{'title': 'The Bear S03 1080p', 'torrent_id': 'cafe1234'}]},"
            " open(p,'w'))\n"
            "print(p)\n"
            "print('Added: The Bear S03 1080p')\n"
        )
        return [sys.executable, "-c", script, request]


def build(telegram, tmp_path, allowed=(42,)):
    client = TelegramClient(TOKEN, api_base=telegram.base)
    runner = StubAgent(
        artifact_path=tmp_path / "added_bear.json",
        cap=DailyCap(10, tmp_path / "cap.json"),
        repo_root=tmp_path,
        timeout=30,
    )
    bot = Bot(client, runner, set(allowed), tmp_path / "audit.jsonl")
    return bot


def test_full_round_trip_from_update_to_reply(telegram, tmp_path):
    bot = build(telegram, tmp_path)
    telegram.pending = [update(1, 42, "/get the bear s03")]

    assert bot.poll_once() == 1
    # Offset must advance, or the same update is served forever.
    assert bot._offset == 2

    job = bot.jobs.get_nowait()
    bot.work_once(job)

    replies = [m["text"] for m in telegram.sent]
    assert any("Looking for: the bear s03" in r for r in replies)
    assert any("Added: The Bear S03 1080p" in r for r in replies)
    # The artifact path is plumbing; it must not reach the chat.
    assert not any("added_bear.json" in r for r in replies)

    audit = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    completed = [e for e in audit if e["event"] == "completed"]
    assert completed and completed[0]["torrent_ids"] == ["cafe1234"]


def test_stranger_is_refused_over_real_http(telegram, tmp_path):
    bot = build(telegram, tmp_path, allowed=(42,))
    telegram.pending = [update(1, 9999, "/get something")]

    bot.poll_once()

    assert bot.jobs.qsize() == 0
    assert [m["text"] for m in telegram.sent] == ["Not authorised."]
    assert telegram.sent[0]["chat_id"] == 9999


def test_poll_handles_a_batch_and_keeps_the_last_offset(telegram, tmp_path):
    bot = build(telegram, tmp_path)
    telegram.pending = [
        update(7, 42, "/status"),
        update(8, 42, "/help"),
        update(9, 9999, "/get nope"),
    ]

    assert bot.poll_once() == 3
    assert bot._offset == 10
    texts = [m["text"] for m in telegram.sent]
    assert "Idle." in texts[0]
    assert "/get <title>" in texts[1]
    assert texts[2] == "Not authorised."


def test_long_message_is_truncated_rather_than_dropped(telegram, tmp_path):
    # Telegram rejects >4096 chars outright, which would look like the bot
    # silently ignoring a successful run.
    bot = build(telegram, tmp_path)
    bot.say(42, "x" * 9000)
    sent = telegram.sent[0]["text"]
    assert len(sent) < 4096
    assert sent.endswith("[truncated]")


def test_delete_webhook_is_called_before_polling(telegram, tmp_path):
    bot = build(telegram, tmp_path)
    bot.client.delete_webhook()
    assert telegram.webhook_deleted is True
