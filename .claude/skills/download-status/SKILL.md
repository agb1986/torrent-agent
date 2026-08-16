---
name: download-status
description: Show the status of downloads in Deluge - state, progress, speed, and ETA for every torrent. Use when the user asks how a download is going, whether something has finished, what is downloading, or for a Deluge status overview.
---

# Download Status

Show the state of every torrent in Deluge: name, state, progress %, download
rate, ETA, and size.

## Step 1 — Run the script

Always run from the project root:

```bash
cd <project root> && .venv/bin/python scripts/status.py
```

## Step 2 — Interpret the output

The script prints a table sorted with active downloads first.

| Output | What to tell the user |
|--------|----------------------|
| Rows in `Downloading` state | Report progress %, rate, and ETA for each. |
| Everything `Seeding` / 100% | The downloads are complete; offer **tidy-files** / **transfer-files** as the next step, or **remove-seeding-torrents** to clean up. |
| `No torrents in Deluge.` | Nothing is queued — offer to fetch something with **get-torrents**. |
| `error: Could not connect to the Deluge daemon…` | Tell the user to start `deluged` (see `scripts/start.sh`). |

## Notes

- Read-only: this never adds, pauses, or removes anything.
- No VPN check needed; it is a local Deluge query.
- Always use `.venv/bin/python`, not the system Python.
- If the user is waiting on a specific download, filter your summary to that
  torrent rather than relaying the whole table.
