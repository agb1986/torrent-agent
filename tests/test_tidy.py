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


def test_a_films_part_number_stays_in_the_title(tmp_path, monkeypatch):
    """`Dune Part Two` must not be looked up as `Dune`.

    guessit splits the part off into its own field, leaving a title TMDB only
    knows from 1984 and 2021 — the ambiguity escalated the whole download.
    """
    asked = []

    def _resolve(mt, title, year, imdb):
        asked.append(title)
        return ("693134", "Dune: Part Two", 2024, "")

    monkeypatch.setattr(tidy, "_resolve_tmdb", _resolve)
    src = tmp_path / "Dune Part Two 2024 1080p BluRay x265 DD 7 1-Pahe in"
    _mk(src / "Dune Part Two 2024 1080p BluRay x265 DD 7 1-Pahe in.mkv")

    plan = tidy.plan_for(src)

    assert asked == ["Dune Part Two"]
    assert plan.confident, plan.problems
    assert plan.root.name == "Dune Part Two (2024) [tmdbid-693134].mkv"


def test_a_disc_split_part_is_not_glued_onto_the_title(tmp_path, monkeypatch):
    """Half a rip is also "part 1", and it is not part of the film's name.

    The year sitting between the title and the part token is what separates
    the two cases; without that check every split release would be looked up
    under a title nothing matches.
    """
    asked = []

    def _resolve(mt, title, year, imdb):
        asked.append(title)
        return ("13446", "Some Film", 2001, "")

    monkeypatch.setattr(tidy, "_resolve_tmdb", _resolve)
    src = tmp_path / "Some.Film.2001.1080p.BluRay.Part.1"
    _mk(src / "Some.Film.2001.1080p.BluRay.Part.1.mkv")

    plan = tidy.plan_for(src)

    assert asked == ["Some Film"]


def test_a_rejoined_title_tmdb_rejects_falls_back_to_the_bare_one(tmp_path, monkeypatch):
    """Rejoining is a claim about where the name ends, and it can be wrong.

    TMDB not knowing "Some Film Part 2" means the part was never part of the
    title — not that the film is unknown — so the bare title gets its turn.
    """
    answers = {"Some Film Part 2": (None, "Some Film Part 2", 2001, "")}
    asked = []

    def _resolve(mt, title, year, imdb):
        asked.append(title)
        return answers.get(title, ("999", "Some Film", 2001, ""))

    monkeypatch.setattr(tidy, "_resolve_tmdb", _resolve)
    src = tmp_path / "Some Film Part 2 2001 1080p BluRay"
    _mk(src / "Some Film Part 2 2001 1080p BluRay.mkv")

    plan = tidy.plan_for(src)

    assert asked == ["Some Film Part 2", "Some Film"]
    assert plan.confident, plan.problems
    assert plan.root.name == "Some Film (2001) [tmdbid-999].mkv"


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


# --- manga ------------------------------------------------------------------


def test_a_manga_release_is_routed_without_renaming(tmp_path):
    src = tmp_path / "[Group] One Piece v01-v10 [Complete]"
    src.mkdir()
    (src / "One Piece v01.cbz").write_bytes(b"\0" * 1024)
    (src / "One Piece v02.cbz").write_bytes(b"\0" * 1024)

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    assert plan.kind == "manga"
    assert plan.root == src
    assert plan.moves == []  # nothing renamed — transfer moves it as one unit


def test_a_single_manga_file_is_routed(tmp_path):
    src = tmp_path / "One Piece v01.cbz"
    src.write_bytes(b"\0" * 1024)

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    assert plan.kind == "manga"
    assert plan.root == src


def test_a_directory_with_no_media_and_no_manga_is_still_escalated(tmp_path):
    # Regression guard: the manga fallback must not swallow the genuine
    # "nothing recognisable here" case.
    src = tmp_path / "empty"
    src.mkdir()
    (src / "readme.txt").write_text("nothing here")

    plan = tidy.plan_for(src)
    assert not plan.confident
    assert plan.kind == "unknown"


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


# --- absolute (anime) episode numbering ------------------------------------


@pytest.fixture
def anime_lookups(monkeypatch):
    """A long-running show TVmaze numbers by broadcast year, as it does anime."""
    episodes = {(year, n): f"Ep {year}-{n}" for year in (2023, 2024, 2025)
                for n in range(1, 61)}

    def _install(name="Long Show", show_id=1505):
        monkeypatch.setattr(
            tidy, "tvmaze_show",
            lambda title, year=None: {"id": show_id, "name": name,
                                      "premiered": "1999-10-20", "externals": {}},
        )
        monkeypatch.setattr(tidy, "tvmaze_episodes", lambda sid: episodes)
        monkeypatch.setattr(
            tidy, "_resolve_tmdb", lambda mt, t, y, imdb: ("37854", name, 1999, ""),
        )
        return episodes

    return _install


def test_absolute_episode_numbers_map_through_broadcast_order(tmp_path, anime_lookups):
    """Anime ships "Show - 145 - Title", not S2025E25.

    guessit has no concept of absolute numbering and splits that token into
    season 1, episode 45 — which resolves against no show. Before this was
    handled every file in a pack failed the lookup, so five One Piece Egghead
    Island packs downloaded overnight and every one escalated instead of
    filing, for 74GB of downloads nobody could watch.
    """
    anime_lookups()
    src = tmp_path / "Long Show (Arc 145-146) - 2160p"
    _mk(src / "Long Show - 145 - The Winner Takes All - 2160p.mkv")
    _mk(src / "Long Show - 146 - A Forbidden Piece of History - 2160p.mkv")

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    # 145th episode overall: seasons 2023 and 2024 hold 60 each, so 2025 #25.
    assert [m.target.name for m in plan.moves] == [
        "S2025E25 - Ep 2025-25.mkv",
        "S2025E26 - Ep 2025-26.mkv",
    ]
    assert plan.moves[0].target.parent.name == "Season 2025"


def test_round_absolute_numbers_are_not_lost_to_a_zero_episode(tmp_path, anime_lookups):
    """"100" splits into season 1, episode 0 — a number, but a falsy one."""
    anime_lookups()
    src = tmp_path / "Long Show"
    _mk(src / "Long Show - 100 - The Winner Takes All - 2160p.mkv")

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    assert plan.moves[0].target.name == "S2024E40 - Ep 2024-40.mkv"


def test_a_point_five_recap_is_left_in_place_rather_than_colliding(tmp_path, anime_lookups):
    """"145.5" is anime's recap convention, and it rounds onto episode 145.

    Claiming it would file two different files as the same episode. It is not a
    failure either — refusing the whole pack over a recap would strand every
    real episode with it — so it is reported and left behind.
    """
    anime_lookups()
    src = tmp_path / "Long Show"
    _mk(src / "Long Show - 145 - The Winner Takes All - 2160p.mkv")
    _mk(src / "Long Show - 145.5 - A Special Recap - 2160p.mkv")

    plan = tidy.plan_for(src)

    assert plan.confident, plan.problems
    assert [m.target.name for m in plan.moves] == ["S2025E25 - Ep 2025-25.mkv"]
    assert any("recap special" in n for n in plan.notes)
    assert any(p.name.endswith("145.5 - A Special Recap - 2160p.mkv") for p in plan.left_behind)


def test_an_explicit_season_episode_never_becomes_an_absolute_number(tmp_path, tv_lookups):
    """S02E05 must not be rebuilt into absolute 205 when TVmaze lacks it.

    The absolute reader works by rejoining the digits guessit split apart, so
    it has to prove those digits really appear as one number in the name —
    otherwise a missing episode silently maps onto an unrelated one instead of
    escalating.
    """
    tv_lookups(episodes={(2, n): f"Ep {n}" for n in range(1, 100) if n != 5})
    src = tmp_path / "Generation Kill"
    _mk(src / "Generation Kill S02E05 Missing.mkv")

    plan = tidy.plan_for(src)

    assert not plan.confident
    assert any("S02E05" in p for p in plan.problems)


def test_a_shared_title_is_settled_by_which_show_the_files_fit(tmp_path, monkeypatch):
    """Two shows named "One Piece": the 1999 anime and the 2023 live-action.

    They score identically on TVmaze and the live-action comes back first, and
    anime releases carry no year to disambiguate on — so the year-matching path
    never runs and the anime resolved to the live-action series' tmdb id. The
    episodes themselves are the evidence: 8 live-action episodes cannot hold
    absolute number 145.
    """
    live = {"id": 46065, "name": "One Piece", "premiered": "2023-08-31", "externals": {}}
    anime = {"id": 1505, "name": "One Piece", "premiered": "1999-10-20", "externals": {}}
    anime_eps = {(year, n): f"Ep {year}-{n}" for year in (2023, 2024, 2025)
                 for n in range(1, 61)}
    lists = {46065: {(1, n): f"Live {n}" for n in range(1, 9)}, 1505: anime_eps}

    monkeypatch.setattr(tidy, "tvmaze_show", lambda title, year=None: live)
    monkeypatch.setattr(tidy, "tvmaze_candidates", lambda title: [live, anime])
    monkeypatch.setattr(tidy, "tvmaze_episodes", lambda sid: lists[sid])
    monkeypatch.setattr(
        tidy, "_resolve_tmdb",
        lambda mt, t, y, imdb: (("37854" if y == 1999 else "111110"), "One Piece", y, ""),
    )

    src = tmp_path / "One Piece (Arc 145-146) - 2160p"
    _mk(src / "One Piece - 145 - The Winner Takes All - 2160p.mkv")

    plan = tidy.plan_for(src)

    assert plan.tmdb_id == "37854"  # the anime, not tt11737520's live-action
    assert plan.year == 1999
