"""Selection rules for scripts/prune.py.

This is the only code in the repo that deletes a user's media, so the tests
that matter are the ones proving it declines to. Deluge's RPC hands back bytes
for keys and values, so the row fixtures go through `torrent_rows` rather than
being written as the decoded dicts the selector sees.
"""

import pytest

from prune import GB, Policy, select, torrent_rows

HOUR = 3600


def policy(**over):
    base = {"enabled": True, "min_free_gb": 100, "min_seed_hours": 72,
            "min_ratio": 1.0, "delete_data": True}
    base.update(over)
    return Policy(base)


def row(name, *, state="Seeding", ratio=2.0, hours=100, gb=10, finished=True):
    return {"id": name, "name": name, "state": state, "ratio": ratio,
            "seeding_time": int(hours * HOUR), "size": gb * GB, "finished": finished}


# --- the refusals ---------------------------------------------------------


def test_does_nothing_while_there_is_space():
    """Pruning reclaims disk. A disk with room has no reason to lose anything."""
    chosen, reason = select([row("a")], policy(), free_bytes=500 * GB)
    assert chosen == []
    assert "nothing to do" in reason


def test_only_seeding_torrents_are_candidates():
    """Paused, Error and Downloading each mean something is unresolved."""
    rows = [row("paused", state="Paused"), row("err", state="Error"),
            row("dl", state="Downloading", finished=False)]
    chosen, _ = select(rows, policy(), free_bytes=1 * GB)
    assert chosen == []


def test_unfinished_is_never_taken_even_if_seeding():
    chosen, _ = select([row("a", finished=False)], policy(), free_bytes=1 * GB)
    assert chosen == []


def test_ratio_alone_is_not_enough():
    """A popular release hits 2.0 within the hour — that is not a paid debt."""
    chosen, _ = select([row("a", ratio=9.0, hours=1)], policy(), free_bytes=1 * GB)
    assert chosen == []


def test_seed_time_alone_is_not_enough():
    """An unpopular release can sit for weeks without ever reaching the ratio."""
    chosen, _ = select([row("a", ratio=0.1, hours=1000)], policy(), free_bytes=1 * GB)
    assert chosen == []


def test_zero_seed_hours_is_refused_as_a_policy():
    """An unset threshold would make every finished torrent instantly eligible."""
    assert "min_seed_hours" in policy(min_seed_hours=0).refusal


def test_zero_free_target_is_refused():
    """A target of 0 can never be met, so the loop would empty the session."""
    assert "min_free_gb" in policy(min_free_gb=0).refusal


def test_sane_policy_has_no_refusal():
    assert policy().refusal == ""


# --- what it does take ----------------------------------------------------


def test_takes_only_enough_to_reach_the_target():
    """Stops at the target rather than clearing everything eligible."""
    rows = [row(f"t{i}", gb=30, hours=100 + i) for i in range(10)]
    chosen, _ = select(rows, policy(min_free_gb=100), free_bytes=40 * GB)
    # 60 GB short, 30 GB each -> two.
    assert len(chosen) == 2


def test_longest_seeded_goes_first():
    """Not the largest: 'reclaim it all in one go' deletes the box set."""
    rows = [row("small-old", gb=1, hours=500), row("huge-new", gb=90, hours=80)]
    chosen, _ = select(rows, policy(min_free_gb=100), free_bytes=99 * GB)
    assert [c["name"] for c in chosen] == ["small-old"]


def test_reports_when_it_cannot_free_enough():
    rows = [row("a", gb=1, hours=100)]
    chosen, reason = select(rows, policy(min_free_gb=100), free_bytes=10 * GB)
    assert chosen == [rows[0]]
    assert "short" in reason


def test_reason_names_the_thresholds_when_nothing_qualifies():
    chosen, reason = select([row("a", hours=1)], policy(), free_bytes=1 * GB)
    assert chosen == []
    assert "72h" in reason and "1.0" in reason


# --- decoding -------------------------------------------------------------


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents

    def call(self, method, state_filter, keys):
        return self.torrents


def test_torrent_rows_decodes_bytes():
    client = FakeClient({
        b"aaa": {b"name": b"Show S01", b"state": b"Seeding", b"ratio": 1.5,
                 b"seeding_time": 7200, b"total_wanted": 1024, b"is_finished": True},
    })
    rows = torrent_rows(client)
    assert rows == [{"id": "aaa", "name": "Show S01", "state": "Seeding",
                     "ratio": 1.5, "seeding_time": 7200, "size": 1024.0,
                     "finished": True}]


def test_torrent_rows_tolerates_missing_fields():
    """A torrent that has never seeded reports no ratio at all."""
    rows = torrent_rows(FakeClient({b"aaa": {b"name": b"x", b"state": b"Queued"}}))
    assert rows[0]["ratio"] == 0.0 and rows[0]["seeding_time"] == 0


def test_empty_session():
    assert torrent_rows(FakeClient({})) == []


@pytest.mark.parametrize("free", [0, 1 * GB, 99 * GB])
def test_empty_session_at_any_free_space(free):
    """A full disk with nothing prunable is a normal answer, not an error."""
    chosen, reason = select([], policy(), free_bytes=free)
    assert chosen == [] and reason
