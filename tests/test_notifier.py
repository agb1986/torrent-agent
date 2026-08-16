"""Completion notifications.

The rules that matter are about *not* sending: a restart must be silent, and
the first run after installing must not shout about torrents that finished
weeks ago. A notifier that cries wolf gets muted, at which point it may as
well not exist.
"""

import json

import pytest

from server.notifier import CompletionNotifier
from torrent_agent.deluge import DelugeError


class _FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _row(tid, name, finished, size=5_000_000_000.0):
    return {
        "id": tid,
        "name": name,
        "finished": finished,
        "state": "Seeding" if finished else "Downloading",
        "progress": 100.0 if finished else 42.0,
        "eta": 0,
        "rate": 0.0,
        "size": size,
        "seeds": 3,
        "peers": 5,
    }


@pytest.fixture
def notifier(tmp_path, monkeypatch):
    client = _FakeClient()

    def _make(rows):
        monkeypatch.setattr("server.notifier.list_torrents", lambda cfg: rows)
        return CompletionNotifier(
            client=client,
            chat_ids=[123],
            config={},
            state_path=tmp_path / "notified.json",
        )

    _make.client = client
    return _make


def test_first_run_adopts_existing_state_without_announcing(notifier):
    n = notifier([_row("a", "Old Thing", True), _row("b", "Also Old", True)])

    assert n.poll_once() == []
    assert notifier.client.sent == []
    # But they are now recorded, so they never announce later either.
    assert set(json.loads(n.state_path.read_text())) == {"a", "b"}


def test_announces_a_torrent_that_finishes(notifier, tmp_path):
    rows = [_row("a", "Toast S01", False)]
    n = notifier(rows)
    n.poll_once()                      # first run: adopt (nothing finished)

    rows[0] = _row("a", "Toast S01", True)
    fresh = n.poll_once()

    assert [r["id"] for r in fresh] == ["a"]
    assert len(notifier.client.sent) == 1
    chat_id, text = notifier.client.sent[0]
    assert chat_id == 123
    assert "Toast S01" in text and "Finished" in text


def test_does_not_announce_the_same_torrent_twice(notifier):
    rows = [_row("a", "Toast S01", False)]
    n = notifier(rows)
    n.poll_once()
    rows[0] = _row("a", "Toast S01", True)
    n.poll_once()

    assert n.poll_once() == []
    assert len(notifier.client.sent) == 1


def test_restart_is_silent(notifier, tmp_path):
    # A fresh process reads the same state file; a restart must not re-announce
    # everything that is sitting there finished.
    rows = [_row("a", "Toast S01", True)]
    n = notifier(rows)
    n.poll_once()                      # adopt

    second = CompletionNotifier(
        client=notifier.client, chat_ids=[123], config={},
        state_path=tmp_path / "notified.json",
    )
    assert second.poll_once() == []
    assert notifier.client.sent == []


def test_readding_a_removed_torrent_announces_again(notifier):
    rows = [_row("a", "Toast S01", True)]
    n = notifier(rows)
    n.poll_once()                      # adopt

    rows.clear()                       # removed from Deluge
    n.poll_once()
    rows.append(_row("a", "Toast S01", True))   # added back, already complete

    assert len(n.poll_once()) == 1


def test_notifies_every_allowed_chat(notifier, tmp_path):
    rows = [_row("a", "Toast S01", False)]
    n = notifier(rows)
    n.chat_ids = [123, 456]
    n.poll_once()
    rows[0] = _row("a", "Toast S01", True)
    n.poll_once()

    assert {c for c, _ in notifier.client.sent} == {123, 456}


def test_deluge_being_down_is_not_fatal(notifier, monkeypatch):
    n = notifier([])

    def boom(cfg):
        raise DelugeError("daemon down")

    monkeypatch.setattr("server.notifier.list_torrents", boom)
    with pytest.raises(DelugeError):
        n.poll_once()
    # run_forever swallows it; poll_once is honest about it. The distinction
    # matters: a caller testing one pass wants the error, the daemon does not
    # want to die of a container restart.
