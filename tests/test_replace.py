"""`replace`: picking lost causes, removing them, and building the re-fetch.

The stall rule is deliberately conservative — no seeds AND no meaningful rate
AND past a grace period AND not already near done — since a single snapshot
of Deluge's state can't tell "just started" from "long dead" any other way.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from torrent_agent import replace

CONFIG = {"replace": {"stall_minutes": 30, "rate_threshold_bytes": 1024,
                       "near_complete_progress": 95.0}}


def _row(**over):
    row = {
        "id": "abc123",
        "name": "Some.Show.S01E01.1080p.WEB.h264-GRP",
        "finished": False,
        "save_path": "/downloads",
        "state": "Downloading",
        "progress": 12.0,
        "eta": 0,
        "rate": 0.0,
        "size": 1_000_000_000.0,
        "seeds": 0,
        "peers": 0,
        "time_added": int(time.time()) - 3600,  # an hour old
    }
    row.update(over)
    return row


# --- find_lost_causes -------------------------------------------------------


def test_a_stalled_seederless_torrent_is_a_candidate(monkeypatch):
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [_row()])
    out = replace.find_lost_causes(CONFIG)
    assert len(out) == 1
    assert out[0]["name"] == "Some.Show.S01E01.1080p.WEB.h264-GRP"
    assert out[0]["age_minutes"] == pytest.approx(60.0, abs=1.0)


def test_a_fresh_torrent_within_the_grace_period_is_left_alone(monkeypatch):
    row = _row(time_added=int(time.time()) - 60)  # one minute old
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_a_torrent_with_seeders_is_not_a_lost_cause(monkeypatch):
    row = _row(seeds=3)
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_a_torrent_still_transferring_is_not_a_lost_cause(monkeypatch):
    row = _row(rate=50_000.0)
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_a_near_complete_torrent_is_left_to_finish(monkeypatch):
    row = _row(progress=97.0)
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_seeding_torrents_are_never_candidates(monkeypatch):
    row = _row(state="Seeding", progress=100.0)
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_paused_torrents_are_never_candidates(monkeypatch):
    # Paused is the user's own choice, not a stall.
    row = _row(state="Paused")
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_a_tracker_error_with_no_peers_is_a_candidate(monkeypatch):
    row = _row(state="Error")
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert len(replace.find_lost_causes(CONFIG)) == 1


def test_a_torrent_with_no_time_added_is_skipped_rather_than_guessed(monkeypatch):
    row = _row(time_added=0)
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [row])
    assert replace.find_lost_causes(CONFIG) == []


def test_results_are_ordered_oldest_stall_first(monkeypatch):
    young = _row(name="young", time_added=int(time.time()) - 40 * 60)
    old = _row(name="old", time_added=int(time.time()) - 400 * 60)
    monkeypatch.setattr(replace.deluge, "list_torrents", lambda c: [young, old])
    out = replace.find_lost_causes(CONFIG)
    assert [r["name"] for r in out] == ["old", "young"]


# --- remove_lost_cause -------------------------------------------------------


def test_remove_lost_cause_removes_data_too(monkeypatch):
    calls = []

    class _Client:
        def call(self, method, *args):
            calls.append((method, args))

    @contextlib.contextmanager
    def fake_connect(config):
        yield _Client()

    monkeypatch.setattr(replace.deluge, "connect", fake_connect)
    replace.remove_lost_cause("abc123", CONFIG)

    assert calls == [("core.remove_torrent", ("abc123", True))]


# --- replacement_query -------------------------------------------------------


def test_an_episode_becomes_a_season_episode_query():
    query = replace.replacement_query("Some.Show.S01E01.1080p.WEB.h264-GRP.mkv")
    assert query == ("Some Show S01E01", "tv")


def test_a_season_pack_becomes_a_season_query():
    query = replace.replacement_query("Some.Show.S02.1080p.WEB.h264-GRP")
    assert query == ("Some Show S02", "tv")


def test_an_episode_with_a_year_keeps_it_in_the_query():
    """Regression: a stalled "Frasier.2023.S01E07..." replaced without the
    year searched plain "Frasier S01E07" — ambiguous between the 2023 reboot
    and the 1993 original — and fetched the wrong show's episode of the same
    number. The year is the only thing that disambiguates a same-titled
    reboot/remake, same as `tidy.tvmaze_show`'s year matching.
    """
    query = replace.replacement_query(
        "Frasier.2023.S01E07.1080p.HEVC.x265-MeGusta.mkv"
    )
    assert query == ("Frasier 2023 S01E07", "tv")


def test_a_season_pack_with_a_year_keeps_it_too():
    query = replace.replacement_query("Frasier.2023.S02.1080p.x265-ELiTE")
    assert query == ("Frasier 2023 S02", "tv")


def test_a_movie_becomes_a_title_and_year_query():
    query = replace.replacement_query("Some.Movie.2020.1080p.BluRay.x264-GRP")
    assert query == ("Some Movie 2020", "movie")


def test_a_movie_without_a_year_is_still_a_query():
    query = replace.replacement_query("Some.Movie.BluRay.x264-GRP")
    assert query == ("Some Movie", "movie")


def test_an_unparseable_name_refuses_rather_than_guesses():
    # No title token at all — season/episode alone is not enough to search.
    assert replace.replacement_query("S01E01.mkv") is None


# --- CLI --------------------------------------------------------------------


def test_dry_run_lists_candidates_and_touches_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(replace, "load_config", lambda path: CONFIG)
    monkeypatch.setattr(replace, "find_lost_causes", lambda cfg: [{**_row(), "age_minutes": 60.0}])
    removed = []
    monkeypatch.setattr(
        replace, "remove_lost_cause", lambda tid, cfg: removed.append(tid)
    )

    code = replace.main(["--dry-run"])

    assert code == 0
    assert removed == []
    out = capsys.readouterr().out
    assert "Some.Show.S01E01" in out


def test_no_candidates_is_reported_and_exits_clean(monkeypatch, capsys):
    monkeypatch.setattr(replace, "load_config", lambda path: CONFIG)
    monkeypatch.setattr(replace, "find_lost_causes", lambda cfg: [])

    code = replace.main([])

    assert code == 0
    assert "No stalled torrents found." in capsys.readouterr().out
