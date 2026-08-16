"""Turn an IMDb link into something worth searching for.

Pasting a link is the least ambiguous way to say what you want — it names one
specific title, where "the office" or "dune" names several. Indexers do not
search by IMDb id though, so the id has to become a title and a year before
any of the search stack is any use.

Resolution goes through the same sources the rest of the project already
trusts: TVmaze for television, Wikidata for film. Both take an IMDb id
directly, so this is a lookup rather than a guess — the whole point of
accepting a link.

    expand("https://www.imdb.com/title/tt0995832/")
    -> ("Generation Kill 2008", "Generation Kill (2008, tv) via tt0995832")
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT = 15
_TVMAZE = "https://api.tvmaze.com"

# Matches a bare id or one anywhere in a URL: imdb.com/title/tt0995832/,
# m.imdb.com, a share link with query junk on the end, or just "tt0995832".
_IMDB_RE = re.compile(r"\b(tt\d{7,9})\b")


def extract_id(text: str) -> str | None:
    match = _IMDB_RE.search(text or "")
    return match.group(1) if match else None


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _lookup_tv(imdb: str) -> dict | None:
    """TVmaze indexes by IMDb id directly, so this is exact."""
    try:
        show = _get_json(f"{_TVMAZE}/lookup/shows?imdb={urllib.parse.quote(imdb)}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(show, dict) or not show.get("name"):
        return None
    premiered = str(show.get("premiered") or "")
    return {
        "kind": "tv",
        "name": show["name"],
        "year": int(premiered[:4]) if premiered[:4].isdigit() else None,
    }


def _lookup_film(imdb: str) -> dict | None:
    # scripts/ is not a package; reuse rather than reimplement the Wikidata
    # route, which already knows how to ask by IMDb id.
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import tmdb_id
    except ImportError:  # pragma: no cover - packaging accident
        return None

    try:
        candidates = tmdb_id.wikidata_by_imdb("movie", imdb)
    except Exception:
        return None
    if not candidates:
        return None
    best = candidates[0]
    return {"kind": "film", "name": best.get("name"), "year": best.get("year")}


def resolve(imdb: str) -> dict | None:
    """What this IMDb id refers to, or None if nothing recognises it.

    Television is tried first: TVmaze answers by id exactly, and a series
    resolved wrongly as a film would search for a single release rather than
    a season pack.
    """
    return _lookup_tv(imdb) or _lookup_film(imdb)


def expand(request: str) -> tuple[str, str | None]:
    """Replace an IMDb id in `request` with a searchable title.

    Returns (request_to_search, note). The note is None when nothing was
    changed, so callers can stay quiet in the ordinary case.

    An id that resolves to nothing is left alone rather than stripped: the
    search will fail, but it fails with what the user actually typed, which is
    easier to make sense of than a silently emptied query.
    """
    imdb = extract_id(request)
    if not imdb:
        return request, None

    found = resolve(imdb)
    if not found or not found.get("name"):
        return request, f"Could not resolve {imdb} — searching for it as written."

    title = str(found["name"])
    year = found.get("year")
    # Everything around the id is dropped: a pasted URL is the whole request,
    # and its path segments are not search terms.
    query = f"{title} {year}" if year else title
    note = f"{title}" + (f" ({year}" if year else " (") + f", {found['kind']}) via {imdb}"
    return query, note
