"""ai_data_store.post_entry: the unattended layer's direct route to the store.

Best-effort throughout — a failed or unconfigured post must never raise, since
this runs after a real download has already landed.
"""

from __future__ import annotations

import json

from torrent_agent import ai_data_store


def test_posts_nothing_when_unconfigured(monkeypatch):
    monkeypatch.delenv("AI_DATA_STORE_URL", raising=False)
    monkeypatch.delenv("AI_DATA_STORE_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(
        ai_data_store.urllib.request, "urlopen",
        lambda *a, **k: calls.append(1),
    )
    ai_data_store.post_entry("torrent-agent", "Added - X", ["added"], {})
    assert calls == []


def test_posts_to_the_derived_entries_url(monkeypatch):
    monkeypatch.setenv("AI_DATA_STORE_URL", "http://host:1234/sse")
    monkeypatch.setenv("AI_DATA_STORE_TOKEN", "secret")

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(ai_data_store.urllib.request, "urlopen", fake_urlopen)

    ai_data_store.post_entry("torrent-agent", "Added - X", ["added"], {"k": "v"})

    assert captured["url"] == "http://host:1234/entries"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["source"] == "torrent-agent"
    assert captured["body"]["description"] == "Added - X"


def test_a_post_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("AI_DATA_STORE_URL", "http://host:1234/sse")
    monkeypatch.setenv("AI_DATA_STORE_TOKEN", "secret")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(ai_data_store.urllib.request, "urlopen", boom)
    ai_data_store.post_entry("torrent-agent", "Added - X", ["added"], {})  # must not raise
