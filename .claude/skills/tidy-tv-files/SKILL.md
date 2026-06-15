---
name: tidy-tv-files
description: Tidy TV media files - repackage directories or single episode files into a clean "Show Name/Season XX/SXXEYY - Episode Name.ext" structure, fetching missing episode names from TVmaze, then optionally transfer to the CASAOS server (casaos@casaos.local). Use when the user wants to tidy, rename, repackage, or organize TV show files, or send media to the CASAOS/media server.
---

# Tidy TV Files

Repackage messy TV media files into a clean, consistently named structure, then
optionally transfer them to the CASAOS server.

Target naming format:

```
Show Name/
  Season 01/
    S01E01 - Pilot.mkv
    S01E02 - The Second One.mkv
```

## Step 1 — Identify the input

The input is a single file or a directory (from the user's prompt; if no path
was given, ask for it).

- **Directory**: inspect it recursively. Media extensions to keep:
  `.mkv .mp4 .avi .m4v .mov .ts .wmv` plus subtitles `.srt .sub .ass .ssa .idx`.
  Everything else (`.nfo`, `.txt`, samples, `.exe`, screenshots, torrent junk)
  is left behind in the source directory — never delete it yourself.
- **Single file**: just that file.

## Step 2 — Parse show / season / episode from filenames

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
  you may use it, but prefer the canonical name from TVmaze (Step 3) when
  available.

If the show name is ambiguous or unparseable, ask the user rather than guessing.

## Step 3 — Fetch episode names from TVmaze

Use the free TVmaze API (no key needed) via `curl`:

1. Resolve the show:
   `curl -s "https://api.tvmaze.com/singlesearch/shows?q=SHOW+NAME"` →
   take `id` and canonical `name`. Confirm with the user if the matched show
   name differs significantly from the parsed one.
2. Get all episodes:
   `curl -s "https://api.tvmaze.com/shows/<id>/episodes"` → array with
   `season`, `number`, `name`.
3. Map each file's (season, episode) to the canonical episode name.

Fallback if TVmaze has no match: fetch
`https://epguides.com/<ShowNameNoSpaces>/` and parse the episode list from the
HTML. If both fail, use the name from the filename, or `Episode XX` as a last
resort.

Sanitize episode names for filesystems: replace `/` and `\` with `-`, drop
`: ? * " < > |` (replace `:` with ` -`), collapse repeated spaces, trim
trailing dots/spaces.

## Step 4 — Build and confirm the rename plan

Construct the full mapping before touching anything:

- **Directory input**: create a new directory next to the source named after the
  canonical show name, with `Season 01`, `Season 02`, ... subdirectories. Each
  media file becomes `SXXEYY - Episode Name.ext` (zero-padded, two digits; more
  if the show has 100+ episodes per season). Subtitle files get the same
  basename as their episode, keeping language tags if present (e.g.
  `S01E01 - Pilot.en.srt`).
- **Single file input**: rename in place to `SXXEYY - Episode Name.ext`.

Show the user the complete old → new mapping as a table and **wait for
confirmation** before executing. Flag anything unresolved (unknown episode,
duplicate target names, missing TVmaze data).

On confirmation, **move** (`mv`) files into the new structure — do not copy.
Leave the leftover junk in the original directory and tell the user it can be
deleted; do not delete it yourself.

## Step 5 — Ask what to do next

After tidying, always ask the user which of these to do:

1. **Add single file to existing show on the server**
2. **Transfer new directory via transfer.py**
3. **Do nothing** — finish here.

### Important: SSH is password-authenticated

The server (`casaos@casaos.local`) and `transfer.py` use interactive password
auth, so you cannot run ssh/rsync yourself. Prepare the exact command and ask
the user to run it with the `!` prefix so they can type the password and the
output lands in the conversation.

### Option 1 — single file into existing structure

1. Ask the user to run:
   `! ssh casaos@casaos.local 'ls /mnt/data/tv'`
2. From the output, find the matching show directory, then list its contents
   the same way to find/confirm the right season directory (match the server's
   existing season-dir naming convention). If the season directory doesn't
   exist, include `mkdir -p` for it in the rsync step via `--rsync-path` or a
   prior ssh command.
3. Give the user the final rsync command to run, e.g.:
   `! rsync -avh --progress "S01E05 - Title.mkv" "casaos@casaos.local:/mnt/data/tv/Show Name/Season 01/"`
4. Check the output they paste back and confirm success.

### Option 2 — new directory via transfer.py

`transfer.py` lives in `scripts/transfer.py` inside this repo. Give the user
the command to run from the repo root:

```
! python3 scripts/transfer.py "/path/to/Show Name" --tv
```

(`--tv` targets `/mnt/data/tv` on `casaos.local`.) Confirm success from the
rsync output they paste back.

Available destination flags: `--tv`, `--film`, `--book`, `--manga`.

### Option 3 — do nothing

Summarize what was tidied and where it lives, and stop.
