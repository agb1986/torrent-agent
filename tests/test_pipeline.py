"""The unattended path: finished torrent -> tidied -> delivered -> Jellyfin.

The ordering is load-bearing (Deluge first, or tidying breaks a live torrent),
and so is the refusal: an unconfident plan must leave the download untouched
rather than file it somewhere plausible.
"""

from __future__ import annotations

import contextlib

import pytest

from server import pipeline
from torrent_agent.tidy import Move, TidyPlan

CONFIG = {
    "deluge": {"host": "127.0.0.1", "port": 58846},
    "server": {"destinations": {"tv": "/mnt/data/tv", "film": "/mnt/data/film"}},
}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub Deluge, tidy and transfer; record what each was asked to do."""
    calls = {"removed": [], "executed": [], "delivered": [], "scanned": []}

    class _Client:
        def call(self, method, *args):
            if method == "core.remove_torrent":
                calls["removed"].append(args[0])
                return True
            raise AssertionError(method)

    @contextlib.contextmanager
    def fake_connect(config):
        yield _Client()

    monkeypatch.setattr(pipeline.deluge, "connect", fake_connect)

    import transfer

    monkeypatch.setattr(transfer, "server_is_local", lambda *a, **k: True)
    monkeypatch.setattr(
        transfer, "transfer_local",
        lambda src, dest: calls["delivered"].append((src, dest)) or 0,
    )
    monkeypatch.setattr(
        transfer, "scan_jellyfin", lambda path: calls["scanned"].append(path)
    )
    monkeypatch.setattr(pipeline, "execute", lambda plan: calls["executed"].append(plan))

    source = tmp_path / "downloads" / "Some.Show.S01"
    source.mkdir(parents=True)
    calls["source"] = source
    calls["torrent"] = {
        "id": "abc123",
        "name": "Some.Show.S01",
        "save_path": str(tmp_path / "downloads"),
        "size": 1.0,
    }
    return calls


def _confident_plan(tmp_path):
    root = tmp_path / "Some Show (2010) [tmdbid-99]"
    return TidyPlan(
        kind="tv", name="Some Show", year=2010, tmdb_id="99", root=root,
        moves=[Move(source=tmp_path / "a.mkv", target=root / "Season 01/S01E01 - A.mkv")],
    )


def test_happy_path_runs_every_stage_in_order(wired, monkeypatch, tmp_path):
    plan = _confident_plan(tmp_path)
    monkeypatch.setattr(pipeline, "plan_for", lambda src: plan)

    outcome = pipeline.run(wired["torrent"], CONFIG)

    assert outcome.ok, outcome.message
    assert wired["removed"] == ["abc123"]          # Deluge first
    assert wired["executed"] == [plan]
    assert wired["delivered"] == [(str(plan.root), "/mnt/data/tv")]
    assert wired["scanned"] == ["/mnt/data/tv/Some Show (2010) [tmdbid-99]"]


def test_an_unconfident_plan_changes_nothing(wired, monkeypatch):
    plan = TidyPlan(kind="tv", problems=["ambiguous TMDB match — A (1974), B (2004)"])
    monkeypatch.setattr(pipeline, "plan_for", lambda src: plan)

    outcome = pipeline.run(wired["torrent"], CONFIG)

    assert not outcome.ok
    assert outcome.stage == "tidy"
    assert wired["executed"] == []
    assert wired["delivered"] == []
    assert "ambiguous" in outcome.details[0]


def test_films_go_to_the_film_destination(wired, monkeypatch, tmp_path):
    plan = _confident_plan(tmp_path)
    plan.kind = "film"
    monkeypatch.setattr(pipeline, "plan_for", lambda src: plan)

    pipeline.run(wired["torrent"], CONFIG)

    assert wired["delivered"][0][1] == "/mnt/data/film"


def test_a_missing_download_stops_before_touching_deluge(wired, monkeypatch):
    wired["torrent"]["name"] = "not-on-disk"

    outcome = pipeline.run(wired["torrent"], CONFIG)

    assert not outcome.ok
    assert outcome.stage == "locate"
    assert wired["removed"] == [], "must not remove a torrent whose files are missing"


def test_a_failed_delivery_is_reported_not_swallowed(wired, monkeypatch, tmp_path):
    import transfer

    plan = _confident_plan(tmp_path)
    monkeypatch.setattr(pipeline, "plan_for", lambda src: plan)
    monkeypatch.setattr(transfer, "transfer_local", lambda src, dest: 1)

    outcome = pipeline.run(wired["torrent"], CONFIG)

    assert not outcome.ok
    assert outcome.stage == "deliver"
    assert wired["scanned"] == [], "must not tell Jellyfin about a failed delivery"


def test_a_jellyfin_failure_does_not_fail_a_delivered_file(wired, monkeypatch, tmp_path):
    import transfer

    plan = _confident_plan(tmp_path)
    monkeypatch.setattr(pipeline, "plan_for", lambda src: plan)

    def boom(path):
        raise OSError("jellyfin down")

    monkeypatch.setattr(transfer, "scan_jellyfin", boom)

    outcome = pipeline.run(wired["torrent"], CONFIG)

    # The files are on the server; a missed scan is a nuisance, not a loss.
    assert outcome.ok


def test_the_message_says_nothing_moved_when_it_refuses():
    plan = TidyPlan(kind="tv", problems=["TVmaze has no S01E09"])
    text = pipeline.format_outcome(
        pipeline.Outcome(False, "tidy", "Not sure how to file it.", plan=plan,
                         details=plan.problems)
    )
    assert "Left where it is" in text
    assert "S01E09" in text


# --- container vs host paths ----------------------------------------------


def test_container_paths_are_translated_to_host_paths():
    """Deluge answers with its own view of the filesystem.

    It runs in a container and reports /downloads, which does not exist on the
    host, where the same files live at /mnt/data/downloads. Found the hard way:
    the pipeline refused a real download because the path it was handed did not
    exist — correctly, but for a reason no test had covered.
    """
    cfg = {"deluge": {"path_map": {"/downloads": "/mnt/data/downloads"}}}
    assert pipeline.host_path("/downloads/Show.S01", cfg) == "/mnt/data/downloads/Show.S01"
    assert pipeline.host_path("/downloads", cfg) == "/mnt/data/downloads"


def test_longest_prefix_wins():
    cfg = {
        "deluge": {
            "path_map": {
                "/downloads": "/mnt/data/downloads",
                "/downloads/complete": "/mnt/fast/complete",
            }
        }
    }
    assert pipeline.host_path("/downloads/complete/X", cfg) == "/mnt/fast/complete/X"


def test_unmapped_paths_pass_through_untouched():
    # A native deluged already reports host paths; it should need no config.
    assert pipeline.host_path("/home/me/Downloads/X", {"deluge": {}}) == "/home/me/Downloads/X"


def test_a_failed_jellyfin_scan_is_not_reported_as_notified(wired, monkeypatch, tmp_path):
    """The summary must not claim a scan that did not happen.

    scan_jellyfin catches its own errors and prints a [WARN] rather than
    raising, so "no exception" is not evidence of success — the first real run
    delivered correctly and cheerfully reported "Jellyfin notified" after two
    HTTP 500s.
    """
    import transfer

    plan = _confident_plan(tmp_path)
    monkeypatch.setattr(pipeline, "plan_for", lambda src: plan)
    monkeypatch.setattr(
        transfer, "scan_jellyfin",
        lambda path: print("[WARN] Jellyfin scan failed (500) — transfer itself was fine"),
    )

    outcome = pipeline.run(wired["torrent"], CONFIG)

    assert outcome.ok                    # the files did land
    assert outcome.jellyfin_ok is False
    assert "did NOT pick it up" in pipeline.format_outcome(outcome)
