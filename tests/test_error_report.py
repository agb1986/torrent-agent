"""error-report.json: append-only, never raises, valid JSON-lines."""

from __future__ import annotations

import json
from pathlib import Path

from torrent_agent import error_report


def test_record_error_appends_one_json_line(tmp_path, monkeypatch):
    path = tmp_path / "error-report.json"
    monkeypatch.setattr(error_report, "PATH", path)

    error_report.record_error("doctor", "vpn", "not bound to proton0")
    error_report.record_error("bot", "the bear", "DelugeError: could not connect")

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["component"] == "doctor"
    assert first["summary"] == "vpn"
    assert first["detail"] == "not bound to proton0"
    assert "ts" in first


def test_record_error_never_raises_when_the_path_is_unwritable(monkeypatch):
    monkeypatch.setattr(error_report, "PATH", Path("/nonexistent-dir/error-report.json"))
    error_report.record_error("doctor", "x", "y")  # must not raise
