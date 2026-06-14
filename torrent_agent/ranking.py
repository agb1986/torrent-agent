"""Rank torrent results by quality, seeders, and recency.

Selection priorities, in order:
  1. Resolution preference (from config)
  2. Preferred codec (e.g. x265/HEVC)
  3. Seeders
  4. Recency

Hard filters drop results below the seeder floor and oversized single episodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from guessit import guessit

from .search import TorrentResult

_GB = 1024**3


@dataclass
class ScoredResult:
    result: TorrentResult
    resolution: str | None
    codec: str | None
    season: int | None
    episode: int | None
    score: tuple[float, ...]

    @property
    def age_days(self) -> float | None:
        if not self.result.published:
            return None
        now = datetime.now(timezone.utc)
        pub = self.result.published
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        return max(0.0, (now - pub).total_seconds() / 86400)


def _normalize_resolution(value: Any) -> str | None:
    if not value:
        return None
    return str(value).lower()


def _parse(title: str) -> dict[str, Any]:
    try:
        return dict(guessit(title))
    except Exception:  # guessit can raise on pathological titles
        return {}


def _canon_codec(value: str | None) -> str:
    # guessit returns codecs like "H.265"/"H.264"; configs say "h265"/"x265".
    # Strip non-alphanumerics so "h.265" and "h265" compare equal.
    if not value:
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _codec_matches(codec: str | None, prefer: list[str]) -> bool:
    canon = _canon_codec(codec)
    if not canon:
        return False
    return any(_canon_codec(p) in canon for p in prefer)


def _is_single_episode(info: dict[str, Any]) -> bool:
    # A specific episode number and not a season-pack/range.
    episode = info.get("episode")
    return isinstance(episode, int)


def rank(
    results: list[TorrentResult],
    preferences: dict[str, Any],
    media_type: str = "any",
) -> list[ScoredResult]:
    resolutions = [r.lower() for r in preferences.get("resolutions", [])]
    prefer_codecs = preferences.get("prefer_codecs", [])
    min_seeders = int(preferences.get("min_seeders", 0))
    max_ep_gb = float(preferences.get("max_episode_size_gb", 0) or 0)

    scored: list[ScoredResult] = []
    for res in results:
        if res.seeders < min_seeders:
            continue
        if not res.link:
            continue

        info = _parse(res.title)
        resolution = _normalize_resolution(info.get("screen_size"))
        codec = info.get("video_codec")
        codec = str(codec) if codec else None

        # Oversized single-episode filter.
        if (
            max_ep_gb > 0
            and res.size_bytes
            and _is_single_episode(info)
            and res.size_bytes > max_ep_gb * _GB
        ):
            continue

        if resolution and resolution in resolutions:
            res_score = float(len(resolutions) - resolutions.index(resolution))
        else:
            res_score = 0.0
        codec_score = 1.0 if _codec_matches(codec, prefer_codecs) else 0.0
        recency = (
            res.published.replace(tzinfo=timezone.utc).timestamp()
            if res.published and res.published.tzinfo is None
            else (res.published.timestamp() if res.published else 0.0)
        )

        scored.append(
            ScoredResult(
                result=res,
                resolution=resolution,
                codec=codec,
                season=info.get("season") if isinstance(info.get("season"), int) else None,
                episode=info.get("episode") if isinstance(info.get("episode"), int) else None,
                score=(res_score, codec_score, float(res.seeders), recency),
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
