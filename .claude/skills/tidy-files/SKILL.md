---
name: tidy-files
description: Tidy TV and film media files - repackage TV directories or single episodes into a clean "Show Name (Year) [tmdbid-N]/Season XX/SXXEYY - Episode Name.ext" structure (episode names from TVmaze, ids from TMDB/Wikidata so Jellyfin matches exactly), and rename films to "Film Name (Year) [tmdbid-N].ext". Use when the user wants to tidy, rename, repackage, or organize downloaded TV shows or movies.
---

# Tidy Files

> **Placeholders below** — `$SERVER` is `user@host`, `$TV_DIR` / `$FILM_DIR`
> are the entries under `[server.destinations]`, and `$JELLYFIN_URL` is
> `[jellyfin] url`, all from the active `config.toml`. They are not
> environment variables; read the values out of the config before running
> anything. An empty `[server] host` means the library is on this machine, so
> there is nothing to ssh to — use local paths directly.


Repackage messy media files into a clean, consistently named structure.

TV target format — a directory tree:

```
One Piece (1999) [tmdbid-37854]/
  Season 01/
    S01E01 - I'm Luffy! The Man Who's Gonna Be King of the Pirates!.mkv
    S01E02 - The Great Swordsman Appears! Pirate Hunter Roronoa Zoro.mkv
```

Film target format — a flat file:

```
Withnail and I (1987) [tmdbid-13446].mkv
```

The `[tmdbid-NNNNN]` tag is what makes Jellyfin match the item **exactly**
instead of guessing from the name. Its guess is wrong often enough to matter:
search "One Piece" and the top hit is the 2023 live action, not the 1999 anime.
Season directories and episode files carry no tag — Jellyfin only needs the id
at the series/film level.

Tidying only. To deliver the result afterwards, use the
**transfer-files** skill.

## Step 1 — Identify the input

Deluge's download directory differs per machine, so ask Deluge rather than
assuming. `scripts/status.py` lists the session, and the path is in the
`[deluge]` block of the active config — on the CasaOS server it is
`$DOWNLOADS_DIR`. Where Deluge runs in a container it reports its *own*
view (`/downloads`); `[deluge.path_map]` translates that to the host path.

Resolve what the user gave you against that directory:

- **An explicit path** → use it as-is.
- **A bare title** (`succession`, `withnail`) → search the download directory
  for it, case-insensitively and on a fragment of the name, since release
  directories carry a lot of junk around the title:

  ```bash
  find $DOWNLOADS_DIR -maxdepth 2 -iname "*succession*"
  ```

  Put a `*` between words rather than a space — releases are as often
  dot-separated as spaced, so `*young*sheldon*` finds both
  `Young Sheldon (2017) Season 1 S01 ...` and `Young.Sheldon.S07...`, while
  `*young sheldon*` silently misses the second.

  Several hits usually still mean **one** job — resolve them before asking:

  - A directory and the media file inside it are the same item. Take the
    directory.
  - Per-season directories of one show (`Young Sheldon ... Season 1 S01`,
    `... Season 2 S02`, …) are one show. Tidy them together into a single
    `Show Name/` tree with a `Season XX` directory each.
  - A raw release directory and an already-tidied one sitting side by side are
    genuinely different. Ask which, and say which is which.

  No match at all — say so rather than tidying something unrelated.
- **Nothing at all** → list the download directory and ask which item to tidy.
  Do not tidy the whole folder in one go unless the user asks for exactly that.

Then, for whatever you resolved to:

- **Directory**: inspect it recursively. Media extensions to keep:
  `.mkv .mp4 .avi .m4v .mov .ts .wmv` plus subtitles `.srt .sub .ass .ssa .idx`.
  Everything else (`.nfo`, `.txt`, samples, `.exe`, screenshots, torrent junk)
  is left behind in the source directory — never delete it yourself.
- **Single file**: just that file.

## Step 2 — Decide TV or film

Treat the input as **TV** if any filename or parent directory carries a
season/episode token (`S01E01`, `1x01`, `Season 3`, `Complete Series`, …).
Otherwise treat it as a **film** — typically a single media file, or a release
directory holding one media file plus junk.

A directory may hold several unrelated films; handle each as its own film. If a
directory mixes TV and film, say so and ask the user how to split it.

Then follow **Step 3A** (TV) or **Step 3B** (film), and **Step 3C** either way —
both target names end in a TMDB id.

## Step 3A — TV: parse show / season / episode

Extract from each filename (and parent directory names when the filename is
unhelpful):

- Show name: the leading portion before the season/episode token, with
  dots/underscores converted to spaces, and release junk stripped (resolution,
  codec, group tags, `WEB-DL`, `x265`, `1080p`, brackets, etc.). Title-case it
  sensibly.
- Season/episode patterns, in order of preference:
  - `S01E01`, `s1e1`, `S01E01E02` (multi-episode)
  - `1x01`
  - bare `101` style only when unambiguous (3–4 digits, show context makes it
    clear)
  - For specials use season 0 (`S00Exx`).
- Episode name: if the filename already contains one after the SxxEyy token,
  you may use it, but prefer the canonical name from TVmaze (below) when
  available.

If the show name is ambiguous or unparseable, ask the user rather than guessing.

### Fetch episode names from TVmaze

Use the free TVmaze API (no key needed) via `curl`:

1. Resolve the show:
   `curl -s "https://api.tvmaze.com/singlesearch/shows?q=SHOW+NAME"` →
   take `id` and canonical `name`. Also keep `premiered` (the year for the
   directory name) and `externals.imdb` (feeds the TMDB lookup below).
   Confirm with the user if the matched show name differs significantly from
   the parsed one.
2. Get all episodes:
   `curl -s "https://api.tvmaze.com/shows/<id>/episodes"` → array with
   `season`, `number`, `name`.
3. Map each file's (season, episode) to the canonical episode name.

TVmaze's own search has the same weakness as Jellyfin's — `q=one piece`
returns the 2023 live action. When the release is plainly older or newer than
what came back, search with the year (`q=one piece 1999`) or use
`/search/shows?q=` and pick from the list rather than accepting
`singlesearch`'s first guess.

Fallback if TVmaze has no match: fetch
`https://epguides.com/<ShowNameNoSpaces>/` and parse the episode list from the
HTML. If both fail, use the name from the filename, or `Episode XX` as a last
resort.

TVmaze censors some episode titles (e.g. `Sh*t Show at the F**k Factory`).
Restore the real words — a filename is not the place for asterisks.

### TV rename plan

- **Directory input**: create a new directory next to the source named
  `Show Name (Year) [tmdbid-NNNNN]` (see **Step 3C**), with `Season 01`,
  `Season 02`, ... subdirectories. Each
  media file becomes `SXXEYY - Episode Name.ext` (zero-padded, two digits; more
  if the show has 100+ episodes per season). Subtitle files get the same
  basename as their episode, keeping language tags if present (e.g.
  `S01E01 - Pilot.en.srt`).
- **Single file input**: rename in place to `SXXEYY - Episode Name.ext`.

## Step 3B — Film: parse name and year

Target name is `Film Name (Year) [tmdbid-NNNNN].ext`, e.g.
`Withnail and I (1987) [tmdbid-13446].mkv` (the id comes from **Step 3C**).

- Film name: everything before the year token, dots/underscores converted to
  spaces, release junk stripped (`1080p`, `2160p`, `BluRay`, `WEB-DL`, `REMUX`,
  `x265`, `HEVC`, `10bit`, `DDP5.1`, `PROPER`, `REMASTERED`, group tags,
  brackets, `YTS.MX`, …). Title-case it sensibly.
- Year: the four-digit `19xx`/`20xx` token in the filename or its parent
  directory — present in nearly every release name. Take the token that reads as
  the *release year*, not one belonging to the title (`2001 A Space Odyssey`,
  `Blade Runner 2049`, `1917`).

Edition tags worth keeping go **after** the tmdbid tag, in parentheses:
`The Prestige (2006) [tmdbid-1124] (Director's Cut).mkv`. Keep the
`Name (Year) [tmdbid-N]` part contiguous — that is the bit Jellyfin parses.
Drop everything else.

### When the year is missing

Look it up on Wikipedia (keyless):

```bash
curl -s "https://en.wikipedia.org/w/api.php?action=query&format=json&prop=extracts&exintro&explaintext&redirects=1&titles=FILM%20NAME"
```

The intro reads `... is a YYYY ... film` — take that year. Note that
`Title (film)` is often itself a disambiguation page (Dunkirk, for one), so the
extract may not describe a film at all. If it does not clearly match the film
you expect, ask the user for the year rather than guessing.

### Film rename plan

- **Single file input**: rename in place to `Film Name (Year) [tmdbid-N].ext`.
- **Release directory input**: move the one media file out to
  `Film Name (Year) [tmdbid-N].ext` alongside the source directory, leaving the
  junk behind. Keep subtitles on a matching basename
  (`Film Name (Year) [tmdbid-N].en.srt`).
- Films are flat files, not directories — this matches `$FILM_DIR` on the
  server.

## Step 3C — Look up the TMDB id

Run `scripts/tmdb_id.py` from the project root (`~/workspace/repos/torrent-agent`)
once per show or film — not per episode:

```bash
.venv/bin/python scripts/tmdb_id.py --type tv --title "One Piece" --year 1999
.venv/bin/python scripts/tmdb_id.py --type movie --title "Withnail and I" --year 1987
```

Pass `--imdb tt0388629` whenever TVmaze gave you an `externals.imdb` — that
turns the search into an exact lookup and removes the guesswork entirely.
Always pass `--year` when you have one; it is what separates a film from its
remake.

It prints JSON with `tmdb_id`, the canonical `name`, the `year`, and a ready-made
`tag`:

```json
{
  "tmdb_id": "37854",
  "name": "One Piece",
  "year": 1999,
  "source": "wikidata",
  "tag": "One Piece (1999) [tmdbid-37854]"
}
```

Use `tag` verbatim as the directory name (TV) or the file basename (film) —
it already strips characters a path cannot hold. `--tag` prints just that line
if you want it for a shell variable.

No API key is needed: without `TMDB_API_KEY` it resolves ids through Wikidata,
which is where the examples above come from. Setting the key just makes TMDB
itself the first source, which matters for very new releases Wikidata has not
caught up with.

### When it cannot decide

- **Exit 2 — ambiguous.** It prints the candidates it could not separate
  (usually a title and its remakes). Show them to the user with their years and
  ask which; never pick one yourself, a wrong id makes Jellyfin download
  metadata for the wrong film entirely.
- **Exit 1 — no match.** Say so and tidy **without** the tag, using
  `Show Name (Year)` / `Film Name (Year).ext`. A missing tag costs nothing —
  Jellyfin falls back to guessing, exactly as it did before. Never invent an id.

Sanity-check the `name` it returns against what you parsed. If it came back
with a different show (`source: wikidata` searching by title can drift), fix the
title or pass `--imdb` and rerun.

## Step 4 — Confirm, then execute

Show the user the complete old → new mapping as a table and **wait for
confirmation** before executing. Flag anything unresolved (unknown episode,
missing year, missing TMDB id, duplicate target names, missing TVmaze data).
State the show/film the TMDB id resolved to — that is the user's chance to
catch a wrong match before the library inherits it.

On confirmation, **move** (`mv`) files into the new structure — do not copy.
Quote every path; release names are full of spaces, brackets and apostrophes —
and now square brackets too, from the tmdbid tag.

Once the moves succeed, log the mapping for ai-data-store by piping it as JSON
into `scripts/log_tidy.py` (run from the project root):

```bash
echo '{"kind": "tv", "name": "Show Name", "tmdb_id": "37854", "mapping": [{"from": "/old/path.mkv", "to": "/new/path.mkv"}]}' \
  | .venv/bin/python scripts/log_tidy.py
```

`kind` is `"tv"` or `"film"`, `name` is the canonical show/film name, `tmdb_id`
is the id you tagged with (omit it if there was none), and `mapping` lists every
file actually moved. This script only prints the artifact path it wrote — that
output is bookkeeping, not something to relay to the user.

## Step 5 — Offer to delete the leftovers

The move leaves the original release directory behind holding whatever was not
claimed. Offer to delete it — but only after the move has actually happened, and
only with a **separate** confirmation. Approval of the rename plan in Step 4 is
not approval to delete.

### Pre-deletion verification (CRITICAL)

**Before asking the user, verify the tidy succeeded:**

1. Count media files in the tidied output directory (newly created `Show Name/`
   or renamed file).
2. Count files moved from source directories.
3. Ensure they match exactly. If counts diverge, **abort the deletion offer** and
   report the discrepancy to the user before proceeding.

Example for multi-season TV:
```bash
# Count in source directories (across all season dirs being tidied)
find /path/to/source/*/  -type f -iname "*.mkv" | wc -l

# Count in destination
find /path/to/Show\ Name -type f -iname "*.mkv" | wc -l
# These must match exactly before offering deletion
```

If counts don't match, **do not proceed with deletion**. Report what's missing
and ask the user to investigate.

### If verification passes

Look at what is actually left:

```bash
find "/path/to/release dir" -type f -printf "%s\t%p\n" | sort -rn | head -20
du -sh "/path/to/release dir"
```

Report the total size, then split what you found:

- **Junk** — `.nfo`, `.txt`, `sample.mkv`, screenshots, `www.*.jpg`, proxy
  lists, torrent scraps. Safe to lose.
- **Real content you did not move** — an `Extras/` folder of genuine bonus
  features, commentary tracks, a photo gallery. Name these individually with
  their sizes. A release can carry over a gigabyte of extras that the user may
  not want to throw away sight unseen.

### Deletion safeguards (MANDATORY)

When asking the user, list the **exact directory names** being deleted:

❌ **WRONG:**  "Delete leftover directories (100 MB)?"
✅ **RIGHT:** "Delete these 3 directories (100 MB total)? — Young Sheldon Season 1 S01 (...), Young Sheldon Season 2 S02 (...), Young Sheldon Season 3 S03 (...)"

When executing deletion:

1. **Never use glob patterns** (`rm -rf Young*Sheldon*/`).
2. **Delete by explicit full path**, one directory at a time:
   ```bash
   rm -rf "/full/path/to/exact/directory/name"
   ```
   Repeat for each directory.
3. **After each deletion, verify the tidied output still exists**:
   ```bash
   [ -d "/path/to/Show Name" ] && echo "SAFE: output still exists" \
     || echo "ERROR: output deleted!"
   ```
4. **Stop immediately if output is ever deleted**.

Do not delete when:

- the move failed, or the tidied files are not verifiably in place;
- media files are left that belong to the tidy but were never claimed (an
  episode that failed to parse, a second film in the same directory) — resolve
  those first;
- the user declines or is ambiguous. Say it can be deleted later and move on;
- **file counts don't match between source and destination** — report the
  mismatch and abort.

Never delete anything outside the release directory that was just tidied, and
never delete the tidied output.

## Step 6 — Offer the transfer

Report what was tidied and where it now lives, then offer to push it to the
media server via the **transfer-files** skill. If the user declines, stop here.

### If the show is already on the server untagged

Older tidies produced a bare `Show Name/`. Pushing `Show Name (Year) [tmdbid-N]/`
next to it gives Jellyfin **two** entries for the one show, with the episodes
split between them. Check before transferring:

```bash
ssh $SERVER 'ls $TV_DIR | grep -i "show name"'
```

If an untagged directory is there, the fix is to rename the **remote** one to
the tagged name so the new episodes land inside it. That is a change to the
user's library, so ask first, then:

```bash
ssh $SERVER 'mv "$TV_DIR/Show Name" "$TV_DIR/Show Name (1999) [tmdbid-37854]"'
```
