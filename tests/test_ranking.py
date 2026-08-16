"""Ranking: codec canonicalization, hard filters, and score ordering."""

# Keeps `str | None` in the helper signatures below from being evaluated at
# import time, so the suite runs on the server's Python 3.9 as well as 3.12.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from torrent_agent.ranking import rank
from torrent_agent.search import TorrentResult

_NOW = datetime.now(timezone.utc)


def _result(title: str, seeders: int = 10, size_gb: float | None = 2.0,
            age_days: float = 1.0) -> TorrentResult:
    return TorrentResult(
        title=title,
        seeders=seeders,
        leechers=0,
        size_bytes=int(size_gb * 1024**3) if size_gb else None,
        published=_NOW - timedelta(days=age_days),
        source="test",
        info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
    )


PREFS = {
    "resolutions": ["1080p", "2160p", "720p"],
    "prefer_codecs": ["x265", "hevc", "h265"],
    "max_episode_size_gb": 20,
    "min_seeders": 1,
}


def test_codec_matches_across_spellings():
    # guessit emits "H.265"; config says "h265"/"x265" — they must compare equal.
    scored = rank([_result("Show.S01E01.1080p.WEB-DL.H.265-GRP")], PREFS, "tv")
    assert scored[0].codec is not None
    assert scored[0].score[1] == 1.0


def test_preferred_resolution_ranks_first():
    scored = rank(
        [
            _result("Show.S01E01.720p.WEB.x265-GRP"),
            _result("Show.S01E01.1080p.WEB.x265-GRP"),
        ],
        PREFS,
        "tv",
    )
    assert scored[0].resolution == "1080p"


def test_seeder_floor_drops_dead_torrents():
    prefs = dict(PREFS, min_seeders=5)
    scored = rank([_result("Show.S01E01.1080p.WEB.x265", seeders=2)], prefs, "tv")
    assert scored == []


def test_oversized_single_episode_dropped_but_pack_kept():
    episode = _result("Show.S01E01.1080p.WEB.x265", size_gb=25)
    pack = _result("Show.Season.1.Complete.1080p.WEB.x265", size_gb=25)
    scored = rank([episode, pack], PREFS, "tv")
    titles = [s.result.title for s in scored]
    assert episode.title not in titles
    assert pack.title in titles


def test_cam_releases_dropped_for_movies():
    cam = _result("Some.Movie.2024.1080p.HDCAM.x264")
    ts = _result("Some.Movie.2024.1080p.HDTS.x264")
    web = _result("Some.Movie.2024.1080p.WEB-DL.x265")
    scored = rank([cam, ts, web], PREFS, "movie")
    titles = [s.result.title for s in scored]
    assert titles == [web.title]


def test_cam_releases_kept_for_tv_media_type():
    # The hard drop only applies to movie searches.
    cam = _result("Some.Movie.2024.1080p.HDCAM.x264")
    assert rank([cam], PREFS, "tv")


def test_proper_breaks_ties_below_seeders():
    proper = _result("Show.S01E01.1080p.WEB.x265.PROPER-GRP", seeders=10)
    original = _result("Show.S01E01.1080p.WEB.x265-GRP", seeders=10)
    better_seeded = _result("Show.S01E01.1080p.WEB.x265-OTHER", seeders=500)
    scored = rank([original, proper, better_seeded], PREFS, "tv")
    titles = [s.result.title for s in scored]
    # Seeders still dominate; proper only wins the tie.
    assert titles[0] == better_seeded.title
    assert titles[1] == proper.title


def test_results_without_link_are_dropped():
    r = _result("Show.S01E01.1080p.WEB.x265")
    r.magnet = None
    r.download_url = None
    assert rank([r], PREFS, "tv") == []


def test_season_episode_parsed():
    scored = rank([_result("Show.S02E05.1080p.WEB.x265")], PREFS, "tv")
    assert scored[0].season == 2
    assert scored[0].episode == 5
