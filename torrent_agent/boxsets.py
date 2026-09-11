"""Put a newly delivered film into Jellyfin collections (box sets).

The collections themselves are the jellyfin-collections skill's
`collections.json`: a collection name mapped to TMDB ids. This reads and
extends that same file, so the skill's `sync_collections.py --sync` and the
pipeline never disagree about what a collection contains.

Two passes, in this order:

1. **Director rules**, from the directors Jellyfin records for the film. A
   collection named exactly after the director gets the film. Otherwise, once
   the library holds `min_members` of their films, a collection is created for
   them — unless their other films already sit together in one collection
   (Edgar Wright's three are the Cornetto trilogy, Peter Jackson's are LOTR),
   where a director collection would only duplicate it. Co-directed films
   never create one by rule: two directors would make two identical sets.
2. **Claude**, for everything a name cannot match: themed collections
   ("Mind Benders", "British & Irish"), team collections ("The Coen
   Brothers"), and whether this film starts a new one. It sees every
   collection's current members, so it judges fit by what is in a collection
   rather than by its name. Its answer is checked, not trusted: a new
   collection needs `min_members` films that are really in the library, the
   new film among them, and a name nothing else uses.

Membership is cheap to undo and does not move a file, so unlike tidy this is
allowed to use judgment. It is still best-effort: nothing here can fail a
delivery, and a Jellyfin error leaves the file updated so `--sync` can finish
the job later.

    python -m torrent_agent.boxsets 1949 --dry-run   # what would happen
    python -m torrent_agent.boxsets 1949 65754       # assign existing films
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("torrent_agent.boxsets")

# Same reason as transfer.py's _DIRECT: the bot's process carries gluetun's
# HTTP proxy in its environment, and Jellyfin is on the LAN.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_POLL_SECONDS = 10


@dataclass
class Assignment:
    tmdb_id: int
    film: str = ""
    joined: list[str] = field(default_factory=list)
    created: dict[str, int] = field(default_factory=dict)   # name -> member count
    listed: list[str] = field(default_factory=list)          # already a member
    why: str = ""
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False

    def lines(self) -> list[str]:
        """What the delivery summary says about collections."""
        verb = "would add to" if self.dry_run else "added to"
        out = []
        if self.joined:
            out.append(f"Collections: {verb} {', '.join(self.joined)}")
        for name, count in self.created.items():
            made = "Would create" if self.dry_run else "New collection"
            out.append(f'{made} "{name}" ({count} films)')
        if self.film and not self.joined and not self.created:
            # "Fits none" about a film already in collections reads as if it
            # had been taken out of them.
            if self.listed:
                out.append(f"Collections: already in {', '.join(self.listed)}; nothing to add")
            else:
                out.append(f"Collections: {self.film} fits none")
        if self.why:
            out.append(self.why)
        return out + self.notes


# --------------------------------------------------------------------------- #
# Jellyfin
# --------------------------------------------------------------------------- #

class Jellyfin:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def _call(self, path: str, params: dict | None = None, method: str = "GET") -> Any:
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(
            url,
            data=b"" if method == "POST" else None,
            method=method,
            headers={"X-Emby-Token": self.api_key},
        )
        with _DIRECT.open(req, timeout=60) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def _items(self, **params: str) -> list[dict]:
        params.setdefault("recursive", "true")
        params.setdefault("limit", "5000")
        return self._call("/Items", params).get("Items", [])

    def movies(self) -> list[dict]:
        return self._items(
            includeItemTypes="Movie",
            fields="ProviderIds,People,Genres,Tags,ProductionLocations,Overview",
        )

    def boxsets(self) -> dict[str, str]:
        return {b["Name"]: b["Id"] for b in self._items(includeItemTypes="BoxSet")}

    def members(self, boxset_id: str) -> list[dict]:
        return self._items(
            parentId=boxset_id, includeItemTypes="Movie", fields="ProviderIds"
        )

    def add_to(self, boxset_id: str, item_ids: list[str]) -> None:
        # Jellyfin skips items already in the set, so this is safe to repeat.
        self._call(f"/Collections/{boxset_id}/Items",
                   {"ids": ",".join(item_ids)}, method="POST")

    def create(self, name: str, item_ids: list[str]) -> None:
        self._call("/Collections", {"name": name, "ids": ",".join(item_ids)},
                   method="POST")


def tmdb_of(item: dict) -> int | None:
    value = (item.get("ProviderIds") or {}).get("Tmdb")
    return int(value) if value and str(value).isdigit() else None


def directors(item: dict) -> list[str]:
    return [p["Name"] for p in item.get("People") or [] if p.get("Type") == "Director"]


def label(item: dict) -> str:
    year = item.get("ProductionYear")
    return f"{item.get('Name', '?')} ({year})" if year else item.get("Name", "?")


def wait_for_film(
    jellyfin: Jellyfin,
    tmdb_id: int,
    wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict | None, list[dict]]:
    """The film's library item, once Jellyfin has scanned it in.

    The scan runs in the background after /Library/Media/Updated returns, so
    the item turns up some seconds after delivery — and its directors and
    genres (fetched from TMDB) a little after that. Waits for both; if the
    item appears but its metadata never does, it is returned anyway, since
    Claude still knows a film by its title and year.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        movies = jellyfin.movies()
        film = next((m for m in movies if tmdb_of(m) == tmdb_id), None)
        described = film is not None and (directors(film) or film.get("Genres"))
        if described or time.monotonic() >= deadline:
            return film, movies
        sleep(_POLL_SECONDS)


# --------------------------------------------------------------------------- #
# collections.json
# --------------------------------------------------------------------------- #

def load_defs(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _entry(name: str, ids: list[int]) -> str:
    return f"{json.dumps(name, ensure_ascii=False)}: {json.dumps(ids)}"


def render_defs(text: str, defs: dict[str, Any]) -> str:
    """`defs` as file text, keeping the hand-kept layout of `text`.

    The file is edited by hand — one collection per line, blank lines between
    groups — and a plain json.dump would explode every list onto one id per
    line. So changed lists are rewritten where they stand and new ones are
    appended; if the result does not parse back to `defs`, fall back to a
    one-line-per-collection dump, which is at least readable.
    """
    before = json.loads(text)
    out = text
    for name, ids in defs.items():
        if name.startswith("_") or before.get(name) == ids:
            continue
        if name in before:
            key = re.escape(json.dumps(name, ensure_ascii=False))
            pattern = re.compile(r"(?m)^([ \t]*)" + key + r"\s*:\s*\[[^\]]*\]")
            out, count = pattern.subn(
                lambda m: m.group(1) + _entry(name, ids), out, count=1
            )
            if count != 1:
                break
        else:
            close = out.rstrip().rfind("}")
            out = out[:close].rstrip() + ",\n\n  " + _entry(name, ids) + "\n" + out[close:]
    try:
        if json.loads(out) == defs:
            return out
    except ValueError:
        pass
    body = ",\n".join(
        f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
        for k, v in defs.items()
    )
    return "{\n" + body + "\n}\n"


def save_defs(path: Path, defs: dict[str, Any]) -> None:
    text = render_defs(path.read_text(), defs)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def custom(defs: dict[str, Any]) -> dict[str, list[int]]:
    return {k: v for k, v in defs.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- #
# Pass 1 — directors
# --------------------------------------------------------------------------- #

def director_moves(
    film: dict,
    movies: list[dict],
    collections: dict[str, list[int]],
    franchises: dict[str, list[int]],
    min_members: int,
) -> tuple[list[str], dict[str, list[int]], list[str]]:
    """(collections to join, collections to create, what was decided and why)."""
    tmdb = tmdb_of(film)
    names = directors(film)
    join: list[str] = []
    create: dict[str, list[int]] = {}
    notes: list[str] = []
    groups = [set(ids) for ids in collections.values()] + [set(ids) for ids in franchises.values()]

    for name in names:
        if name in collections:
            join.append(name)
            notes.append(f'joins "{name}" (director)')
            continue
        theirs = [t for m in movies if name in directors(m) and (t := tmdb_of(m))]
        others = {t for t in theirs if t != tmdb}
        if len(theirs) < min_members:
            notes.append(f"{name}: {len(theirs)} film(s) in the library, "
                         f"no collection until {min_members}")
        elif len(names) > 1:
            notes.append(f"{name}: co-directed, so no director collection by rule")
        elif others and any(others <= g for g in groups):
            notes.append(f"{name}: other films already share a collection, "
                         f"so not creating one by rule")
        else:
            create[name] = sorted(theirs)
            notes.append(f'creates "{name}" ({len(theirs)} films, director)')
    if not names:
        notes.append("Jellyfin lists no director")
    return join, create, notes


# --------------------------------------------------------------------------- #
# Pass 2 — Claude
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You curate the custom collections (box sets) of a personal Jellyfin film \
library. A film has just been added. Decide which existing custom collections \
it belongs in, and whether it should start a new one.

How the collections work:
- A custom collection is a named list of films. Its members show what it is \
about, so judge by them rather than by the name alone: "British & Irish" is \
about where a film comes from, "Mind Benders" about how it plays with the \
viewer.
- Director collections are handled by rules before you see the film, and \
their result is given to you. Don't repeat it. A collection named for a \
directing team (like "The Coen Brothers") is invisible to those rules, so \
that one is yours to decide.
- Franchise collections are made automatically by Jellyfin from TMDB. Never \
create one that duplicates them.

What to return:
- join: every existing custom collection the film clearly belongs in. Ask \
whether someone browsing that collection would expect to find this film \
there. An empty list is a fine answer; don't push a film into the nearest \
thing because nothing fits.
- create: a new collection only when this film and at least {others} other \
films already in the library form a clear group that no existing collection \
covers (a director or team, a franchise Jellyfin missed, a distinct theme). \
List every library film that belongs in it by tmdb id, the new film \
included. Name it in the style of the existing collections. Most additions \
create nothing, and never more than one.
- why: one or two plain sentences for the library's owner, explaining the \
placements, or why there are none."""


def _describe(item: dict) -> str:
    who = ", ".join(directors(item)) or "director unknown"
    genres = ", ".join(item.get("Genres") or [])
    return f"{tmdb_of(item)} | {label(item)} | {who} | {genres}"


def build_prompt(
    film: dict,
    movies: list[dict],
    collections: dict[str, list[int]],
    franchises: dict[str, list[int]],
    rule_notes: list[str],
) -> str:
    by_tmdb = {tmdb_of(m): m for m in movies}
    tmdb = tmdb_of(film)

    def members(ids: list[int]) -> str:
        return "; ".join(label(by_tmdb[t]) for t in ids if t in by_tmdb) or "(none in library)"

    listed = [n for n, ids in collections.items() if tmdb in ids]
    parts = [
        "New film:",
        f"  {_describe(film)}",
        f"  Countries: {', '.join(film.get('ProductionLocations') or []) or 'unknown'}",
        f"  Tags: {', '.join((film.get('Tags') or [])[:20]) or 'none'}",
        f"  Overview: {film.get('Overview') or 'none'}",
        f"  Already listed in: {', '.join(listed) or 'nothing'}",
        "",
        "Director rules: " + ("; ".join(rule_notes) or "nothing to do"),
        "",
        "Custom collections:",
        *(f"  {name}: {members(ids)}" for name, ids in collections.items()),
        "",
        "Franchise collections Jellyfin manages itself:",
        *(f"  {name}: {members(ids)}" for name, ids in franchises.items()),
        "",
        "Library (tmdb | title | directors | genres):",
        *(f"  {_describe(m)}" for m in sorted(movies, key=label) if tmdb_of(m)),
    ]
    return "\n".join(parts)


def _schema(names: list[str]) -> dict:
    # `why` first, and every field described: last and undescribed, it once
    # came back as the literal word "placeholder" — and it is the one field
    # the owner reads on their phone.
    name = {"type": "string", "enum": names} if names else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "why": {
                "type": "string",
                "description": "One or two sentences for the library's owner "
                               "explaining where the film went and why, or why "
                               "it fits nowhere.",
            },
            "join": {
                "type": "array",
                "items": name,
                "description": "Existing custom collections the film belongs in.",
            },
            "create": {
                "type": "array",
                "description": "A new collection, only when clearly warranted. "
                               "Usually empty.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "tmdb_ids": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["name", "tmdb_ids"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["why", "join", "create"],
        "additionalProperties": False,
    }


def ask_claude(
    client: Any,
    model: str,
    prompt: str,
    names: list[str],
    min_members: int,
) -> dict:
    """Claude's placement, as a dict matching _schema. Raises on failure."""
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": _schema(names)},
        },
        # A declined request is retried server-side on the recommended
        # fallback model rather than coming back as a refusal.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT.replace("{others}", str(min_members - 1)),
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


def _claude_client() -> Any:
    import anthropic

    return anthropic.Anthropic(timeout=180.0)


# --------------------------------------------------------------------------- #
# The whole thing
# --------------------------------------------------------------------------- #

def assign(
    tmdb_id: int | str,
    config: dict[str, Any],
    *,
    jellyfin: Jellyfin | None = None,
    client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
    wait_seconds: float | None = None,
) -> Assignment | None:
    """Assign one delivered film to collections. None when not configured.

    Never raises: a box set is not worth a failed delivery.
    """
    settings = config.get("collections", {}) or {}
    if not settings.get("file"):
        return None
    result = Assignment(tmdb_id=int(tmdb_id), dry_run=dry_run)
    try:
        _assign(result, settings, config, jellyfin, client, sleep, wait_seconds)
    except Exception as exc:  # noqa: BLE001 - never fail the caller over this
        log.warning("collections for tmdb %s failed: %s", tmdb_id, exc)
        result.notes.append(f"Collections stopped: {exc}")
    return result


def _assign(
    result: Assignment,
    settings: dict[str, Any],
    config: dict[str, Any],
    jellyfin: Jellyfin | None,
    client: Any,
    sleep: Callable[[float], None],
    wait_seconds: float | None,
) -> None:
    from .config import anthropic_api_key

    path = Path(settings["file"]).expanduser()
    if not path.is_file():
        result.notes.append(f"Collections skipped: no {path}")
        return
    if jellyfin is None:
        jf = config.get("jellyfin", {})
        if not (jf.get("url") and jf.get("api_key")):
            result.notes.append("Collections skipped: no Jellyfin url/api key")
            return
        jellyfin = Jellyfin(jf["url"], jf["api_key"])

    min_members = int(settings.get("min_members", 3))
    wait = settings.get("wait_seconds", 180) if wait_seconds is None else wait_seconds
    film, movies = wait_for_film(jellyfin, result.tmdb_id, float(wait), sleep)
    if film is None:
        result.notes.append(
            f"Collections skipped: Jellyfin has not listed tmdb {result.tmdb_id} "
            f"after {int(wait)}s — run `python -m torrent_agent.boxsets "
            f"{result.tmdb_id}` once it has"
        )
        return
    result.film = label(film)

    defs = load_defs(path)
    collections = custom(defs)
    boxsets = jellyfin.boxsets()
    franchises = {
        name: [t for m in jellyfin.members(bid) if (t := tmdb_of(m))]
        for name, bid in boxsets.items() if name not in collections
    }
    library = {t for m in movies if (t := tmdb_of(m))}
    taken = {n.casefold() for n in list(collections) + list(boxsets)}

    join, create, rule_notes = director_moves(
        film, movies, collections, franchises, min_members
    )

    if client is None and anthropic_api_key() is None:
        result.notes.append("Themes skipped: no ANTHROPIC_API_KEY")
    else:
        try:
            answer = ask_claude(
                client or _claude_client(),
                config.get("anthropic", {}).get("model", "claude-opus-5"),
                build_prompt(film, movies, collections, franchises, rule_notes),
                list(collections),
                min_members,
            )
        except Exception as exc:  # noqa: BLE001 - director moves still stand
            log.warning("claude placement failed: %s", exc)
            result.notes.append(f"Themes skipped: {exc}")
        else:
            join += [n for n in answer.get("join", []) if n in collections]
            result.why = (answer.get("why") or "").strip()
            for proposal in answer.get("create", [])[:1]:
                name = (proposal.get("name") or "").strip()
                ids = sorted({t for t in proposal.get("tmdb_ids", []) if t in library})
                if not name or name.casefold() in taken or name in create:
                    result.notes.append(f'Not creating "{name}": the name is taken')
                elif result.tmdb_id not in ids:
                    result.notes.append(f'Not creating "{name}": it leaves out the new film')
                elif len(ids) < min_members:
                    result.notes.append(
                        f'Not creating "{name}": only {len(ids)} of its films are in the library'
                    )
                else:
                    create[name] = ids

    # A collection that already lists the id still needs Jellyfin told —
    # that is the pre-listed case sync_collections.py --sync would otherwise
    # have to catch.
    listed = [n for n, ids in collections.items() if result.tmdb_id in ids]
    join = [n for n in dict.fromkeys(join) if n not in listed]
    result.listed = listed
    result.joined = join
    result.created = {n: len(ids) for n, ids in create.items()}
    if result.dry_run or not (join or create or listed):
        return

    # The file first: it is the record. If Jellyfin then refuses, the skill's
    # --sync rebuilds from it, whereas the other order loses the decision.
    for name in join:
        defs[name] = collections[name] + [result.tmdb_id]
    defs.update(create)
    if join or create:
        save_defs(path, defs)

    by_tmdb = {tmdb_of(m): m["Id"] for m in movies if tmdb_of(m)}
    for name in listed + join:
        try:
            if name in boxsets:
                jellyfin.add_to(boxsets[name], [film["Id"]])
            else:
                ids = [by_tmdb[t] for t in defs[name] if t in by_tmdb]
                jellyfin.create(name, ids)
        except Exception as exc:  # noqa: BLE001
            result.notes.append(
                f'Jellyfin refused "{name}" ({exc}) — sync_collections.py --sync will retry'
            )
    for name, ids in create.items():
        try:
            jellyfin.create(name, [by_tmdb[t] for t in ids if t in by_tmdb])
        except Exception as exc:  # noqa: BLE001
            result.notes.append(
                f'Jellyfin refused "{name}" ({exc}) — sync_collections.py will retry'
            )


def main() -> None:
    from .config import load_config

    parser = argparse.ArgumentParser(
        description="Assign films already in Jellyfin to collections, by TMDB id."
    )
    parser.add_argument("tmdb_ids", nargs="+", type=int)
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and report, change nothing")
    parser.add_argument("--wait", type=float, default=0,
                        help="seconds to wait for Jellyfin to list the film")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    config = load_config()
    for tmdb_id in args.tmdb_ids:
        result = assign(tmdb_id, config, dry_run=args.dry_run, wait_seconds=args.wait)
        if result is None:
            raise SystemExit("No [collections] file configured in config.toml.")
        print(f"tmdb {tmdb_id} {result.film}")
        for line in result.lines():
            print(f"  {line}")


if __name__ == "__main__":
    main()
