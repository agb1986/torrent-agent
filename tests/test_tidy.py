"""Deciding how a download should be named — and when to refuse.

Most of these are about refusing. Once tidying runs unattended, a confident
wrong answer writes a mis-titled show into a real library, Jellyfin fetches
metadata for the wrong programme, and nobody notices until they try to watch
it. An escalation costs a Telegram message.
"""

from __future__ import annotations

import pytest

from torrent_agent import tidy

BIG = 60 * 1024 * 1024  # over the junk threshold


def _mk(path, size=BIG):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return path


@pytest.fixture
def tv_lookups(monkeypatch):
    """TVmaze and TMDB stubbed for a known show."""

    def _install(name="Generation Kill", premiered="2008-07-13", episodes=None, tmdb="17035"):
        monkeypatch.setattr(
            tidy, "tvmaze_show",
            lambda title, year=None: {"id": 1240, "name": name, "premiered": premiered,
                                       "externals": {"imdb": "tt0995832"}},
        )
        monkeypatch.setattr(
            tidy, "tvmaze_episodes",
            lambda sid: episodes if episodes is not None else {
                (1, 1): "Get Some", (1, 2): "The Cradle of Civilization",
                (1, 3): "Screwby", (1, 7): "Bomb in the Garden",
            },
        )
        monkeypatch.setattr(
            tidy, "_resolve_tmdb",
            lambda mt, t, y, imdb: (tmdb, name, 2008 if premiered else None, ""),
        )

    return _install


# --- tvmaze_show year disambiguation ---------------------------------------


def test_tvmaze_show_picks_the_matching_release_year(monkeypatch):
    """A same-titled reboot must not resolve to the original show.

    Regression for the "Frasier" bug: title-only search picked the 1993
    original (tt0106004) over the 2023 revival (tt14124236) the user
    actually asked for, writing the download into the wrong library folder.
    """
    original = {"id": 540, "name": "Frasier", "premiered": "1993-09-16",
                "externals": {"imdb": "tt0106004"}}
    reboot = {"id": 53775, "name": "Frasier", "premiered": "2023-10-12",
              "externals": {"imdb": "tt14124236"}}
    monkeypatch.setattr(
        tidy, "_get_json", lambda url: [{"show": original}, {"show": reboot}],
    )

    assert tidy.tvmaze_show("Frasier", 2023) == reboot
    assert tidy.tvmaze_show("Frasier", 1993) == original
    assert tidy.tvmaze_show("Frasier", None) == original  # first result, unresolved


# --- the happy paths ------------------------------------------------------


def test_bare_episode_numbers_are_treated_as_season_one(tmp_path, tv_lookups):
    # A miniseries often ships E01..E07 with no season token anywhere.
    tv_lookups()
    src = tmp_path / "Generation Kill (1080p x265 Joy)"
    _mk(src / "Generation Kill E01 Get Some (1080p x265 Joy).mkv")
    _mk(src / "Generation Kill E07 Bomb in the Garden (1080p x265 Joy).mkv")

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    assert plan.root.name == "Generation Kill (2008) [tmdbid-17035]"
    targets = sorted(m.target.relative_to(plan.root).as_posix() for m in plan.moves)
    assert targets == [
        "Season 01/S01E01 - Get Some.mkv",
        "Season 01/S01E07 - Bomb in the Garden.mkv",
    ]


def test_junk_files_are_left_behind(tmp_path, tv_lookups):
    tv_lookups()
    src = tmp_path / "Generation Kill"
    _mk(src / "Generation Kill E01 Get Some.mkv")
    (src / "How to play HEVC (THIS FILE).txt").write_text("junk")
    (src / "sample.mkv").write_bytes(b"\0" * 1024)      # too small to be real

    plan = tidy.plan_for(src)

    assert plan.confident
    assert len(plan.moves) == 1
    assert {p.name for p in plan.left_behind} == {
        "How to play HEVC (THIS FILE).txt", "sample.mkv"
    }


def test_a_film_becomes_a_flat_tagged_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tidy, "_resolve_tmdb", lambda mt, t, y, imdb: ("13446", "Withnail & I", 1987, "")
    )
    src = tmp_path / "Withnail.and.I.1987.1080p.BluRay.x264-GROUP"
    _mk(src / "Withnail.and.I.1987.1080p.BluRay.x264-GROUP.mkv")

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    assert plan.kind == "film"
    assert plan.root.name == "Withnail & I (1987) [tmdbid-13446].mkv"


# --- the refusals ---------------------------------------------------------


def test_ambiguous_tmdb_match_is_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tidy, "_resolve_tmdb",
        lambda mt, t, y, imdb: (None, t, y, "ambiguous TMDB match — candidates: A (1974), B (2004)"),
    )
    src = tmp_path / "The.Wicker.Man.1973.1080p"
    _mk(src / "The.Wicker.Man.1973.1080p.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("ambiguous" in p for p in plan.problems)


def test_a_film_without_a_year_is_escalated(tmp_path):
    src = tmp_path / "Some.Film.1080p.BluRay.x264"
    _mk(src / "Some.Film.1080p.BluRay.x264.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("no year" in p for p in plan.problems)


def test_unknown_show_is_escalated(tmp_path, monkeypatch):
    monkeypatch.setattr(tidy, "tvmaze_show", lambda title, year=None: None)
    src = tmp_path / "Obscure.Thing.S01E01.1080p"
    _mk(src / "Obscure.Thing.S01E01.1080p.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("TVmaze" in p for p in plan.problems)


def test_missing_episode_in_tvmaze_is_escalated(tmp_path, tv_lookups):
    tv_lookups(episodes={(1, 1): "Get Some"})
    src = tmp_path / "Generation Kill"
    _mk(src / "Generation Kill E01 Get Some.mkv")
    _mk(src / "Generation Kill E09 Nonexistent.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("S01E09" in p for p in plan.problems)


def test_files_disagreeing_on_the_show_are_escalated(tmp_path, tv_lookups):
    tv_lookups()
    src = tmp_path / "mixed"
    _mk(src / "Generation Kill S01E01 Get Some.mkv")
    _mk(src / "Toast.of.London.S01E01.Addictive.Personality.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("disagree" in p for p in plan.problems)


def test_a_directory_mixing_episodes_and_films_is_escalated(tmp_path):
    src = tmp_path / "mixed"
    _mk(src / "Some.Show.S01E01.1080p.mkv")
    _mk(src / "Withnail.and.I.1987.1080p.BluRay.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("mixes" in p for p in plan.problems)


def test_no_media_files_is_escalated(tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    (src / "readme.txt").write_text("nothing here")

    plan = tidy.plan_for(src)
    assert not plan.confident
    assert any("no media files" in p for p in plan.problems)


# --- execution ------------------------------------------------------------


def test_execute_refuses_an_unconfident_plan(tmp_path):
    plan = tidy.TidyPlan(kind="tv", problems=["something is unclear"])
    with pytest.raises(ValueError, match="unconfident"):
        tidy.execute(plan)


def test_execute_moves_files_into_place(tmp_path, tv_lookups):
    tv_lookups()
    src = tmp_path / "Generation Kill"
    _mk(src / "Generation Kill E01 Get Some.mkv")

    plan = tidy.plan_for(src)
    tidy.execute(plan)

    landed = plan.root / "Season 01" / "S01E01 - Get Some.mkv"
    assert landed.exists()
    assert not (src / "Generation Kill E01 Get Some.mkv").exists()
