---
name: fetch-to-jellyfin
description: Full pipeline - fetch a torrent, wait for the download to finish, remove it from Deluge, tidy the files, deliver them to the media server, and let it pick them up. Use when the user wants media downloaded AND delivered to the server/Jellyfin in one go (e.g. "get X onto the server", "download and add X to jellyfin").
---

# Fetch to Jellyfin

Run the whole media pipeline in one request. Each stage is an existing skill —
this skill is the orchestration and hand-off rules between them.

> **This is the interactive route.** The same pipeline runs unattended on the
> server: `server/notifier.py --deliver` watches Deluge and, on completion,
> removes the torrent, tidies, delivers and tells Jellyfin — with no session
> open. Use this skill when you want to drive it yourself, when the automated
> run escalated because it was not sure how to name something, or on a machine
> where the notifier is not running. Check first whether the download has
> already been filed; on a server with auto-delivery on, it probably has.

```
get-torrents ──▶ poll download-status ──▶ remove from Deluge
                                              │
                                              ▼
                       tidy-files ──▶ transfer-files ──▶ Jellyfin scan
```

## Stage 1 — Fetch (get-torrents)

Follow the **get-torrents** skill to add the torrent. If the VPN is down or
nothing is found, stop the pipeline there and report why.

Note the torrent title from the agent's output — it identifies the download in
the later stages.

**Also keep the torrent id**, which Stage 3 needs. It is not in the text
summary: read it from the logging artifact whose path the agent prints as its
first line (the one Step 3 of **get-torrents** tells you not to relay). Each
entry under `added` carries a `torrent_id`:

```bash
.venv/bin/python -c "
import json,sys
for a in json.load(open(sys.argv[1]))['added']:
    print(a['torrent_id'], a['title'])
" /path/to/tmp/added_<slug>_<timestamp>.json
```

A multi-episode request adds several torrents — collect every id.

## Stage 2 — Wait for completion (download-status)

Poll with the **download-status** skill:

```bash
cd <project root> && .venv/bin/python scripts/status.py
```

- Use the reported ETA to pick the poll interval: check again at roughly the
  ETA, with a floor of 2 minutes. Use `sleep` in the Bash tool or a background
  command — do not busy-loop.
- The torrent is done when its state is `Seeding` (or progress is 100%).
- If the state is `Paused`/`Error`, or progress has not moved across two polls
  spaced ≥5 minutes apart, stop and report the stall instead of waiting
  forever.
- Long downloads: tell the user the ETA after the first poll so they know what
  the pipeline is doing.

## Stage 3 — Remove the torrent from Deluge

Once the download has completed — and **only** then — remove the torrent(s)
this run added, before tidying. Pass every id from Stage 1:

```bash
cd <project root> && \
  .venv/bin/python scripts/remove_seeding.py --id <torrent_id> [--id <torrent_id> ...]
```

- **Always pass `--id`.** Bare `remove_seeding.py` removes *every* seeding
  torrent, including unrelated ones the user is deliberately seeding. The
  pipeline must only clean up after itself.
- **Data is never deleted** — `remove_data=False` is hardcoded, so the files
  stay in the download directory for Stage 4 to tidy. Removing the torrent only drops
  Deluge's entry.
- The first output line is a logging artifact path — skip it when relaying, as
  with the other scripts.
- `No matching torrents in Deluge (already removed?)` is **not** a failure.
  It means the torrent was removed by hand in the meantime; the download is
  still on disk. Carry on to Stage 4.
- If removal fails for any other reason, say so but **continue the pipeline** —
  a leftover Deluge entry is untidy, not harmful, and the media is what the
  user asked for.

Doing this before Stage 4 is deliberate: tidying renames the files out from
under Deluge, so a torrent left in place afterwards cannot seed anyway and just
sits there in Error.

Two consequences worth stating plainly when you report at the end: the release
stops seeding at this point (its contribution back to the swarm is roughly
nothing), and re-downloading from the swarm is no longer possible if a later
stage goes wrong. The downloaded files themselves are untouched and complete —
the torrent only reaches this stage after finishing — so tidy and transfer can
always be retried from what is on disk.

## Stage 4 — Tidy (tidy-files)

The finished download is in Deluge's download directory. Follow the
**tidy-files** skill to
repackage it (`Show Name (Year) [tmdbid-N]/Season XX/SXXEYY - Episode.ext` for
TV, `Film Name (Year) [tmdbid-N].ext` for films — the tmdbid tag is what lets
Jellyfin match the item instead of guessing from the name).

## Stage 5 — Transfer (transfer-files)

Follow the **transfer-files** skill to send the tidied result to the server.
Note its rules still apply: whole-directory transfers (Option A) are printed
for the user to run, not run directly. In that case, hand the user the command
and tell them the Jellyfin scan happens automatically when it finishes — the
pipeline ends there for you.

Single-file transfers (Option B) you run yourself, then trigger the Jellyfin
scan as that skill describes.

## Stage 6 — Confirm

Report the full journey: what was fetched (title/resolution), that the torrent
was removed from Deluge, where it landed on the server, and that Jellyfin was
notified. If any stage was handed to the user (Option A transfer), say exactly
what is left for them to do.

## Failure rules

- A failure in any stage stops the pipeline — never transfer an untidied or
  incomplete download. **Stage 3 is the exception**: a failed removal is
  reported but does not stop anything.
- Never remove a torrent that has not finished downloading. Stage 3 runs only
  after Stage 2 confirms completion.
- Only ever remove torrents by the ids this run added — never the bare
  all-seeding sweep.
- Data is never deleted at any stage; removing a torrent keeps its files, and
  cleanup of the original release directory is offered at the
  end, not done automatically.
