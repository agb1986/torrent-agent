"""The two lookup tools that resolve a range/exclusion before search+add.

Not the model loop itself (needs a live Anthropic call) — just the tool
implementations, which are ordinary functions once you get past `_dispatch`.
"""

from __future__ import annotations

import json

import tmdb_id

from torrent_agent import agent
from torrent_agent.agent import TorrentAgent


def _mk_agent():
    return TorrentAgent({})


# --------------------------------------------------------------------------- #
# list_episodes
# --------------------------------------------------------------------------- #

def test_list_episodes_returns_one_seasons_worth(monkeypatch):
    monkeypatch.setattr(
        agent.tidy, "tvmaze_show",
        lambda title, year=None: {"id": 1234, "name": "Father Ted"},
    )
    monkeypatch.setattr(
        agent.tidy, "tvmaze_episode_details",
        lambda show_id: {
            (1, 1): {"name": "Good Luck, Father Ted", "airstamp": "1995-04-21T21:00:00Z"},
            (2, 1): {"name": "Hell", "airstamp": "1996-02-23T21:00:00Z"},
            (3, 1): {"name": "Are You Right There, Father Ted?", "airstamp": "1998-03-06T21:00:00Z"},
            (3, 2): {"name": "Kicking Bishop Brennan Up the Arse", "airstamp": "1998-03-13T21:00:00Z"},
        },
    )

    result = json.loads(_mk_agent()._tool_list_episodes("Father Ted", 3))

    assert result["show"] == "Father Ted"
    assert [e["episode"] for e in result["episodes"]] == [1, 2]
    assert all(e["season"] == 3 for e in result["episodes"])
    assert all(e["aired"] for e in result["episodes"])  # all long since aired


def test_list_episodes_no_show_match(monkeypatch):
    monkeypatch.setattr(agent.tidy, "tvmaze_show", lambda title, year=None: None)
    result = json.loads(_mk_agent()._tool_list_episodes("Nonexistent Show", 1))
    assert "error" in result


def test_list_episodes_no_such_season(monkeypatch):
    monkeypatch.setattr(
        agent.tidy, "tvmaze_show", lambda title, year=None: {"id": 1, "name": "X"}
    )
    monkeypatch.setattr(
        agent.tidy, "tvmaze_episode_details",
        lambda show_id: {(1, 1): {"name": "Only Episode", "airstamp": None}},
    )
    result = json.loads(_mk_agent()._tool_list_episodes("X", 9))
    assert "error" in result
    assert "season 9" in result["error"]


def test_list_episodes_marks_unaired_episodes(monkeypatch):
    monkeypatch.setattr(
        agent.tidy, "tvmaze_show", lambda title, year=None: {"id": 1, "name": "X"}
    )
    monkeypatch.setattr(
        agent.tidy, "tvmaze_episode_details",
        lambda show_id: {(1, 1): {"name": "Far Future", "airstamp": "2099-01-01T00:00:00Z"}},
    )
    result = json.loads(_mk_agent()._tool_list_episodes("X", 1))
    assert result["episodes"][0]["aired"] is False


# --------------------------------------------------------------------------- #
# list_filmography
# --------------------------------------------------------------------------- #

def test_list_filmography_returns_films(monkeypatch):
    monkeypatch.setattr(
        tmdb_id, "films_by_director",
        lambda name: [{"name": "Se7en", "year": 1995}, {"name": "Fight Club", "year": 1999}],
    )
    result = json.loads(_mk_agent()._tool_list_filmography("David Fincher"))
    assert result["director"] == "David Fincher"
    assert {f["name"] for f in result["films"]} == {"Se7en", "Fight Club"}


def test_list_filmography_no_match(monkeypatch):
    monkeypatch.setattr(tmdb_id, "films_by_director", lambda name: [])
    result = json.loads(_mk_agent()._tool_list_filmography("Nobody At All"))
    assert "error" in result


def test_list_filmography_lookup_failure_is_a_tool_error_not_a_crash(monkeypatch):
    def boom(name):
        raise OSError("wikidata down")

    monkeypatch.setattr(tmdb_id, "films_by_director", boom)
    result = json.loads(_mk_agent()._tool_list_filmography("David Fincher"))
    assert "error" in result
    assert "wikidata down" in result["error"]


# --------------------------------------------------------------------------- #
# tool schema
# --------------------------------------------------------------------------- #

def test_manga_is_a_valid_media_type():
    search_tool = next(t for t in agent.TOOLS if t["name"] == "search_torrents")
    assert "manga" in search_tool["input_schema"]["properties"]["media_type"]["enum"]


def test_dispatch_routes_the_new_tools(monkeypatch):
    a = _mk_agent()
    monkeypatch.setattr(a, "_tool_list_episodes", lambda show, season, year=None: "episodes-called")
    monkeypatch.setattr(a, "_tool_list_filmography", lambda director: "filmography-called")

    assert a._dispatch("list_episodes", {"show": "X", "season": 1}) == "episodes-called"
    assert a._dispatch("list_filmography", {"director": "X"}) == "filmography-called"
