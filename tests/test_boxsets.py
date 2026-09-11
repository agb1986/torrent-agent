"""Putting a delivered film into Jellyfin collections.

Directors go by rule, themes by Claude — and Claude's answer is checked
rather than trusted, because it names collections and films that must really
exist. The collections file is hand-kept, so writing it back must not
reformat it.
"""

import json
from types import SimpleNamespace

import pytest

from torrent_agent import boxsets


def movie(tmdb, name, year, directors=(), genres=("Drama",)):
    return {
        "Id": f"item{tmdb}",
        "Name": name,
        "ProductionYear": year,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "People": [{"Name": d, "Type": "Director"} for d in directors],
        "Genres": list(genres),
    }


LIBRARY = [
    movie(807, "Se7en", 1995, ["David Fincher"]),
    movie(550, "Fight Club", 1999, ["David Fincher"]),
    movie(1949, "Zodiac", 2007, ["David Fincher"], ["Crime", "Mystery"]),
    movie(747, "Shaun of the Dead", 2004, ["Edgar Wright"]),
    movie(4638, "Hot Fuzz", 2007, ["Edgar Wright"]),
    movie(107985, "The World's End", 2013, ["Edgar Wright"]),
    movie(339403, "Baby Driver", 2017, ["Edgar Wright"]),
    movie(115, "The Big Lebowski", 1998, ["Joel Coen", "Ethan Coen"]),
    movie(6977, "No Country for Old Men", 2007, ["Joel Coen", "Ethan Coen"]),
    movie(275, "Fargo", 1996, ["Joel Coen", "Ethan Coen"]),
    movie(62, "2001: A Space Odyssey", 1968, ["Stanley Kubrick"]),
    movie(185, "A Clockwork Orange", 1971, ["Stanley Kubrick"]),
    movie(694, "The Shining", 1980, ["Stanley Kubrick"]),
    movie(120, "The Fellowship of the Ring", 2001, ["Peter Jackson"]),
    movie(121, "The Two Towers", 2002, ["Peter Jackson"]),
    movie(122, "The Return of the King", 2003, ["Peter Jackson"]),
    movie(9999, "Heavenly Creatures", 1994, ["Peter Jackson"]),
]

DEFS_TEXT = """{
  "_comment": "hand kept",

  "David Fincher": [807, 550],
  "The Coen Brothers": [115, 6977],

  "Three Flavours Cornetto": [747, 4638, 107985],
  "Detectives & Killers": [807]
}
"""

BOXSETS = {
    "David Fincher": "bs-fincher",
    "The Coen Brothers": "bs-coen",
    "Three Flavours Cornetto": "bs-cornetto",
    "Detectives & Killers": "bs-dk",
    "The Lord of the Rings Collection": "bs-lotr",
}
MEMBERS = {"bs-lotr": [120, 121, 122]}


class FakeJellyfin:
    def __init__(self, movies=LIBRARY, sets=None, hidden=None, hide_polls=0):
        self._movies = movies
        self._sets = dict(BOXSETS if sets is None else sets)
        self.hidden, self.hide_polls = hidden, hide_polls
        self.polls = 0
        self.added, self.created = [], []

    def movies(self):
        self.polls += 1
        if self.polls <= self.hide_polls:
            return [m for m in self._movies if boxsets.tmdb_of(m) != self.hidden]
        return self._movies

    def boxsets(self):
        return dict(self._sets)

    def members(self, boxset_id):
        return [movie(t, "x", 2000) for t in MEMBERS.get(boxset_id, [])]

    def add_to(self, boxset_id, item_ids):
        self.added.append((boxset_id, item_ids))

    def create(self, name, item_ids):
        self.created.append((name, item_ids))


class FakeClaude:
    def __init__(self, join=(), create=(), why="", exc=None, stop_reason="end_turn"):
        self.answer = {"join": list(join), "create": list(create), "why": why}
        self.exc, self.stop_reason = exc, stop_reason
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        text = SimpleNamespace(type="text", text=json.dumps(self.answer))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])


@pytest.fixture
def defs_file(tmp_path):
    path = tmp_path / "collections.json"
    path.write_text(DEFS_TEXT)
    return path


def run(tmdb, defs_file, claude=None, jellyfin=None, **kwargs):
    jellyfin = jellyfin or FakeJellyfin()
    claude = claude or FakeClaude()
    config = {"collections": {"file": str(defs_file), "wait_seconds": 0, "min_members": 3}}
    result = boxsets.assign(
        tmdb, config, jellyfin=jellyfin, client=claude, sleep=lambda s: None, **kwargs
    )
    return result, jellyfin, claude


def saved(defs_file):
    return json.loads(defs_file.read_text())


def test_not_configured_is_a_no_op():
    assert boxsets.assign(1949, {}) is None
    assert boxsets.assign(1949, {"collections": {"file": ""}}) is None


# --- director rules ---------------------------------------------------------


def test_a_film_joins_the_collection_named_for_its_director(defs_file):
    result, jf, _ = run(1949, defs_file)

    assert result.joined == ["David Fincher"]
    assert saved(defs_file)["David Fincher"] == [807, 550, 1949]
    assert jf.added == [("bs-fincher", ["item1949"])]


def test_the_file_keeps_its_hand_kept_layout(defs_file):
    run(1949, defs_file)

    assert defs_file.read_text() == DEFS_TEXT.replace(
        '"David Fincher": [807, 550]', '"David Fincher": [807, 550, 1949]'
    )


def test_a_director_collection_is_created_at_three_films(defs_file):
    result, jf, _ = run(62, defs_file)

    assert result.created == {"Stanley Kubrick": 3}
    assert saved(defs_file)["Stanley Kubrick"] == [62, 185, 694]
    assert jf.created == [("Stanley Kubrick", ["item62", "item185", "item694"])]


def test_no_director_collection_duplicates_one_their_films_already_share(defs_file):
    # Edgar Wright's other three are the Cornetto trilogy; "Edgar Wright"
    # would be the same set plus one. Whether Baby Driver belongs with them
    # is a judgment, so it is left to Claude.
    result, _, claude = run(339403, defs_file)

    assert result.created == {}
    assert "already share a collection" in claude.calls[0]["messages"][0]["content"]


def test_a_franchise_set_counts_as_already_shared(defs_file):
    result, _, _ = run(9999, defs_file)   # Peter Jackson: the rest is LOTR

    assert result.created == {}


def test_a_co_directed_film_never_creates_a_collection_by_rule(defs_file):
    result, _, _ = run(275, defs_file)   # Fargo: Joel and Ethan Coen

    assert result.created == {}
    assert "Joel Coen" not in saved(defs_file)


# --- Claude's pass -----------------------------------------------------------


def test_claude_sees_members_and_what_the_rules_did(defs_file):
    _, _, claude = run(1949, defs_file)

    call = claude.calls[0]
    prompt = call["messages"][0]["content"]
    assert "Detectives & Killers: Se7en (1995)" in prompt
    assert 'joins "David Fincher" (director)' in prompt
    assert "at least 2 other films" in call["system"]
    schema = call["output_config"]["format"]["schema"]
    assert schema["properties"]["join"]["items"]["enum"] == [
        "David Fincher", "The Coen Brothers", "Three Flavours Cornetto", "Detectives & Killers",
    ]
    assert call["fallbacks"] == "default"


def test_claude_joins_are_added_to_the_rule_result(defs_file):
    claude = FakeClaude(join=["Detectives & Killers", "Not A Collection"], why="A serial-killer procedural.")
    result, jf, _ = run(1949, defs_file, claude)

    assert result.joined == ["David Fincher", "Detectives & Killers"]
    assert saved(defs_file)["Detectives & Killers"] == [807, 1949]
    assert ("bs-dk", ["item1949"]) in jf.added
    assert "A serial-killer procedural." in result.lines()


def test_claude_can_create_a_collection(defs_file):
    claude = FakeClaude(create=[{"name": "Serial Killers", "tmdb_ids": [807, 1949, 550, 424242]}])
    result, jf, _ = run(1949, defs_file, claude)

    # 424242 is not in the library — dropped, not trusted.
    assert result.created == {"Serial Killers": 3}
    assert saved(defs_file)["Serial Killers"] == [550, 807, 1949]
    assert ("Serial Killers", ["item550", "item807", "item1949"]) in jf.created


@pytest.mark.parametrize("proposal, why", [
    ({"name": "detectives & killers", "tmdb_ids": [807, 550, 1949]}, "name is taken"),
    ({"name": "The Lord of the Rings Collection", "tmdb_ids": [807, 550, 1949]}, "name is taken"),
    ({"name": "Nineties", "tmdb_ids": [807, 550, 275]}, "leaves out the new film"),
    ({"name": "Pair", "tmdb_ids": [1949, 807, 1, 2]}, "only 2 of its films"),
])
def test_a_bad_proposal_is_refused_not_applied(defs_file, proposal, why):
    result, jf, _ = run(1949, defs_file, FakeClaude(create=[proposal]))

    assert result.created == {}
    assert any(why in n for n in result.notes)
    assert proposal["name"] not in saved(defs_file)
    assert jf.created == []


def test_at_most_one_new_collection_from_claude(defs_file):
    claude = FakeClaude(create=[
        {"name": "First", "tmdb_ids": [807, 550, 1949]},
        {"name": "Second", "tmdb_ids": [807, 550, 1949]},
    ])
    result, _, _ = run(1949, defs_file, claude)

    assert list(result.created) == ["First"]


def test_a_claude_failure_keeps_the_director_moves(defs_file):
    result, jf, _ = run(1949, defs_file, FakeClaude(exc=RuntimeError("api down")))

    assert result.joined == ["David Fincher"]
    assert jf.added == [("bs-fincher", ["item1949"])]
    assert any("Themes skipped: api down" in n for n in result.notes)


def test_a_refusal_is_a_skip_not_an_answer(defs_file):
    claude = FakeClaude(join=["Detectives & Killers"], stop_reason="refusal")
    result, _, _ = run(1949, defs_file, claude)

    assert result.joined == ["David Fincher"]
    assert any("declined" in n for n in result.notes)


def test_no_api_key_runs_only_the_director_rules(defs_file, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = {"collections": {"file": str(defs_file), "wait_seconds": 0}}

    result = boxsets.assign(1949, config, jellyfin=FakeJellyfin(), sleep=lambda s: None)

    assert result.joined == ["David Fincher"]
    assert "Themes skipped: no ANTHROPIC_API_KEY" in result.notes


# --- waiting on Jellyfin, and failure -----------------------------------------


def test_it_waits_for_jellyfin_to_scan_the_film_in(defs_file):
    jf = FakeJellyfin(hidden=1949, hide_polls=2)
    config = {"collections": {"file": str(defs_file), "wait_seconds": 60}}
    naps = []

    result = boxsets.assign(1949, config, jellyfin=jf, client=FakeClaude(), sleep=naps.append)

    assert result.joined == ["David Fincher"]
    assert jf.polls == 3 and len(naps) == 2


def test_a_film_jellyfin_never_lists_changes_nothing(defs_file):
    jf = FakeJellyfin(hidden=1949, hide_polls=99)
    result, _, claude = run(1949, defs_file, jellyfin=jf)

    assert any("has not listed tmdb 1949" in n for n in result.notes)
    assert defs_file.read_text() == DEFS_TEXT
    assert claude.calls == [] and jf.added == []


def test_dry_run_decides_but_changes_nothing(defs_file):
    result, jf, _ = run(1949, defs_file, FakeClaude(join=["Detectives & Killers"]), dry_run=True)

    assert result.lines()[0] == "Collections: would add to David Fincher, Detectives & Killers"
    assert defs_file.read_text() == DEFS_TEXT
    assert jf.added == [] and jf.created == []


def test_an_already_listed_film_is_pushed_to_jellyfin_not_duplicated(defs_file):
    result, jf, _ = run(807, defs_file)   # Se7en is in two collections already

    assert result.joined == []
    assert defs_file.read_text() == DEFS_TEXT
    assert jf.added == [("bs-fincher", ["item807"]), ("bs-dk", ["item807"])]
    assert result.lines()[0] == (
        "Collections: already in David Fincher, Detectives & Killers; nothing to add"
    )


def test_a_jellyfin_error_leaves_the_file_updated_for_sync(defs_file):
    jf = FakeJellyfin()

    def refuse(boxset_id, item_ids):
        raise OSError("HTTP 500")

    jf.add_to = refuse
    result, _, _ = run(1949, defs_file, jellyfin=jf)

    assert saved(defs_file)["David Fincher"] == [807, 550, 1949]
    assert any("--sync will retry" in n for n in result.notes)


def test_a_collection_jellyfin_lacks_is_created_with_all_its_members(defs_file):
    sets = {k: v for k, v in BOXSETS.items() if k != "Detectives & Killers"}
    claude = FakeClaude(join=["Detectives & Killers"])
    _, jf, _ = run(1949, defs_file, claude, jellyfin=FakeJellyfin(sets=sets))

    assert jf.created == [("Detectives & Killers", ["item807", "item1949"])]


# --- the file writer ------------------------------------------------------------


def test_a_list_split_over_lines_is_still_rewritten_in_place():
    text = '{\n  "_c": "x",\n  "A": [\n    1,\n    2\n  ],\n  "B": [3]\n}\n'
    out = boxsets.render_defs(text, {"_c": "x", "A": [1, 2, 9], "B": [3]})

    assert out == '{\n  "_c": "x",\n  "A": [1, 2, 9],\n  "B": [3]\n}\n'


def test_a_new_collection_is_appended_after_the_rest():
    text = '{\n  "A": [1]\n}\n'
    out = boxsets.render_defs(text, {"A": [1], "New & Shiny": [2, 3]})

    assert out == '{\n  "A": [1],\n\n  "New & Shiny": [2, 3]\n}\n'
