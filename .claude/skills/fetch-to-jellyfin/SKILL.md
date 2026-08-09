---
name: fetch-to-jellyfin
description: Full pipeline - fetch a torrent, wait for the download to finish, tidy the files, transfer them to the CASAOS server, and let Jellyfin pick them up. Use when the user wants media downloaded AND delivered to the server/Jellyfin in one go (e.g. "get X onto the server", "download and add X to jellyfin").
---

# Fetch to Jellyfin

Run the whole media pipeline in one request. Each stage is an existing skill —
this skill is the orchestration and hand-off rules between them.

```
get-torrents ──▶ poll download-status ──▶ tidy-files ──▶ transfer-files ──▶ Jellyfin scan
```

## Stage 1 — Fetch (get-torrents)

Follow the **get-torrents** skill to add the torrent. If the VPN is down or
nothing is found, stop the pipeline there and report why.

Note the torrent title from the agent's output — it identifies the download in
the later stages.

## Stage 2 — Wait for completion (download-status)

Poll with the **download-status** skill:

```bash
cd /home/agb86/workspace/repos/torrent-agent && .venv/bin/python scripts/status.py
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

## Stage 3 — Tidy (tidy-files)

The finished download is in `~/Downloads`. Follow the **tidy-files** skill to
repackage it (`Show Name/Season XX/SXXEYY - Episode.ext` for TV,
`Film Name - Year.ext` for films).

## Stage 4 — Transfer (transfer-files)

Follow the **transfer-files** skill to send the tidied result to the server.
Note its rules still apply: whole-directory transfers (Option A) are printed
for the user to run, not run directly. In that case, hand the user the command
and tell them the Jellyfin scan happens automatically when it finishes — the
pipeline ends there for you.

Single-file transfers (Option B) you run yourself, then trigger the Jellyfin
scan as that skill describes.

## Stage 5 — Confirm

Report the full journey: what was fetched (title/resolution), where it landed
on the server, and that Jellyfin was notified. If any stage was handed to the
user (Option A transfer), say exactly what is left for them to do.

## Failure rules

- A failure in any stage stops the pipeline — never transfer an untidied or
  incomplete download.
- Data is never deleted at any stage; cleanup of the original release
  directory in `~/Downloads` is offered at the end, not done automatically.
