"""Target selection for scripts/remove_seeding.py.

The safety-critical bit: `--id` must remove exactly what it was given and
nothing else, whatever state those torrents are in. Deluge's RPC hands back
bytes for both keys and values, so the fixtures below are deliberately bytes.
"""

import pytest

from remove_seeding import select_targets


class FakeClient:
    """Stand-in for DelugeRPCClient recording the filter it was called with."""

    def __init__(self, torrents):
        self.torrents = torrents
        self.calls = []

    def call(self, method, state_filter, keys):
        self.calls.append((method, state_filter, keys))
        if not state_filter:
            return self.torrents
        state = state_filter.get("state")
        return {
            tid: info
            for tid, info in self.torrents.items()
            if info.get(b"state") == state.encode()
        }


@pytest.fixture
def client():
    return FakeClient(
        {
            b"aaa": {b"name": b"Seeding Show S01E01", b"state": b"Seeding"},
            b"bbb": {b"name": b"Tidied Show S01E04", b"state": b"Error"},
            b"ccc": {b"name": b"Still Downloading", b"state": b"Downloading"},
        }
    )


def test_no_ids_selects_only_seeding(client):
    assert select_targets(client) == {"aaa": "Seeding Show S01E01"}


def test_ids_ignore_state(client):
    """A tidied torrent sits in Error, not Seeding — it must still be found."""
    assert select_targets(client, ["bbb"]) == {"bbb": "Tidied Show S01E04"}
    assert client.calls[0][1] == {}, "id mode must not send a state filter"


def test_ids_select_nothing_else(client):
    """The pipeline's cleanup must never touch unrelated torrents."""
    targets = select_targets(client, ["bbb"])
    assert "aaa" not in targets and "ccc" not in targets


def test_unknown_id_is_absent_not_an_error(client):
    assert select_targets(client, ["deadbeef"]) == {}


def test_id_match_is_case_insensitive(client):
    assert select_targets(client, ["BBB"]) == {"bbb": "Tidied Show S01E04"}


def test_multiple_ids(client):
    assert select_targets(client, ["aaa", "ccc"]) == {
        "aaa": "Seeding Show S01E01",
        "ccc": "Still Downloading",
    }


def test_missing_name_does_not_crash():
    bare = FakeClient({b"aaa": {b"state": b"Seeding"}})
    assert select_targets(bare, ["aaa"]) == {"aaa": "<unknown>"}


def test_empty_deluge_returns_nothing():
    assert select_targets(FakeClient({})) == {}
