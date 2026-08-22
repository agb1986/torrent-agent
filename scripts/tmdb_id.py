#!/usr/bin/env python3
"""Resolve a show/film to its TMDB id so Jellyfin can match it exactly.

Jellyfin reads an id straight out of the name — `One Piece (1999) [tmdbid-37854]`
— and skips its own guesswork, which is what mis-identifies long-running or
remade titles (search "One Piece" and you get the 2023 live action, not the
1999 anime).

Two sources, tried in order:

1. **TMDB itself**, if `TMDB_API_KEY` (or `[tmdb].api_key` in config.toml) is
   set. Authoritative, and the only one that knows about brand new releases.
2. **Wikidata**, keyless, via its plain API (never the query service — WDQS
   answers 502 often enough to be useless here). TMDB ids live on `P4947`
   (film) and `P4983` (TV series), so the property carrying the id also acts as
   the type filter — a film never has P4983. Given an IMDb id it is an exact
   lookup; without one it searches by title and disambiguates on year.

    python scripts/tmdb_id.py --type tv --title "One Piece" --year 1999
    python scripts/tmdb_id.py --type tv --title "One Piece" --imdb tt0388629
    python scripts/tmdb_id.py --type movie --title "Withnail and I" --tag

Exit codes: 0 resolved, 1 nothing found, 2 ambiguous (candidates are printed;
ask the user rather than guessing).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent.config import load_config

USER_AGENT = "torrent-agent/1.0 (https://github.com/agb1986/torrent-agent)"
TIMEOUT = 20

# The Wikidata property holding the TMDB id, per media type. Doubles as the
# type filter: only films carry P4947, only TV series carry P4983.
WIKIDATA_TMDB_PROPERTY = {"movie": "P4947", "tv": "P4983"}
# Date properties worth reading, most authoritative first. Films use
# publication date; series usually carry a start time as well.
WIKIDATA_DATE_PROPERTIES = {"movie": ["P577"], "tv": ["P580", "P577"]}


class LookupError_(Exception):
    """Neither source could answer — network down, or nothing matched."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get_json(url: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{query}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return json.load(response)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

def format_tag(name: str, year: int | None, tmdb_id: int | str | None) -> str:
    """Build the Jellyfin-readable name: `One Piece (1999) [tmdbid-37854]`.

    Degrades cleanly: a missing year or id just drops that part rather than
    leaving an empty `()` or `[tmdbid-]` for Jellyfin to choke on.
    """
    out = sanitise_name(name)
    if year:
        out += f" ({year})"
    if tmdb_id:
        out += f" [tmdbid-{tmdb_id}]"
    return out


def sanitise_name(name: str) -> str:
    """Strip what a filename cannot hold, and what would confuse the parser.

    `/` and the rest are illegal or awkward in a path; stray `(1999)` or
    `[tmdbid-...]` in the source name would otherwise be duplicated by the tag
    we are about to append.
    """
    name = re.sub(r"\s*\[tmdbid-[^\]]*\]", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", name)
    name = re.sub(r'[/\\:*?"<>|]', "", name)
    return re.sub(r"\s+", " ", name).strip()


def _normalise(text: str) -> str:
    """Fold a title for comparison: `Withnail & I` == `withnail and i`."""
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _year_of(date: str | None) -> int | None:
    match = re.search(r"(\d{4})", date or "")
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# Source 1 — TMDB proper (needs a key)
# --------------------------------------------------------------------------- #

def _tmdb_key(config: dict | None = None) -> str:
    config = config if config is not None else load_config()
    return config.get("tmdb", {}).get("api_key", "")


def _tmdb_result(item: dict, media_type: str) -> dict:
    name = item.get("title") if media_type == "movie" else item.get("name")
    date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
    return {"tmdb_id": item["id"], "name": name, "year": _year_of(date)}


def tmdb_lookup(
    api_key: str,
    media_type: str,
    title: str,
    year: int | None = None,
    imdb: str | None = None,
) -> list[dict]:
    """Query TMDB directly. An IMDb id gives an exact hit; a title, candidates."""
    if imdb:
        data = _get_json(
            f"https://api.themoviedb.org/3/find/{urllib.parse.quote(imdb)}",
            {"api_key": api_key, "external_source": "imdb_id"},
        )
        results = data.get(f"{media_type}_results", [])
    else:
        params = {"api_key": api_key, "query": title}
        if year:
            params["year" if media_type == "movie" else "first_air_date_year"] = str(year)
        data = _get_json(
            f"https://api.themoviedb.org/3/search/{media_type}", params
        )
        results = data.get("results", [])
    return [dict(_tmdb_result(item, media_type), source="tmdb") for item in results]


# --------------------------------------------------------------------------- #
# Source 2 — Wikidata (keyless)
# --------------------------------------------------------------------------- #

def _claim_values(entity: dict, prop: str) -> list:
    return [
        claim["mainsnak"].get("datavalue", {}).get("value")
        for claim in entity.get("claims", {}).get(prop, [])
        if claim["mainsnak"].get("datavalue")
    ]


def _entity_year(entity: dict, media_type: str) -> int | None:
    """Earliest year across the date properties — festival runs and staggered
    international releases leave several, and releases are named for the first."""
    years = [
        year
        for prop in WIKIDATA_DATE_PROPERTIES[media_type]
        for value in _claim_values(entity, prop)
        if (year := _year_of(value.get("time") if isinstance(value, dict) else None))
    ]
    return min(years) if years else None


def _wikidata_candidates(media_type: str, qids: list[str]) -> list[dict]:
    """Fetch claims for `qids` and keep the ones carrying a TMDB id.

    That property is also the type filter — only films have P4947, only series
    have P4983 — so a search for "One Piece" drops the video games and the
    franchise entity without needing to walk P31 subclass chains.
    """
    if not qids:
        return []
    entities = _get_json(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "claims|labels",
            "languages": "en",
            "format": "json",
        },
    ).get("entities", {})

    prop = WIKIDATA_TMDB_PROPERTY[media_type]
    candidates = []
    for qid in qids:  # preserve the search ranking
        entity = entities.get(qid, {})
        ids = _claim_values(entity, prop)
        if not ids:
            continue
        candidates.append(
            {
                "tmdb_id": ids[0],
                "name": entity.get("labels", {}).get("en", {}).get("value"),
                "year": _entity_year(entity, media_type),
                "source": "wikidata",
            }
        )
    return candidates


def wikidata_by_imdb(media_type: str, imdb: str) -> list[dict]:
    """Exact lookup: the entity whose IMDb id (P345) is `imdb`.

    Uses CirrusSearch's `haswbstatement:` rather than SPARQL — the query
    service (query.wikidata.org) regularly answers 502 or times out, while the
    plain API host does not.
    """
    search = _get_json(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "query",
            "list": "search",
            "srsearch": f"haswbstatement:P345={imdb}",
            "srlimit": "5",
            "format": "json",
        },
    )
    qids = [hit["title"] for hit in search.get("query", {}).get("search", [])]
    return _wikidata_candidates(media_type, qids)


def wikidata_by_title(media_type: str, title: str) -> list[dict]:
    """Search Wikidata by title, then keep entities carrying a TMDB id."""
    search = _get_json(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "wbsearchentities",
            "search": title,
            "language": "en",
            "format": "json",
            "limit": "20",
        },
    )
    qids = [hit["id"] for hit in search.get("search", [])]
    return _wikidata_candidates(media_type, qids)


# --------------------------------------------------------------------------- #
# Source 3 — a director's filmography (Wikidata only; TMDB has no equivalent
# "search by crew" endpoint coded anywhere in this repo)
# --------------------------------------------------------------------------- #

def _wikidata_person_qid(name: str) -> str | None:
    """Best-guess QID for a person by name.

    Unlike the title/imdb lookups above, there is no year or external id to
    disambiguate with — a same-named-director collision returns the wrong
    filmography, not a destructive action, so this picks the first plausible
    match rather than refusing. Prefers a hit whose description reads like a
    director over the plain best-ranked result.
    """
    search = _get_json(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "format": "json",
            "limit": "10",
        },
    )
    hits = search.get("search", [])
    if not hits:
        return None
    for hit in hits:
        if "director" in (hit.get("description") or "").lower():
            return hit["id"]
    return hits[0]["id"]


def _wikidata_films(qids: list[str]) -> list[dict]:
    """Name and year for each of `qids`. No TMDB-id filter here — P57+P31 in
    `films_by_director` already established these are films, and not every
    film Wikidata knows carries a TMDB id (P4947)."""
    if not qids:
        return []
    entities = _get_json(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "claims|labels",
            "languages": "en",
            "format": "json",
        },
    ).get("entities", {})

    films = []
    for qid in qids:  # preserve search ranking
        entity = entities.get(qid, {})
        name = entity.get("labels", {}).get("en", {}).get("value")
        if not name:
            continue
        films.append({"name": name, "year": _entity_year(entity, "movie")})
    return films


def films_by_director(name: str) -> list[dict]:
    """Every film Wikidata has on record with `name` credited as director.

    Same CirrusSearch `haswbstatement:` idiom as `wikidata_by_imdb` — P57
    (director) combined with P31=Q11424 (instance of film) so a director's TV
    work or acting credits don't leak into the result.
    """
    qid = _wikidata_person_qid(name)
    if not qid:
        return []
    search = _get_json(
        "https://www.wikidata.org/w/api.php",
        {
            "action": "query",
            "list": "search",
            "srsearch": f"haswbstatement:P57={qid} haswbstatement:P31=Q11424",
            "srlimit": "50",
            "format": "json",
        },
    )
    qids = [hit["title"] for hit in search.get("query", {}).get("search", [])]
    return _wikidata_films(qids)


# --------------------------------------------------------------------------- #
# Picking a winner
# --------------------------------------------------------------------------- #

def _score(candidate: dict, title: str, year: int | None) -> int:
    """Rank a candidate against what we asked for. Higher is better."""
    score = 0
    name = candidate.get("name") or ""
    if _normalise(name) == _normalise(title):
        score += 4
    elif _normalise(title) in _normalise(name) or _normalise(name) in _normalise(title):
        score += 1

    if year and candidate.get("year"):
        delta = abs(candidate["year"] - year)
        # Release years wobble by a year across regions; more than that and it
        # is a different title (a remake, usually) wearing the same name.
        score += 4 if delta == 0 else 2 if delta == 1 else -4
    return score


def choose(candidates: list[dict], title: str, year: int | None) -> tuple[dict | None, list[dict]]:
    """Return (best, ranked). `best` is None when the top two are tied."""
    ranked = sorted(candidates, key=lambda c: _score(c, title, year), reverse=True)
    if not ranked:
        return None, []
    if len(ranked) > 1 and _score(ranked[0], title, year) == _score(ranked[1], title, year):
        return None, ranked
    return ranked[0], ranked


def resolve(
    media_type: str,
    title: str,
    year: int | None = None,
    imdb: str | None = None,
    api_key: str = "",
) -> tuple[dict | None, list[dict]]:
    """Resolve a title to a TMDB id. Returns (best, candidates)."""
    errors = []
    if api_key:
        try:
            candidates = tmdb_lookup(api_key, media_type, title, year, imdb)
            if candidates:
                return choose(candidates, title, year)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            errors.append(f"tmdb: {exc}")

    try:
        candidates = wikidata_by_imdb(media_type, imdb) if imdb else []
        # An IMDb id that Wikidata has no TMDB link for still leaves the title
        # route worth trying.
        if not candidates:
            candidates = wikidata_by_title(media_type, title)
        if candidates:
            return choose(candidates, title, year)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        errors.append(f"wikidata: {exc}")

    if errors:
        raise LookupError_("; ".join(errors))
    return None, []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve a show or film to its TMDB id for Jellyfin naming."
    )
    parser.add_argument("--type", required=True, choices=["tv", "movie"])
    parser.add_argument("--title", required=True, help="Show or film name")
    parser.add_argument("--year", type=int, help="Release/first-air year, to disambiguate")
    parser.add_argument("--imdb", help="IMDb id (tt...), e.g. TVmaze's externals.imdb")
    parser.add_argument("--tag", action="store_true", help="Print only the Jellyfin name")
    parser.add_argument("--id-only", action="store_true", help="Print only the TMDB id")
    args = parser.parse_args()

    try:
        best, candidates = resolve(
            args.type, args.title, args.year, args.imdb, _tmdb_key()
        )
    except LookupError_ as exc:
        print(f"[ERROR] lookup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if best is None:
        if candidates:
            print(
                "[AMBIGUOUS] several equally good matches — pick one:",
                file=sys.stderr,
            )
            print(json.dumps({"candidates": candidates}, indent=2))
            sys.exit(2)
        print(f"[ERROR] no TMDB match for {args.title!r}", file=sys.stderr)
        sys.exit(1)

    name = best.get("name") or args.title
    year = best.get("year") or args.year
    if args.id_only:
        print(best["tmdb_id"])
    elif args.tag:
        print(format_tag(name, year, best["tmdb_id"]))
    else:
        print(
            json.dumps(
                {
                    "tmdb_id": best["tmdb_id"],
                    "name": name,
                    "year": year,
                    "source": best["source"],
                    "tag": format_tag(name, year, best["tmdb_id"]),
                    "candidates": candidates[1:5],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
