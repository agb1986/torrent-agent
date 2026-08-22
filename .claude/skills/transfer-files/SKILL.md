---
name: transfer-files
description: Transfer media to the media server over rsync/SSH (or a local move when it is this machine) - either a whole tidied directory via scripts/transfer.py, or a single file into a show's existing season directory. Use when the user wants to send, push, upload, copy, or sync files to the media server.
---

# Transfer Files

> **Placeholders below** — `$SERVER` is `user@host`, `$TV_DIR` / `$FILM_DIR`
> are the entries under `[server.destinations]`, and `$JELLYFIN_URL` is
> `[jellyfin] url`, all from the active `config.toml`. They are not
> environment variables; read the values out of the config before running
> anything. An empty `[server] host` means the library is on this machine, so
> there is nothing to ssh to — use local paths directly.


Send media to the configured media server.

Files should already be tidied into their target naming before transfer — use
the **tidy-files** skill first if they are not.

## SSH authentication

The server uses key auth via `~/.ssh/id_rsa_ha`. Both `transfer.py` and direct
`ssh`/`rsync` commands can be run straight from the Bash tool — no password
prompt, no need to ask the user to run them.

If a command fails with `Could not resolve hostname $SERVER_HOST`, just retry —
mDNS resolution is intermittently flaky on this box and recovers on its own.

## Destinations

| Flag | Path on server | Holds |
|---|---|---|
| `--tv` | `$TV_DIR` | `Show Name (Year) [tmdbid-N]/Season XX/SXXEYY - Episode.ext` |
| `--film` | `$FILM_DIR` | flat `Film Name (Year) [tmdbid-N].ext` files |
| `--book` | `/media/local/books` | |
| `--manga` | `/media/local/manga` | |

## Step 1 — Locate what to transfer

Media lives in Deluge's download directory, the same as for **tidy-files** —
`$DOWNLOADS_DIR` on the CasaOS server. Resolve what the
user gave you against it:

- **An explicit path** → use it as-is.
- **A bare title** (`succession`, `withnail`) → search for it:

  ```bash
  find $DOWNLOADS_DIR -maxdepth 2 -iname "*succession*"
  ```

- **Nothing at all** → list the download directory and ask which item to send.

A title will often match **two** things: the original release directory
(`Succession (2018) Season 1-4 S01-S04 (1080p Mixed x265 ...)`) and the tidied
one next to it (`Succession (2018) [tmdbid-76331]/`). **Transfer the tidied
one** — the clean `Show Name (Year) [tmdbid-N]/Season XX/` tree, or the
`Film Name (Year) [tmdbid-N].ext` file. Sending a raw release directory puts
junk on the server and leaves Jellyfin unable to match it.

If only an untidied release directory exists, run **tidy-files** first rather
than transferring it as-is.

**Exception: `--book`/`--manga`.** These have no TVmaze/TMDB-style renaming
scheme (`tidy-files` doesn't know how to name them) and no Jellyfin scan to
match a naming convention for — transfer the downloaded directory or file
as-is, under its original release name, skipping tidy-files entirely.

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
python3 ~/workspace/repos/torrent-agent/scripts/transfer.py "/path/to/Show Name (1999) [tmdbid-37854]" --tv
```

```bash
python3 ~/workspace/repos/torrent-agent/scripts/transfer.py "/path/to/Film Name (1987) [tmdbid-13446].mkv" --film
```

It rsyncs with `--archive --verbose --progress --human-readable`, triggers the
Jellyfin scan itself when the transfer succeeds, and prints elapsed time at the
end.
More than one destination flag can be passed; each runs as a separate transfer.

Nothing further to do — the script handles the scan.

## Option B — single file into an existing show

1. List the shows to find the exact directory name:

   ```bash
   ssh $SERVER 'ls $TV_DIR'
   ```

2. List that show's contents to confirm the season directory and match its
   existing naming convention:

   ```bash
   ssh $SERVER 'ls "$TV_DIR/Show Name"'
   ssh $SERVER 'ls "$TV_DIR/Show Name/Season 01"'
   ```

   Name the new file consistently with what is already there. If the season
   directory does not exist, create it first:

   ```bash
   ssh $SERVER 'mkdir -p "$TV_DIR/Show Name/Season 01"'
   ```

3. rsync the file into that directory (note the trailing slash on the
   destination):

   ```bash
   rsync -avh --progress "S01E05 - Title.mkv" \
     "$SERVER:$TV_DIR/Show Name/Season 01/"
   ```

4. Confirm success from the rsync output and report the transferred size.

5. Trigger a Jellyfin scan so the new file appears in the library — see below.
   `transfer.py` does this by itself, but a direct rsync does not.

## Triggering a Jellyfin scan

Jellyfin runs on the server at `$JELLYFIN_URL`, authenticated with
the `X-Emby-Token` header. The API key is in the global env as
`JELLYFIN_API_KEY`, which sits behind the non-interactive guard in `~/.bashrc`
— so reach it through an interactive shell (`bash -ic '...'`), the same as
`ANTHROPIC_API_KEY`.

Use the **targeted media-updated endpoint** (the same one Sonarr/Radarr use),
which tells Jellyfin exactly which path changed instead of rescanning the
whole library:

```
POST /Library/Media/Updated
{"Updates": [{"Path": "<container-path>", "UpdateType": "Created"}]}
```

```bash
bash -ic 'curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST "$JELLYFIN_URL/Library/Media/Updated" \
  -H "X-Emby-Token: $JELLYFIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"Updates\":[{\"Path\":\"/media/tv/Show Name\",\"UpdateType\":\"Created\"}]}"'
```

Two things make this go wrong silently:

- **Paths must be translated.** The Jellyfin container mounts the libraries
  separately: `$TV_DIR` → `/media/tv` and `$FILM_DIR` →
  `/media/movies`. So scan `/media/tv/Show Name` or
  `/media/movies/Film - Year.mkv`, never a `/mnt/data/...` path. (This is NOT
  the single-prefix mapping the old Plex setup used.)
- **Jellyfin answers `204` regardless** — including for a path it does not
  recognise. The status code only says the request was accepted. (`401` means
  a bad or missing token.)

Fallback if the targeted update misbehaves — full library scan:

```bash
bash -ic 'curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "$JELLYFIN_URL/Library/Refresh" -H "X-Emby-Token: $JELLYFIN_API_KEY"'
```

`/media/local/books` and `/media/local/manga` are not Jellyfin libraries — no
scan applies to those.

### Confirming the scan worked

Because the status code proves nothing, verify by searching for the title.
`TotalRecordCount` > 0 means Jellyfin has indexed it:

```bash
bash -ic 'curl -s -H "X-Emby-Token: $JELLYFIN_API_KEY" \
  "$JELLYFIN_URL/Items?searchTerm=Succession&recursive=true&includeItemTypes=Series"' \
  | python3 -m json.tool | grep -E "\"Name\"|\"TotalRecordCount\""
```

(Use `includeItemTypes=Movie` for films, or `Episode` to count episodes.)

Scans are not instant on a large directory; if nothing shows up, wait a few
seconds and query again before assuming something failed.

## Notes

- Quote every path — show and film names are full of spaces, apostrophes and
  brackets.
- Large transfers can outrun a default tool timeout. Give the command a generous
  timeout, or run it in the background and report when it finishes.
- rsync is incremental, so re-running a transfer to fill in missing files is
  safe and cheap.
- Never delete anything from the server as part of a transfer.
