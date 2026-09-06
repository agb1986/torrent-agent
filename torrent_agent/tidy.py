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
# Manga archives/ebooks. No season/episode structure, no TMDB-equivalent id
# space in this repo — a manga plan just routes the release, it doesn't rename
# it (see _plan_manga).
MANGA_SUFFIXES = {".cbz", ".cbr", ".zip", ".rar", ".pdf", ".epub"}

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
    kind: str                       # "tv" | "film" | "manga"
    name: str = ""
    year: int | None = None
    tmdb_id: str | None = None
    root: Path | None = None        # the directory (tv) or file (film) produced
    moves: list[Move] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    left_behind: list[Path] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        # Manga is routed, not renamed (see _plan_manga) — there is nothing to
        # move at this stage, so an empty `moves` list is the expected shape,
        # not a sign the plan is unclear.
        if self.kind == "manga":
            return not self.problems and self.root is not None
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
        if self.notes:
            lines.append("notes:")
            lines += [f"  - {n}" for n in self.notes]
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


def tvmaze_show(title: str, year: int | None = None) -> dict | None:
    """Look up a show by title, disambiguated by release year.

    `singlesearch` picks TVmaze's single best-scored match by title alone,
    which silently prefers an older, more popular show over a same-titled
    reboot/remake (e.g. "Frasier" 1993 over the 2023 revival) — wrong tmdb id,
    wrong library folder, invisible until someone watches it. `search/shows`
    returns every candidate with its premiere date, so pick the one whose
    year matches the release when we have one.
    """
    results = tvmaze_candidates(title)
    if not results:
        return None
    if year is not None:
        for show in results:
            premiered = str(show.get("premiered") or "")
            if premiered[:4].isdigit() and int(premiered[:4]) == year:
                return show
    return results[0]


def tvmaze_candidates(title: str) -> list[dict]:
    """Every show TVmaze offers for `title`, best-scored first."""
    q = urllib.parse.urlencode({"q": title})
    try:
        results = _get_json(f"{_TVMAZE}/search/shows?{q}")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [row["show"] for row in results if row.get("show")]


def tvmaze_episode_details(show_id: int) -> dict[tuple[int, int], dict]:
    """Every episode keyed by (season, number), with name and air time.

    The primitive: tidying only wants names, but subscriptions need to know
    when something aired, and one fetch serves both.
    """
    try:
        rows = _get_json(f"{_TVMAZE}/shows/{show_id}/episodes")
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    return {
        (int(e["season"]), int(e["number"])): {
            "name": e.get("name") or "",
            "airstamp": e.get("airstamp"),
        }
        for e in rows
        if e.get("season") is not None and e.get("number") is not None
    }


def tvmaze_episodes(show_id: int) -> dict[tuple[int, int], str]:
    """Episode names only — what the rename plan needs."""
    return {k: v["name"] for k, v in tvmaze_episode_details(show_id).items()}


# --- absolute (anime) episode numbering -----------------------------------

# "1088.5" is anime's convention for a recap or special sitting between two
# numbered episodes. Its digits round onto the episode before it, so it has to
# be recognised rather than parsed.
_ANIME_SPECIAL_RE = re.compile(r"(?<![\d.])\d{2,4}\.5(?!\d)")


def _broadcast_order(names: dict[tuple[int, int], str]) -> list[tuple[int, int]]:
    """Every episode in broadcast order — what an absolute number indexes.

    Sorted rather than trusted in API order so the mapping is deterministic.
    TVmaze seasons are integers under both schemes it uses — 1, 2, 3… for most
    shows, and 1999, 2000, 2001… for long-running anime it numbers by year —
    so (season, number) sorts into the order episodes aired either way.
    """
    return sorted(names)


def _absolute_number(stem: str, guess: dict) -> int | None:
    """The absolute episode number an anime-style name carries, if any.

    guessit has no concept of absolute numbering. It reads
    "One Piece - 1086 - ..." as season 10 episode 86, which resolves against no
    show, and leaves the whole token as the episode only when something else
    already looked like a group tag ("[SubsPlease] One Piece - 1086"). Either
    way the original digits are recoverable — but only claim them when they
    really do appear as one number in the name, because a plain "S02E05" would
    otherwise reconstruct to a bogus absolute 205 and map onto whatever the
    205th episode happens to be.
    """
    number = guess.get("episode")
    if not isinstance(number, int):
        return None
    season = guess.get("season")
    if season is None:
        candidate = number
    elif isinstance(season, int) and 0 <= number < 100:
        # A round absolute number splits with a zero episode: "1100" reaches
        # here as season 11, episode 0.
        candidate = season * 100 + number
    else:
        return None
    if candidate < 100:
        # Below three digits an absolute number is indistinguishable from an
        # ordinary episode number, and the season-one default already covers it.
        return None
    return candidate if re.search(rf"(?<!\d){candidate}(?!\d)", stem) else None


def _is_anime_special(stem: str, guess: dict) -> bool:
    return _absolute_number(stem, guess) is not None and bool(_ANIME_SPECIAL_RE.search(stem))


def _episode_key(
    stem: str,
    guess: dict,
    names: dict[tuple[int, int], str],
    order: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """The episode a file names, or None if this show has no such episode."""
    number = guess.get("episode")
    if not isinstance(number, int):
        return None
    season = guess.get("season")
    # A miniseries often ships bare E01..E07 with no season token at all.
    direct = (1 if season is None else season, number)
    if isinstance(direct[0], int) and direct in names:
        return direct
    absolute = _absolute_number(stem, guess)
    if absolute is not None and absolute <= len(order):
        return order[absolute - 1]
    return None


def _fit(episodes: list[tuple[Path, dict]], names: dict[tuple[int, int], str]) -> int:
    """How many of these files land on a real episode of this show."""
    order = _broadcast_order(names)
    return sum(_episode_key(f.stem, g, names, order) is not None for f, g in episodes)


def _resolve_tv_show(
    parsed_title: str, release_year: int | None, episodes: list[tuple[Path, dict]]
) -> tuple[dict | None, dict[tuple[int, int], str]]:
    """Pick the show these files belong to, and its episode list.

    `tvmaze_show` answers from the title and release year alone. Anime almost
    never carries a year, and a title two shows share then resolves to whichever
    TVmaze happened to score first: for "One Piece" that is the 2023 live-action
    series, not the 1999 anime — same name, identical score, different programme
    and a different tmdb id. The files themselves settle it, so when not one of
    them lands on an episode of the first answer, try the other shows of that
    exact name and take the one they actually fit.
    """
    show = tvmaze_show(parsed_title, release_year)
    if not show:
        return None, {}
    names = tvmaze_episodes(int(show["id"]))
    if release_year is not None or _fit(episodes, names):
        return show, names

    best, best_names, best_fit = show, names, 0
    for other in tvmaze_candidates(parsed_title):
        if int(other.get("id") or 0) == int(show["id"]):
            continue
        if (other.get("name") or "").casefold() != parsed_title.casefold():
            continue
        other_names = tvmaze_episodes(int(other["id"]))
        scored = _fit(episodes, other_names)
        if scored > best_fit:
            best, best_names, best_fit = other, other_names, scored
    return best, best_names


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


def manga_files(source: Path) -> list[Path]:
    """Every manga archive/ebook under `source`."""
    if source.is_file():
        return [source] if source.suffix.lower() in MANGA_SUFFIXES else []
    return [
        p for p in sorted(source.rglob("*"))
        if p.is_file() and p.suffix.lower() in MANGA_SUFFIXES
    ]


def _plan_manga(source: Path) -> TidyPlan:
    """Route a manga release without renaming it.

    There's no TMDB/Wikidata-equivalent id space for manga in this repo (TMDB
    doesn't catalog it), so unlike TV/film there is no external match to be
    confident or unsure about — the release is moved as one unit, under
    whatever name it already has.
    """
    return TidyPlan(kind="manga", name=source.name, root=source)


def plan_for(source: str | Path, destinations: dict[str, str] | None = None) -> TidyPlan:
    """Decide what `source` should become. Never touches the filesystem."""
    source = Path(source)
    if not source.exists():
        return TidyPlan(kind="unknown", problems=[f"{source} does not exist"])

    files = media_files(source)
    if not files:
        if manga_files(source):
            return _plan_manga(source)
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

    years = {g.get("year") for _f, g in episodes if g.get("year")}
    release_year = years.pop() if len(years) == 1 else None

    show, names = _resolve_tv_show(parsed_title, release_year, episodes)
    if not show:
        return TidyPlan(kind="tv", problems=[f"TVmaze has no match for {parsed_title!r}"])
    if not names:
        return TidyPlan(kind="tv", problems=["TVmaze returned no episode list"])
    order = _broadcast_order(names)

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
        if _is_anime_special(f.stem, g):
            # A ".5" recap has no episode number TVmaze can name, and its digits
            # round onto the episode before it — claiming it would file two
            # files as one. Leave it for a human rather than collide.
            plan.notes.append(f"{f.name}: recap special, left in place")
            continue
        key = _episode_key(f.stem, g, names, order)
        if key is None:
            absolute = _absolute_number(f.stem, g)
            plan.problems.append(
                f"{f.name}: TVmaze has no episode {absolute}"
                if absolute is not None
                else f"{f.name}: TVmaze has no S{season:02d}E{number:02d}"
            )
            continue
        season, number = key
        target = (
            plan.root
            / f"Season {season:02d}"
            / f"S{season:02d}E{number:02d} - {safe_name(names[key])}{f.suffix}"
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
