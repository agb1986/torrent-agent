"""TMDB id resolution and the Jellyfin naming tag."""

import pytest

import tmdb_id


def _entity(tmdb_prop=None, tmdb_value=None, label=None, dates=None):
    claims = {}
    if tmdb_prop:
        claims[tmdb_prop] = [
            {"mainsnak": {"datavalue": {"value": tmdb_value}}}
        ]
    for prop, times in (dates or {}).items():
        claims[prop] = [
            {"mainsnak": {"datavalue": {"value": {"time": t}}}} for t in times
        ]
    return {"claims": claims, "labels": {"en": {"value": label}} if label else {}}


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

def test_format_tag_matches_jellyfin_convention():
    assert tmdb_id.format_tag("One Piece", 1999, 37854) == "One Piece (1999) [tmdbid-37854]"


def test_format_tag_drops_the_parts_it_does_not_have():
    # An empty "()" or "[tmdbid-]" would confuse Jellyfin's parser, so a
    # missing year or id has to vanish entirely rather than render blank.
    assert tmdb_id.format_tag("Withnail and I", None, 13446) == "Withnail and I [tmdbid-13446]"
    assert tmdb_id.format_tag("Withnail and I", 1987, None) == "Withnail and I (1987)"


def test_format_tag_is_idempotent():
    # Re-tidying an already-tagged directory must not stack the tags up.
    once = tmdb_id.format_tag("One Piece", 1999, 37854)
    assert tmdb_id.format_tag(once, 1999, 37854) == once


def test_sanitise_name_strips_characters_a_path_cannot_hold():
    assert tmdb_id.sanitise_name("Face/Off: The Sequel?") == "FaceOff The Sequel"


# --------------------------------------------------------------------------- #
# Wikidata parsing
# --------------------------------------------------------------------------- #

def test_candidates_keep_only_the_requested_media_type(monkeypatch):
    # Searching "One Piece" turns up video games and the franchise entity too;
    # only the series carries P4983, which is what filters them out.
    entities = {
        "Q1": _entity("P4983", "37854", "One Piece", {"P580": ["+1999-10-20T00:00:00Z"]}),
        "Q2": _entity(label="One Piece"),          # the video game
        "Q3": _entity("P4947", "12345", "One Piece"),  # the film series
    }
    monkeypatch.setattr(tmdb_id, "_get_json", lambda *a, **k: {"entities": entities})

    got = tmdb_id._wikidata_candidates("tv", ["Q1", "Q2", "Q3"])
    assert got == [
        {"tmdb_id": "37854", "name": "One Piece", "year": 1999, "source": "wikidata"}
    ]


def test_entity_year_takes_the_earliest_release():
    # Festival runs and staggered international dates leave several; releases
    # are named for the first one.
    entity = _entity(
        dates={"P577": ["+1988-02-12T00:00:00Z", "+1987-06-19T00:00:00Z"]}
    )
    assert tmdb_id._entity_year(entity, "movie") == 1987


def test_entity_year_is_none_when_wikidata_has_no_date():
    assert tmdb_id._entity_year(_entity(label="Nameless"), "movie") is None


# --------------------------------------------------------------------------- #
# Choosing between candidates
# --------------------------------------------------------------------------- #

def _candidate(name, year, tmdb_id_="1"):
    return {"tmdb_id": tmdb_id_, "name": name, "year": year, "source": "wikidata"}


def test_choose_prefers_the_matching_year():
    # The remake trap: same title, different film.
    candidates = [_candidate("The Thing", 2011, "60935"), _candidate("The Thing", 1982, "1091")]
    best, _ = tmdb_id.choose(candidates, "The Thing", 1982)
    assert best["tmdb_id"] == "1091"


def test_choose_tolerates_a_year_off_by_one():
    # Release years wobble by a year across regions.
    candidates = [_candidate("Withnail and I", 1987, "13446"), _candidate("Withnail", 2020, "999")]
    best, _ = tmdb_id.choose(candidates, "Withnail and I", 1988)
    assert best["tmdb_id"] == "13446"


def test_choose_normalises_ampersands_and_punctuation():
    candidates = [_candidate("Withnail & I", 1987, "13446"), _candidate("Withnail and I Redux", 1987, "9")]
    best, _ = tmdb_id.choose(candidates, "Withnail and I", 1987)
    assert best["tmdb_id"] == "13446"


def test_choose_returns_no_winner_when_two_tie():
    # Two remakes and no year to separate them: the caller must ask rather
    # than tag the library with a coin flip.
    candidates = [_candidate("The Thing", 1982, "1091"), _candidate("The Thing", 2011, "60935")]
    best, ranked = tmdb_id.choose(candidates, "The Thing", None)
    assert best is None
    assert len(ranked) == 2


def test_choose_on_no_candidates():
    assert tmdb_id.choose([], "Nothing", 1999) == (None, [])


# --------------------------------------------------------------------------- #
# Source order
# --------------------------------------------------------------------------- #

def test_resolve_prefers_tmdb_when_a_key_is_set(monkeypatch):
    monkeypatch.setattr(
        tmdb_id, "tmdb_lookup", lambda *a, **k: [dict(_candidate("One Piece", 1999, "37854"), source="tmdb")]
    )
    monkeypatch.setattr(tmdb_id, "wikidata_by_title", lambda *a, **k: pytest.fail("should not fall back"))

    best, _ = tmdb_id.resolve("tv", "One Piece", 1999, api_key="secret")
    assert best["source"] == "tmdb"


def test_resolve_falls_back_to_wikidata_when_tmdb_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("tmdb down")

    monkeypatch.setattr(tmdb_id, "tmdb_lookup", boom)
    monkeypatch.setattr(
        tmdb_id, "wikidata_by_title", lambda *a, **k: [_candidate("One Piece", 1999, "37854")]
    )
    best, _ = tmdb_id.resolve("tv", "One Piece", 1999, api_key="secret")
    assert best["tmdb_id"] == "37854"


def test_resolve_falls_back_to_title_when_imdb_lookup_finds_nothing(monkeypatch):
    # Wikidata knows the IMDb id but has no TMDB link on that entity; the
    # title search can still turn one up.
    monkeypatch.setattr(tmdb_id, "wikidata_by_imdb", lambda *a, **k: [])
    monkeypatch.setattr(
        tmdb_id, "wikidata_by_title", lambda *a, **k: [_candidate("One Piece", 1999, "37854")]
    )
    best, _ = tmdb_id.resolve("tv", "One Piece", 1999, imdb="tt0388629")
    assert best["tmdb_id"] == "37854"


def test_resolve_raises_when_every_source_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(tmdb_id, "wikidata_by_title", boom)
    with pytest.raises(tmdb_id.LookupError_):
        tmdb_id.resolve("tv", "One Piece", 1999)


def test_resolve_returns_nothing_when_no_source_matches(monkeypatch):
    monkeypatch.setattr(tmdb_id, "wikidata_by_title", lambda *a, **k: [])
    assert tmdb_id.resolve("movie", "Zzqq Not A Real Film", None) == (None, [])
