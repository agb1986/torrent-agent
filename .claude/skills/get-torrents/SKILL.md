---
name: get-torrents
description: Search for a torrent and add it to Deluge via the torrent-agent. Use when the user wants to download, fetch, or grab a TV show, movie, or any media — or when they ask you to find and add a torrent.
---

# Get Torrents

Run the torrent-agent to search indexers, rank results, and add the best match
to Deluge.  The agent handles the full search → rank → VPN check → add loop.

## Step 1 — Determine the request

The request comes from the args passed to this skill, or from the user's most
recent message if no args were given.  If neither is clear, ask the user what
they want to download before proceeding.

## Step 2 — Run the agent

Always run from the project root (`/home/agb86/workspace/repos/torrent-agent`).
Source `.env` before invoking so `ANTHROPIC_API_KEY` is available to the
subprocess (it is behind the non-interactive guard in `~/.bashrc`):

```bash
bash -ic 'set -a; . .env 2>/dev/null; set +a; .venv/bin/python -m torrent_agent "<request>"'
```

Replace `<request>` with the full request string, properly shell-quoted.

Example:
```bash
bash -ic 'set -a; . .env 2>/dev/null; set +a; .venv/bin/python -m torrent_agent "The Bear S03 1080p"'
```

## Step 3 — Interpret and relay the output

The agent prints a plain-text summary.  Relay it to the user as-is — **except**
when a torrent was added: the first output line is then an internal logging
artifact path (an absolute path ending `.json`, written for ai-data-store).
Skip that line and relay only the summary that follows it.

Common outcomes and what to tell the user:

| Outcome | What the agent prints | What to say |
|---|---|---|
| Torrent added | "Added: <title>" | Confirm it's in Deluge, mention title and resolution chosen. |
| VPN not active | "VPN is not active…" | Tell the user to start PIA (`piactl connect`) and re-run. |
| Nothing found | "No matching torrents found." | Suggest a broader query or check indexer health. |
| API key missing | `RuntimeError: ANTHROPIC_API_KEY` | Tell the user to add it to `.env` in the project root. |
| Deluge unreachable | `DelugeError: …` | Tell the user to start `deluged` (see `scripts/start.sh`). |

## Step 4 — Offer follow-up

After a successful add, offer to search for more (e.g. other episodes of the
same show) or confirm the download is progressing in Deluge.

## Notes

- **Virtualenv:** always use `.venv/bin/python`, never the system `python`. The
  box has no `ensurepip` so the venv was created with `virtualenv`, not
  `python -m venv`.
- **Multi-episode:** if the user asks for "all episodes" or "the whole season",
  pass that phrasing verbatim — the agent will add one torrent per episode.
- **VPN guard:** the agent refuses to add anything if PIA is down, even if asked.
  Do not try to work around this.
