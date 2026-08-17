"""add_torrent's pre-download malware gate.

Deluge has the file list before a single payload byte downloads (instantly
for a .torrent URL, since Deluge parses the file synchronously — the only
kind of link these tests exercise). add_torrent must refuse and clean up
after itself when that list contains an executable, and must not block
delivery when metadata never arrives, since a stalled magnet is not evidence
of anything and the post-download scan is the backstop.
"""

from __future__ import annotations

import contextlib

import pytest

from torrent_agent import deluge

CONFIG = {"deluge": {"host": "127.0.0.1", "port": 58846}}


class _Client:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def call(self, method, *args):
        self.calls.append((method, args))
        if method == "core.add_torrent_url":
            return "abc123"
        if method == "core.get_torrent_status":
            return {"files": self.files}
        if method == "core.remove_torrent":
            return True
        raise AssertionError(method)


def _wire(monkeypatch, client):
    @contextlib.contextmanager
    def fake_connect(config):
        yield client

    monkeypatch.setattr(deluge, "connect", fake_connect)


def test_a_clean_release_is_added_normally(monkeypatch):
    client = _Client([{"path": "Some.Show.S01E01.mkv"}])
    _wire(monkeypatch, client)

    tid = deluge.add_torrent("http://example/download", CONFIG)

    assert tid == "abc123"
    assert ("core.remove_torrent", ("abc123", True)) not in client.calls


def test_an_executable_is_blocked_and_removed(monkeypatch):
    client = _Client([{"path": "The.Guard.2011.1080p/setup.exe"}])
    _wire(monkeypatch, client)

    with pytest.raises(deluge.DelugeError) as exc:
        deluge.add_torrent("http://example/download", CONFIG)

    assert "setup.exe" in str(exc.value)
    assert ("core.remove_torrent", ("abc123", True)) in client.calls


def test_metadata_never_arriving_does_not_block_the_add(monkeypatch):
    # No files ever show up — a stalled magnet, not a verdict either way.
    client = _Client([])
    _wire(monkeypatch, client)
    monkeypatch.setattr(deluge, "_METADATA_TIMEOUT", 0.0)

    tid = deluge.add_torrent("http://example/download", CONFIG)

    assert tid == "abc123"
    assert ("core.remove_torrent", ("abc123", True)) not in client.calls
