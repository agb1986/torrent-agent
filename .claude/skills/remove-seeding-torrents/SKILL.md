---
name: remove-seeding-torrents
description: Remove all torrents in the Seeding state from Deluge without deleting downloaded data. Use when the user wants to clean up seeding torrents, stop seeding, or remove completed torrents from Deluge.
---

# Remove Seeding Torrents

Remove every torrent currently in the **Seeding** state from Deluge.
Downloaded data is kept on disk — only the torrent entry is removed.

## Step 1 — Run the script

Always run from the project root so `config.toml` is found automatically:

```bash
cd /home/agb86/workspace/repos/torrent-agent && .venv/bin/python scripts/remove_seeding.py
```

## Step 2 — Interpret the output

If any torrents were removed, the first output line is an internal logging
artifact path (an absolute path ending `.json`, written for ai-data-store) —
skip it and relay only the lines that follow.

| Output | What to tell the user |
|--------|----------------------|
| `Removed N seeding torrent(s):` followed by names | Confirm how many were removed and list them. |
| `No seeding torrents found.` | Tell the user Deluge has nothing currently seeding. |
| `DelugeError: Could not connect…` | Tell the user to start `deluged` (see `scripts/start.sh`). |
| `Failed to remove N:` section | Relay the names and errors; partial success is still a success for the ones that went through. |

## Notes

- Data is **never** deleted — `remove_data=False` is hardcoded in the script.
- No VPN check needed; this is a local Deluge operation only.
- Always use `.venv/bin/python`, not the system Python.
