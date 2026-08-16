"""Work out how a finished download should be named, and rename it.

This existed only as the `tidy-files` skill — a procedure a human (or a model)
followed by hand, making judgement calls as it went. Automating delivery means
those judgements have to be code, and the important half of that is knowing
when *not* to act.

The rule throughout: produce a plan, and mark it confident only when every
piece is known. An unconfident plan changes nothing and is escalated to the
user instead. A wrong guess here writes a mis-titled show into a real media
library, where Jellyfin will happily fetch metadata for the wrong programme and
the mistake outlives the download by years — so silence is much cheaper than a
guess.

    from torrent_agent.tidy import plan_for, execute
    plan = plan_for("/mnt/data/downloads/Some.Release")
    if plan.confident:
        execute(plan)
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from guessit import guessit

MEDIA_SUFFIXES = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}
SUBTITLE_SUFFIXES = {".srt", ".sub", ".ass", ".ssa", ".idx"}

_TVMAZE = "https://api.tvmaze.com"
_TIMEOUT = 20

# Small enough to be junk, or a sample: never the feature. Release groups ship
# "sample.mkv" alongside the real file, and tidying the sample instead is a
# quiet way to deliver 40 seconds of a film.
_MIN_MEDIA_BYTES = 50 * 1024 * 1024


@dataclass
class Move:
    source: Path
    target: Path


@dataclass
class TidyPlan:
    kind: str                       # "tv" | "film"
    name: str = ""
    year: int | None = None
    tmdb_id: str | None = None
    root: Path | None = None        # the directory (tv) or file (film) produced
    moves: list[Move] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    left_behind: list[Path] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return not self.problems and bool(self.moves)

    def describe(self) -> str:
        head = f"{self.name} ({self.year})" if self.year else self.name
        if self.tmdb_id:
            head += f" [tmdbid-{self.tmdb_id}]"
        lines = [f"{self.kind}: {head}", f"{len(self.moves)} file(s)"]
        lines += [f"  {m.source.name}  ->  {m.target.name}" for m in self.moves[:10]]
        if len(self.moves) > 10:
            lines.append(f"  …and {len(self.moves) - 10} more")
        if self.problems:
            lines.append("problems:")
            lines += [f"  - {p}" for p in self.problems]
        return "\n".join(lines)


# --- helpers --------------------------------------------------------------


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def media_files(source: Path) -> list[Path]:
    """Every real media file under `source`, largest first.

    Ordering matters for films: a release directory may hold a sample and an
    extra, and the feature is the big one.
    """
    if source.is_file():
        return [source] if source.suffix.lower() in MEDIA_SUFFIXES else []
    found = [
        p
        for p in sorted(source.rglob("*"))
        if p.is_file()
        and p.suffix.lower() in MEDIA_SUFFIXES
        and p.stat().st_size >= _MIN_MEDIA_BYTES
    ]
    return sorted(found, key=lambda p: p.stat().st_size, reverse=True)


def safe_name(text: str) -> str:
    """Strip what a path cannot hold, without mangling the title."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "", text).strip().rstrip(".")
    return re.sub(r"\s+", " ", cleaned)


def tvmaze_show(title: str) -> dict | None:
    q = urllib.parse.urlencode({"q": title})
    try:
        return _get_json(f"{_TVMAZE}/singlesearch/shows?{q}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def tvmaze_episodes(show_id: int) -> dict[tuple[int, int], str]:
    try:
        rows = _get_json(f"{_TVMAZE}/shows/{show_id}/episodes")
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    return {
        (int(e["season"]), int(e["number"])): e.get("name") or ""
        for e in rows
        if e.get("season") is not None and e.get("number") is not None
    }


def _resolve_tmdb(media_type: str, title: str, year: int | None, imdb: str | None):
    """Ask scripts/tmdb_id.py. Returns (tmdb_id, canonical_name, year, problem)."""
    import sys

    root = Path(__file__).resolve().parent.parent
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    try:
        import tmdb_id
    except ImportError as exc:  # pragma: no cover - packaging accident
        return None, title, year, f"could not load tmdb_id ({exc})"

    try:
        best, candidates = tmdb_id.resolve(media_type, title, year, imdb)
    except Exception as exc:
        return None, title, year, f"TMDB/Wikidata lookup failed: {exc}"

    if best is None:
        if candidates:
            names = ", ".join(
                f"{c.get('name')} ({c.get('year')})" for c in candidates[:4]
            )
            # Ambiguity is the dangerous case, not the absent one: picking
            # between a film and its remake wrongly is invisible until someone
            # watches it.
            return None, title, year, f"ambiguous TMDB match — candidates: {names}"
        # No match at all is survivable: tag-less naming still works, Jellyfin
        # just falls back to guessing as it did before any of this existed.
        return None, title, year, ""
    return (
        str(best.get("tmdb_id")),
        best.get("name") or title,
        best.get("year") or year,
        "",
    )


# --- planning -------------------------------------------------------------


def plan_for(source: str | Path, destinations: dict[str, str] | None = None) -> TidyPlan:
    """Decide what `source` should become. Never touches the filesystem."""
    source = Path(source)
    if not source.exists():
        return TidyPlan(kind="unknown", problems=[f"{source} does not exist"])

    files = media_files(source)
    if not files:
        return TidyPlan(
            kind="unknown",
            problems=[f"no media files over {_MIN_MEDIA_BYTES // (1024*1024)}MB in {source}"],
        )

    guesses = [(f, guessit(f.name)) for f in files]
    episodes = [(f, g) for f, g in guesses if g.get("type") == "episode"]

    # A directory holding both is not something to resolve automatically.
    if episodes and len(episodes) != len(guesses):
        return TidyPlan(
            kind="unknown",
            problems=["directory mixes episodes and films — needs a human"],
        )
    return (
        _plan_tv(source, episodes)
        if episodes
        else _plan_film(source, guesses[0][0], guesses[0][1])
    )


def _plan_tv(source: Path, episodes: list[tuple[Path, dict]]) -> TidyPlan:
    titles = {str(g.get("title") or "").strip() for _f, g in episodes}
    titles.discard("")
    if len(titles) != 1:
        return TidyPlan(
            kind="tv",
            problems=[f"files disagree on the show name: {sorted(titles)}"],
        )
    parsed_title = titles.pop()

    show = tvmaze_show(parsed_title)
    if not show:
        return TidyPlan(kind="tv", problems=[f"TVmaze has no match for {parsed_title!r}"])

    names = tvmaze_episodes(int(show["id"]))
    if not names:
        return TidyPlan(kind="tv", problems=["TVmaze returned no episode list"])

    premiered = str(show.get("premiered") or "")
    year = int(premiered[:4]) if premiered[:4].isdigit() else None
    imdb = (show.get("externals") or {}).get("imdb")

    tmdb, canonical, year, problem = _resolve_tmdb("tv", show.get("name") or parsed_title, year, imdb)
    plan = TidyPlan(kind="tv", name=canonical, year=year, tmdb_id=tmdb)
    if problem:
        plan.problems.append(problem)

    tag = safe_name(f"{canonical} ({year})" if year else canonical)
    if tmdb:
        tag += f" [tmdbid-{tmdb}]"
    plan.root = source.parent / tag

    seen: set[Path] = set()
    for f, g in sorted(episodes, key=lambda x: x[0].name):
        # A miniseries often ships bare E01..E07 with no season token at all.
        season = g.get("season")
        season = 1 if season is None else season
        number = g.get("episode")
        if isinstance(number, list):
            plan.problems.append(f"{f.name}: multi-episode file, needs a human")
            continue
        if not isinstance(season, int) or not isinstance(number, int):
            plan.problems.append(f"{f.name}: could not read season/episode")
            continue
        title = names.get((season, number))
        if not title:
            plan.problems.append(f"{f.name}: TVmaze has no S{season:02d}E{number:02d}")
            continue
        target = (
            plan.root
            / f"Season {season:02d}"
            / f"S{season:02d}E{number:02d} - {safe_name(title)}{f.suffix}"
        )
        if target in seen:
            plan.problems.append(f"two files map to {target.name}")
            continue
        seen.add(target)
        plan.moves.append(Move(source=f, target=target))

    plan.left_behind = _unclaimed(source, plan)
    return plan


def _plan_film(source: Path, media: Path, guess: dict) -> TidyPlan:
    title = str(guess.get("title") or "").strip()
    year = guess.get("year")
    if not title:
        return TidyPlan(kind="film", problems=[f"could not read a title from {media.name}"])
    if not isinstance(year, int):
        # The year separates a film from its remake; without it the TMDB match
        # is a coin toss, so escalate rather than guess.
        return TidyPlan(kind="film", name=title, problems=[f"no year in {media.name}"])

    tmdb, canonical, year, problem = _resolve_tmdb("movie", title, year, None)
    plan = TidyPlan(kind="film", name=canonical, year=year, tmdb_id=tmdb)
    if problem:
        plan.problems.append(problem)

    base = safe_name(f"{canonical} ({year})")
    if tmdb:
        base += f" [tmdbid-{tmdb}]"
    parent = source.parent if source.is_dir() else source.parent
    plan.root = parent / f"{base}{media.suffix}"
    plan.moves.append(Move(source=media, target=plan.root))
    plan.left_behind = _unclaimed(source, plan)
    return plan


def _unclaimed(source: Path, plan: TidyPlan) -> list[Path]:
    if source.is_file():
        return []
    claimed = {m.source for m in plan.moves}
    return [p for p in sorted(source.rglob("*")) if p.is_file() and p not in claimed]


# --- execution ------------------------------------------------------------


def execute(plan: TidyPlan) -> list[Move]:
    """Carry out a confident plan. Refuses anything else."""
    if not plan.confident:
        raise ValueError(f"refusing to execute an unconfident plan: {plan.problems}")
    done = []
    for move in plan.moves:
        move.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), str(move.target))
        done.append(move)
    return done
