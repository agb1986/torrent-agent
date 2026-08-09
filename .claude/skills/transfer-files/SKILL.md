---
name: transfer-files
description: Transfer media to the CASAOS server (casaos@casaos.local) over rsync/SSH - either a whole tidied directory via scripts/transfer.py, or a single file into a show's existing season directory. Use when the user wants to send, push, upload, copy, or sync files to the CASAOS/media server.
---

# Transfer Files

Send media to the CASAOS server at `casaos@casaos.local`.

Files should already be tidied into their target naming before transfer — use
the **tidy-files** skill first if they are not.

## SSH authentication

The server uses key auth via `~/.ssh/id_rsa_ha`. Both `transfer.py` and direct
`ssh`/`rsync` commands can be run straight from the Bash tool — no password
prompt, no need to ask the user to run them.

If a command fails with `Could not resolve hostname casaos.local`, just retry —
mDNS resolution is intermittently flaky on this box and recovers on its own.

## Destinations

| Flag | Path on server | Holds |
|---|---|---|
| `--tv` | `/mnt/data/tv` | `Show Name/Season XX/SXXEYY - Episode.ext` |
| `--film` | `/mnt/data/film` | flat `Film Name - Year.ext` files |
| `--book` | `/media/local/books` | |
| `--manga` | `/media/local/manga` | |

## Step 1 — Locate what to transfer

Media lives in `~/Downloads`, the same as for **tidy-files**. Resolve what the
user gave you against it:

- **An explicit path** → use it as-is.
- **A bare title** (`succession`, `withnail`) → search for it:

  ```bash
  find ~/Downloads -maxdepth 2 -iname "*succession*"
  ```

- **Nothing at all** → list `~/Downloads` and ask which item to send.

A title will often match **two** things: the original release directory
(`Succession (2018) Season 1-4 S01-S04 (1080p Mixed x265 ...)`) and the tidied
one next to it (`Succession/`). **Transfer the tidied one** — the clean
`Show Name/Season XX/` tree, or the `Film Name - Year.ext` file. Sending a raw
release directory puts junk on the server and leaves Plex unable to match it.

If only an untidied release directory exists, run **tidy-files** first rather
than transferring it as-is.

## Step 2 — Pick the mode

- **Whole directory** (a tidied show, or a batch) → Option A.
- **Single file joining a show that is already on the server** → Option B.
  This matters because a bare `rsync` of a `Show Name/` directory would nest
  wrongly next to the existing one; targeting the season directory directly
  avoids that.

## Option A — whole directory via transfer.py

**Do not run this one — print the command and let the user run it themselves.**
These transfers are long and the user prefers to drive them.

`transfer.py` lives at `scripts/transfer.py` in this repo. Give the command with
absolute paths so it works from anywhere:

```bash
python3 ~/workspace/repos/torrent-agent/scripts/transfer.py "/path/to/Show Name" --tv
```

```bash
python3 ~/workspace/repos/torrent-agent/scripts/transfer.py "/path/to/Film Name - 1987.mkv" --film
```

It rsyncs with `--archive --verbose --progress --human-readable`, triggers the
Plex scan itself when the transfer succeeds, and prints elapsed time at the end.
More than one destination flag can be passed; each runs as a separate transfer.

Nothing further to do — the script handles the scan.

## Option B — single file into an existing show

1. List the shows to find the exact directory name:

   ```bash
   ssh casaos@casaos.local 'ls /mnt/data/tv'
   ```

2. List that show's contents to confirm the season directory and match its
   existing naming convention:

   ```bash
   ssh casaos@casaos.local 'ls "/mnt/data/tv/Show Name"'
   ssh casaos@casaos.local 'ls "/mnt/data/tv/Show Name/Season 01"'
   ```

   Name the new file consistently with what is already there. If the season
   directory does not exist, create it first:

   ```bash
   ssh casaos@casaos.local 'mkdir -p "/mnt/data/tv/Show Name/Season 01"'
   ```

3. rsync the file into that directory (note the trailing slash on the
   destination):

   ```bash
   rsync -avh --progress "S01E05 - Title.mkv" \
     "casaos@casaos.local:/mnt/data/tv/Show Name/Season 01/"
   ```

4. Confirm success from the rsync output and report the transferred size.

5. Trigger a Plex scan so the new file appears in the library — see below.
   `transfer.py` does this by itself, but a direct rsync does not.

## Triggering a Plex scan

Plex runs on the server at `http://casaos.local:32400`, authenticated with the
`X-Plex-Token` header or query parameter. The token is in the global env as
`PLEX_TOKEN`, which sits behind the non-interactive guard in `~/.bashrc` — so
reach it through an interactive shell (`bash -ic '...'`), the same as
`ANTHROPIC_API_KEY`.

Use a **partial scan**, which walks only the directory just written instead of
the whole library:

```
GET /library/sections/{key}/refresh?path={container-path}&X-Plex-Token={token}
```

```bash
bash -ic 'curl -s -o /dev/null -w "%{http_code}\n" \
  "http://casaos.local:32400/library/sections/6/refresh?path=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "/Media/tv/Show Name")&X-Plex-Token=$PLEX_TOKEN"'
```

Two things make this go wrong silently:

- **Paths must be translated.** The plex container bind-mounts `/mnt/data` as
  `/Media`, so scan `/Media/tv/Show Name`, never `/mnt/data/tv/Show Name`.
- **Plex answers `200` regardless** — including for a path it does not
  recognise. The status code only says the request was accepted. (`404` means a
  bad section key, `401` a bad or missing token.)

Section keys on this server: **6** = TV Shows (`/Media/tv`), **7** = Movies
(`/Media/film`). Re-derive them if the libraries are ever rebuilt:

```bash
bash -ic 'curl -s "http://casaos.local:32400/library/sections?X-Plex-Token=$PLEX_TOKEN"'
```

`/media/local/books` and `/media/local/manga` are not Plex libraries — no scan
applies to those.

### Confirming the scan worked

Because the status code proves nothing, verify by querying the section for the
title. `leafCount` is the number of episodes Plex has indexed:

```bash
bash -ic 'curl -s "http://casaos.local:32400/library/sections/6/all?title=Succession&X-Plex-Token=$PLEX_TOKEN"' \
  | grep -o 'title="[^"]*"\|leafCount="[0-9]*"'
```

Scans are not instant on a large directory; if the count looks short, wait a few
seconds and query again before assuming something failed.

## Notes

- Quote every path — show and film names are full of spaces, apostrophes and
  brackets.
- Large transfers can outrun a default tool timeout. Give the command a generous
  timeout, or run it in the background and report when it finishes.
- rsync is incremental, so re-running a transfer to fill in missing files is
  safe and cheap.
- Never delete anything from the server as part of a transfer.
