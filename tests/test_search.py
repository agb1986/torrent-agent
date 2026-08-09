"""Search normalization: magnet fallbacks, dedupe, and sentinel handling."""

from datetime import datetime, timezone

from torrent_agent import search
from torrent_agent.search import TorrentResult, _dedupe


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_get(payload):
    def get(url, **kwargs):
        return _FakeResponse(payload)
    return get


_HASH = "b" * 40


def test_prowlarr_eztv_magnet_comes_from_guid(monkeypatch):
    # EZTV via Prowlarr: magnetUrl is a localhost redirect, the real magnet is
    # in guid. Regression guard for the "Unsupported scheme: b''" bug.
    row = {
        "title": "Show S01E01 1080p",
        "magnetUrl": "http://localhost:9696/1/download?link=abc",
        "guid": f"magnet:?xt=urn:btih:{_HASH}",
        "seeders": 5,
        "leechers": 1,
        "size": 1000,
        "indexer": "EZTV",
    }
    monkeypatch.setattr(search.requests, "get", _fake_get([row]))
    results = search._search_prowlarr("q", "tv", "http://localhost:9696", "key")
    assert results[0].magnet == f"magnet:?xt=urn:btih:{_HASH}"


def test_prowlarr_magnet_rebuilt_from_info_hash(monkeypatch):
    row = {
        "title": "Show S01E01 1080p",
        "magnetUrl": "http://localhost:9696/1/download?link=abc",
        "guid": "https://example.org/torrent/123",
        "infoHash": _HASH,
        "seeders": 5,
        "leechers": 1,
        "size": 1000,
        "indexer": "1337x",
    }
    monkeypatch.setattr(search.requests, "get", _fake_get([row]))
    results = search._search_prowlarr("q", "tv", "http://localhost:9696", "key")
    assert results[0].magnet is not None
    assert results[0].magnet.startswith(f"magnet:?xt=urn:btih:{_HASH}")


def test_prowlarr_error_object_raises(monkeypatch):
    monkeypatch.setattr(
        search.requests, "get", _fake_get({"message": "all indexers failed"})
    )
    try:
        search._search_prowlarr("q", "tv", "http://localhost:9696", "key")
    except search.SearchError as exc:
        assert "all indexers failed" in str(exc)
    else:
        raise AssertionError("expected SearchError")


def test_apibay_sentinel_row_skipped(monkeypatch):
    rows = [
        {
            "info_hash": "0" * 40,
            "name": "No results returned",
            "seeders": "0",
            "leechers": "0",
            "size": "0",
            "added": "0",
        }
    ]
    monkeypatch.setattr(search.requests, "get", _fake_get(rows))
    assert search._search_apibay("q", "tv") == []


def _result(info_hash, seeders, source):
    return TorrentResult(
        title="t",
        seeders=seeders,
        leechers=0,
        size_bytes=1,
        published=datetime.now(timezone.utc),
        source=source,
        info_hash=info_hash,
        magnet=f"magnet:?xt=urn:btih:{info_hash}" if info_hash else None,
        download_url=None if info_hash else "https://example.org/t",
    )


def test_dedupe_keeps_best_seeded_per_hash():
    a = _result("c" * 40, 5, "apibay")
    b = _result("C" * 40, 50, "prowlarr:EZTV")  # same hash, different case
    out = _dedupe([a, b])
    assert len(out) == 1
    assert out[0].seeders == 50


def test_dedupe_keeps_results_without_hash():
    a = _result(None, 5, "prowlarr:1337x")
    b = _result(None, 6, "prowlarr:1337x")
    assert len(_dedupe([a, b])) == 2
