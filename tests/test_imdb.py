"""Accepting an IMDb link as a request.

A link names exactly one title, which is the point: "fargo" is a 1996 film and
a 2014 series, "the office" is two different shows. Resolving by id removes
the guess — as long as the resolution itself is exact, which is why TVmaze and
Wikidata are used rather than a title search.
"""

from __future__ import annotations

import pytest

from torrent_agent import imdb


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://www.imdb.com/title/tt0995832/", "tt0995832"),
        ("https://m.imdb.com/title/tt0995832/?ref_=nv_sr_1", "tt0995832"),
        ("tt0116282", "tt0116282"),
        ("get me tt1234567 please", "tt1234567"),
        ("imdb.com/title/tt12345678", "tt12345678"),
        ("the bear season 3", None),
        ("", None),
        # Not an id: too short, and "tt" alone should not match.
        ("tt123", None),
    ],
)
def test_id_extraction(text, expected):
    assert imdb.extract_id(text) == expected


def test_a_tv_link_becomes_a_title_and_year(monkeypatch):
    monkeypatch.setattr(
        imdb, "_lookup_tv",
        lambda i: {"kind": "tv", "name": "Generation Kill", "year": 2008},
    )
    query, note = imdb.expand("https://www.imdb.com/title/tt0995832/")

    assert query == "Generation Kill 2008"
    assert "tt0995832" in note and "tv" in note


def test_television_is_tried_before_film(monkeypatch):
    """A series resolved as a film searches for one release, not a season pack."""
    calls = []
    monkeypatch.setattr(
        imdb, "_lookup_tv",
        lambda i: calls.append("tv") or {"kind": "tv", "name": "Fargo", "year": 2014},
    )
    monkeypatch.setattr(imdb, "_lookup_film", lambda i: calls.append("film") or None)

    imdb.expand("tt2802850")
    assert calls == ["tv"], "film lookup should not run once TV matched"


def test_falls_back_to_film(monkeypatch):
    monkeypatch.setattr(imdb, "_lookup_tv", lambda i: None)
    monkeypatch.setattr(
        imdb, "_lookup_film", lambda i: {"kind": "film", "name": "Fargo", "year": 1996}
    )
    query, note = imdb.expand("tt0116282")

    assert query == "Fargo 1996"
    assert "film" in note


def test_the_whole_url_is_replaced_not_appended(monkeypatch):
    # URL path segments are not search terms; leaving them in poisons ranking.
    monkeypatch.setattr(
        imdb, "_lookup_tv", lambda i: {"kind": "tv", "name": "Generation Kill", "year": 2008}
    )
    query, _ = imdb.expand("https://www.imdb.com/title/tt0995832/?ref_=nv_sr_1")
    assert query == "Generation Kill 2008"
    assert "imdb" not in query.lower()


def test_plain_text_is_untouched():
    query, note = imdb.expand("the bear season 3")
    assert query == "the bear season 3"
    assert note is None


def test_an_unresolvable_id_is_left_as_written(monkeypatch):
    # Better to search for what was typed and fail visibly than to send an
    # empty query and report "no results" for a title nobody asked for.
    monkeypatch.setattr(imdb, "_lookup_tv", lambda i: None)
    monkeypatch.setattr(imdb, "_lookup_film", lambda i: None)

    query, note = imdb.expand("tt9999999")
    assert query == "tt9999999"
    assert "Could not resolve" in note


def test_a_title_without_a_year_still_searches(monkeypatch):
    monkeypatch.setattr(
        imdb, "_lookup_tv", lambda i: {"kind": "tv", "name": "Some Show", "year": None}
    )
    query, _ = imdb.expand("tt1111111")
    assert query == "Some Show"


def test_instructions_after_a_bare_id_survive(monkeypatch):
    # Regression: an episode-range request ("all S01 except E01") named the
    # show by id, and the old code discarded everything but the id, silently
    # dropping the range along with it.
    monkeypatch.setattr(
        imdb, "_lookup_tv",
        lambda i: {"kind": "tv", "name": "Foundation", "year": 2021},
    )
    query, _ = imdb.expand("tt14124236 get all S01 episodes except E01")
    assert query == "Foundation 2021 get all S01 episodes except E01"


def test_instructions_around_a_link_survive(monkeypatch):
    monkeypatch.setattr(
        imdb, "_lookup_tv",
        lambda i: {"kind": "tv", "name": "Father Ted", "year": 1995},
    )
    query, _ = imdb.expand(
        "first 2 episodes of https://www.imdb.com/title/tt0118401/ season 3"
    )
    assert query == "first 2 episodes of Father Ted 1995 season 3"
