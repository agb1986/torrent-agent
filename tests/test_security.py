"""The two malware gates: file-list check before download, clamscan after.

`flag_dangerous_files` is pure and cheap, so it gets exercised directly.
`clamav_scan` shells out, so these tests fake the `clamscan` binary rather
than requiring it installed — the real binary is exercised manually, per
CLAUDE.md's "sanity-check against the real thing" convention for anything
that hits an external dependency.
"""

from __future__ import annotations

import stat

import pytest

from torrent_agent import security


# --- flag_dangerous_files --------------------------------------------------


def test_a_clean_release_is_not_flagged():
    files = [{"path": "Some.Show.S01E01.mkv"}, {"path": "Some.Show.S01E01.srt"}]
    assert security.flag_dangerous_files(files) == []


def test_an_executable_is_flagged():
    files = [{"path": "The.Guard.2011.1080p/setup.exe"}]
    assert security.flag_dangerous_files(files) == ["The.Guard.2011.1080p/setup.exe"]


def test_bytes_paths_are_decoded():
    files = [{b"path": b"payload.scr"}]
    assert security.flag_dangerous_files(files) == ["payload.scr"]


def test_the_check_is_case_insensitive():
    files = [{"path": "Movie.EXE"}]
    assert security.flag_dangerous_files(files) == ["Movie.EXE"]


def test_only_the_dangerous_ones_come_back():
    files = [
        {"path": "movie.mkv"},
        {"path": "readme.txt"},
        {"path": "crack.exe"},
    ]
    assert security.flag_dangerous_files(files) == ["crack.exe"]


# --- clamav_scan ------------------------------------------------------------


def _fake_clamscan(tmp_path, script: str):
    """Put a fake `clamscan` on PATH and point shutil.which at it."""
    binary = tmp_path / "clamscan"
    binary.write_text(f"#!/bin/sh\n{script}\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def test_missing_clamscan_raises_unavailable(monkeypatch):
    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    with pytest.raises(security.ClamAVUnavailable):
        security.clamav_scan("/tmp/whatever")


def test_a_clean_scan_reports_nothing(tmp_path, monkeypatch):
    binary = _fake_clamscan(tmp_path, "exit 0")
    monkeypatch.setattr(security.shutil, "which", lambda name: str(binary))
    assert security.clamav_scan(tmp_path) == []


def test_an_infected_file_is_reported(tmp_path, monkeypatch):
    binary = _fake_clamscan(
        tmp_path,
        'echo "/mnt/data/downloads/payload.exe: Win.Trojan.Generic FOUND"; exit 1',
    )
    monkeypatch.setattr(security.shutil, "which", lambda name: str(binary))
    assert security.clamav_scan(tmp_path) == ["/mnt/data/downloads/payload.exe"]


def test_a_scan_error_raises_rather_than_reporting_clean(tmp_path, monkeypatch):
    binary = _fake_clamscan(tmp_path, "echo boom 1>&2; exit 2")
    monkeypatch.setattr(security.shutil, "which", lambda name: str(binary))
    with pytest.raises(RuntimeError):
        security.clamav_scan(tmp_path)
