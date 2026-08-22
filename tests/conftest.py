"""Make the helper scripts importable — scripts/ is not a package."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture(autouse=True)
def _isolate_error_report(tmp_path, monkeypatch):
    """Never let a test write to the real repo-root error-report.json.

    Several code paths (bot/notifier/sub/pfsync/doctor) call
    torrent_agent.error_report.record_error on a failure, and tests that
    exercise those failure paths would otherwise append real lines to this
    checkout's error-report.json every run.
    """
    from torrent_agent import error_report

    monkeypatch.setattr(error_report, "PATH", tmp_path / "error-report.json")
