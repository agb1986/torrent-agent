"""Torrent search: Prowlarr (preferred) with an apibay/TPB fallback.

Both backends are normalized to ``TorrentResult`` so ranking and Deluge code
never need to care where a result came from.
"""

from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

# Public trackers stitched into magnet links built from a bare info hash.
_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
)

# Prowlarr/Newznab category roots.
_CATEGORIES = {"tv": [5000], "movie": [2000], "any": []}

_TIMEOUT = 20


@dataclass
class TorrentResult:
    title: str
    seeders: int
    leechers: int
    size_bytes: int | None
    published: datetime | None
    source: str
    info_hash: str | None = None
    magnet: str | None = None
    download_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def link(self) -> str | None:
        return self.magnet or self.download_url


class SearchError(RuntimeError):
    pass


def _build_magnet(info_hash: str, name: str) -> str:
    params = [("dn", name)] + [("tr", t) for t in _TRACKERS]
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"magnet:?xt=urn:btih:{info_hash}&{query}"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Prowlarr
# --------------------------------------------------------------------------- #
def _search_prowlarr(
    query: str, media_type: str, url: str, api_key: str
) -> list[TorrentResult]:
    endpoint = url.rstrip("/") + "/api/v1/search"
    params: list[tuple[str, str]] = [("query", query), ("type", "search")]
    for cat in _CATEGORIES.get(media_type, []):
        params.append(("categories", str(cat)))
    try:
        resp = requests.get(
            endpoint,
            params=params,
            headers={"X-Api-Key": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise SearchError(f"Prowlarr search failed: {exc}") from exc

    # When every indexer fails, Prowlarr returns an error object, not a list.
    if isinstance(rows, dict):
        raise SearchError(
            f"Prowlarr search failed: {rows.get('message', rows)}"
        )

    results: list[TorrentResult] = []
    for row in rows:
        published = None
        if raw := row.get("publishDate"):
            try:
                published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                published = None
        info_hash = row.get("infoHash") or None
        title = row.get("title", "?")
        # Prowlarr's magnetUrl is often a redirect URL (http://.../download?link=...)
        # that Deluge can't follow to a magnet. Use a value only if it's already a
        # magnet: URI (magnetUrl, then guid), otherwise rebuild one from the hash.
        magnet_field = row.get("magnetUrl") or ""
        guid = row.get("guid") or ""
        if magnet_field.startswith("magnet:"):
            magnet = magnet_field
        elif guid.startswith("magnet:"):
            magnet = guid
        elif info_hash:
            magnet = _build_magnet(info_hash, title)
        else:
            magnet = None
        results.append(
            TorrentResult(
                title=title,
                seeders=_to_int(row.get("seeders")),
                leechers=_to_int(row.get("leechers")),
                size_bytes=_to_int(row.get("size"), 0) or None,
                published=published,
                source=f"prowlarr:{row.get('indexer', '?')}",
                info_hash=info_hash,
                magnet=magnet,
                download_url=row.get("downloadUrl") or None,
            )
        )
    return results


# --------------------------------------------------------------------------- #
# apibay (The Pirate Bay JSON API)
# --------------------------------------------------------------------------- #
def _search_apibay(query: str, media_type: str) -> list[TorrentResult]:
    # cat=0 searches everything; ranking filters by parsed metadata afterwards.
    try:
        resp = requests.get(
            "https://apibay.org/q.php",
            params={"q": query, "cat": "0"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise SearchError(f"apibay search failed: {exc}") from exc

    results: list[TorrentResult] = []
    for row in rows:
        info_hash = row.get("info_hash") or ""
        # apibay returns a single sentinel row when there are no matches.
        if info_hash in ("", "0000000000000000000000000000000000000000"):
            continue
        name = row.get("name", "?")
        added = _to_int(row.get("added"), 0)
        published = (
            datetime.fromtimestamp(added, tz=timezone.utc) if added else None
        )
        results.append(
            TorrentResult(
                title=name,
                seeders=_to_int(row.get("seeders")),
                leechers=_to_int(row.get("leechers")),
                size_bytes=_to_int(row.get("size"), 0) or None,
                published=published,
                source="apibay",
                info_hash=info_hash,
                magnet=_build_magnet(info_hash, name),
            )
        )
    return results


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _dedupe(results: list[TorrentResult]) -> list[TorrentResult]:
    """Collapse the same torrent from multiple sources, keeping the best-seeded.

    Keyed by info hash; results without one are always kept (can't tell apart).
    """
    best: dict[str, TorrentResult] = {}
    out: list[TorrentResult] = []
    for r in results:
        key = r.info_hash.lower() if r.info_hash else None
        if key is None:
            out.append(r)
        elif key not in best:
            best[key] = r
            out.append(r)
        elif r.seeders > best[key].seeders:
            out[out.index(best[key])] = r
            best[key] = r
    return out


def search(query: str, media_type: str, config: dict[str, Any]) -> list[TorrentResult]:
    """Search using the configured backend.

    media_type is one of "tv", "movie", or "any". With ``backend = "auto"`` both
    Prowlarr (if configured) and apibay/TPB are queried and merged, so an episode
    that is dead on one indexer can still be found, well-seeded, on the other.
    """
    search_cfg = config.get("search", {})
    backend = search_cfg.get("backend", "auto")
    prowlarr = search_cfg.get("prowlarr", {})
    prowlarr_ready = bool(prowlarr.get("url") and prowlarr.get("api_key"))

    if backend == "prowlarr":
        if not prowlarr_ready:
            raise SearchError("Prowlarr backend selected but url/api_key not set.")
        return _search_prowlarr(query, media_type, prowlarr["url"], prowlarr["api_key"])
    if backend == "apibay":
        return _search_apibay(query, media_type)

    # auto: query every available source in parallel, merge, and tolerate one
    # failing. Sequential queries cost up to 2x _TIMEOUT before the model sees
    # anything.
    tasks = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        if prowlarr_ready:
            tasks.append(
                pool.submit(
                    _search_prowlarr,
                    query,
                    media_type,
                    prowlarr["url"],
                    prowlarr["api_key"],
                )
            )
        tasks.append(pool.submit(_search_apibay, query, media_type))

        results: list[TorrentResult] = []
        errors: list[str] = []
        for task in tasks:
            try:
                results += task.result()
            except SearchError as exc:
                errors.append(str(exc))

    if not results and errors:
        raise SearchError("; ".join(errors))
    return _dedupe(results)
