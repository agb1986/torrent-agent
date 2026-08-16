"""Following a running series.

The reconciler is the whole design: it asks "what has aired and is missing?"
rather than "is it time to run?", so a tick that never happens costs a delay
rather than a season. Most of these tests are about what it declines to do —
fetch twice, fetch early, retry too soon, or spend a budget it does not have.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server import sub

NOW = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)

# Ted Lasso season 4 as TVmaze actually has it: E01-E02 aired, E03 on the 19th.
TED = {
    (4, n): {
        "name": f"Episode {n}",
        "airstamp": (datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
                     + timedelta(days=7 * (n - 1))).isoformat(),
    }
    for n in range(1, 11)
}
TED.update({(3, n): {"name": f"S3 {n}", "airstamp": "2023-06-01T12:00:00+00:00"}
            for n in range(1, 4)})


class _Cap:
    def __init__(self, remaining=20):
        self._remaining = remaining

    def remaining(self):
        return self._remaining


class _Runner:
    """Records requests; returns a canned successful add unless told otherwise."""

    def __init__(self, ok=True, remaining=20, title="Ted Lasso S04E01 1080p WEB x264-AJP69"):
        self.cap = _Cap(remaining)
        self.requests: list[str] = []
        self.ok = ok
        self.title = title

    def run(self, request):
        self.requests.append(request)
        if not self.ok:
            return type("R", (), {"ok": False, "added": []})()
        return type("R", (), {
            "ok": True,
            "added": [{"torrent_id": f"id{len(self.requests)}", "title": self.title}],
        })()


@pytest.fixture
def store(tmp_path):
    return sub.Store(tmp_path / "subscriptions.json")


@pytest.fixture
def tvmaze(monkeypatch):
    monkeypatch.setattr(
        sub, "show_by_imdb",
        lambda i: {"id": 44458, "name": "Ted Lasso"} if i == "tt10986410" else None,
    )
    monkeypatch.setattr(sub, "tvmaze_episode_details", lambda sid: TED)


# --- subscribing ----------------------------------------------------------


def test_only_imdb_links_are_accepted(store, tvmaze):
    ok, msg = sub.subscribe("ted lasso", store, now=NOW)
    assert not ok and "IMDb link" in msg
    assert store.load() == {}


def test_subscribing_pins_the_airing_season_and_seeds_it(store, tvmaze):
    ok, msg = sub.subscribe("https://www.imdb.com/title/tt10986410/", store, now=NOW)

    assert ok, msg
    entry = store.load()["tt10986410"]
    assert entry["season"] == 4                    # not the finished season 3
    assert len(entry["episodes"]) == 10
    assert "2 episode(s) already aired" in msg
    assert "S04E03 on 2026-08-19" in msg


def test_subscribing_twice_is_refused(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    ok, msg = sub.subscribe("tt10986410", store, now=NOW)
    assert not ok and "Already following" in msg


def test_an_unknown_id_is_refused(store, tvmaze):
    ok, msg = sub.subscribe("tt0000000", store, now=NOW)
    assert not ok and "does not match" in msg


# --- reconciling ----------------------------------------------------------


def test_only_aired_episodes_are_fetched(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    runner = _Runner()

    sub.reconcile(store, runner, {}, now=NOW)

    # E01 and E02 have aired; E03 airs on the 19th and must not be touched.
    assert len(runner.requests) == 2
    assert all("S04E0" in r for r in runner.requests)
    assert not any("S04E03" in r for r in runner.requests)


def test_catch_up_is_paced(store, tvmaze, monkeypatch):
    # A show subscribed mid-season should not eat the whole daily budget at once.
    later = NOW + timedelta(days=60)
    sub.subscribe("tt10986410", store, now=later)
    runner = _Runner()

    sub.reconcile(store, runner, {}, now=later)

    assert len(runner.requests) == sub.MAX_ADDS_PER_TICK == 3


def test_an_added_episode_is_not_fetched_again(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    runner = _Runner()
    sub.reconcile(store, runner, {}, now=NOW)
    first = len(runner.requests)

    sub.reconcile(store, runner, {}, now=NOW + timedelta(hours=1))
    assert len(runner.requests) == first


def test_a_failure_retries_after_twelve_hours_not_sooner(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    runner = _Runner(ok=False)

    sub.reconcile(store, runner, {}, now=NOW)
    attempted = len(runner.requests)
    assert attempted == 2

    sub.reconcile(store, runner, {}, now=NOW + timedelta(hours=6))
    assert len(runner.requests) == attempted, "retried before the window elapsed"

    sub.reconcile(store, runner, {}, now=NOW + timedelta(hours=13))
    assert len(runner.requests) > attempted


def test_the_daily_cap_defers_rather_than_dropping(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    runner = _Runner(remaining=0)

    actions = sub.reconcile(store, runner, {}, now=NOW)

    assert runner.requests == []
    assert any(a.kind == "capped" for a in actions)
    # Still pending, so tomorrow's tick picks it up.
    entry = store.load()["tt10986410"]
    assert entry["episodes"]["S04E01"]["state"] == "pending"


def test_quality_is_learned_from_the_first_add_and_reused(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    runner = _Runner(title="Ted Lasso S04E01 1080p ATVP WEB-DL DDP5.1 H.264-NTb")

    sub.reconcile(store, runner, {}, now=NOW)

    quality = store.load()["tt10986410"]["quality"]
    assert quality["resolution"] == "1080p"
    assert quality["group"] == "NTb"
    # The second request in the same tick already carries the profile.
    assert "1080p" in runner.requests[1] and "NTb" in runner.requests[1]


def test_a_finished_season_unsubscribes(store, tvmaze, monkeypatch):
    monkeypatch.setattr(sub, "MAX_ADDS_PER_TICK", 50)
    later = NOW + timedelta(days=90)          # every episode has aired
    sub.subscribe("tt10986410", store, now=later)
    runner = _Runner()

    actions = sub.reconcile(store, runner, {}, now=later)

    assert any(a.kind == "complete" for a in actions)
    assert store.load() == {}, "should stop following once the season is done"


def test_a_stuck_episode_alerts_once(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    runner = _Runner(ok=False)

    alerts = 0
    at = NOW
    for _ in range(8):
        actions = sub.reconcile(store, runner, {}, now=at)
        alerts += sum(1 for a in actions if a.kind == "stuck")
        at += timedelta(hours=13)

    assert alerts == 2, "one alert per stuck episode (E01 and E02), not per attempt"


def test_new_episodes_appearing_later_are_picked_up(store, tvmaze, monkeypatch):
    sub.subscribe("tt10986410", store, now=NOW)
    extended = dict(TED)
    extended[(4, 11)] = {"name": "Bonus", "airstamp": "2026-10-14T12:00:00+00:00"}
    monkeypatch.setattr(sub, "tvmaze_episode_details", lambda sid: extended)

    sub.reconcile(store, _Runner(), {}, now=NOW)

    assert "S04E11" in store.load()["tt10986410"]["episodes"]


# --- managing -------------------------------------------------------------


def test_unsubscribe(store, tvmaze):
    sub.subscribe("tt10986410", store, now=NOW)
    ok, msg = sub.unsubscribe("tt10986410", store)
    assert ok and "Ted Lasso" in msg
    assert store.load() == {}


def test_unsubscribe_something_not_followed(store):
    ok, msg = sub.unsubscribe("tt9999999", store)
    assert not ok and "Not following" in msg


def test_list_is_readable(store, tvmaze):
    assert "Not following anything" in sub.format_list(store)
    sub.subscribe("tt10986410", store, now=NOW)
    text = sub.format_list(store)
    assert "Ted Lasso" in text and "season 4" in text and "0/10 fetched" in text
