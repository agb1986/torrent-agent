# torrent-agent

A Claude AI agent that finds a torrent for what you ask for and adds it to
Deluge — refusing to download unless your VPN is active.

## How it works

```
request ──▶ Claude (tool-use loop)
                │
                ├─ search_torrents ─▶ Prowlarr  (preferred)
                │                     └ apibay / TPB  (zero-setup fallback)
                │                        └ rank: resolution ▸ codec ▸ seeders ▸ recency
                ├─ check_vpn       ─▶ piactl (PIA), with tunnel-interface fallback
                └─ add_torrent     ─▶ Deluge daemon RPC (refuses if VPN is down)
```

The model picks the best release; magnet URIs stay server-side (referenced by a
short id) so they never bloat the context window.

## Setup

```bash
virtualenv .venv && source .venv/bin/activate   # NOT python -m venv: this box has no ensurepip
pip install -r requirements.txt
cp config.example.toml config.toml      # then edit
export ANTHROPIC_API_KEY=sk-ant-...
```

### Deluge daemon (required)

This machine has the Deluge GTK app but not the daemon. Install and start it:

```bash
sudo apt install deluged deluge-console
deluged                 # starts the daemon on 127.0.0.1:58846
```

The agent reads `~/.config/deluge/auth` for the local `localclient` credentials
automatically (created on first daemon run), so you usually don't need to set
`username`/`password` in `config.toml`.

### Search backend

Out of the box the agent scrapes The Pirate Bay via `apibay` — no setup. For
broader, more reliable coverage, run **Prowlarr** and set `search.prowlarr.url`
and `api_key` in `config.toml`; the agent then prefers it automatically
(`backend = "auto"`).

### VPN

VPN detection uses PIA's `piactl`. "Active" means PIA reports `Connected` on
**this** machine — the same machine Deluge runs on. If `piactl` is unavailable
the agent falls back to checking for a `tun*`/`wg*` tunnel interface.

That check only gates the *moment of adding*, though. For protection against
the VPN dropping mid-download, Deluge's peer sockets are bound to the tunnel
device itself:

```bash
python scripts/bind_vpn.py          # detect tunnel + bind (start.sh does this)
python scripts/bind_vpn.py --check  # verify; exit 1 if unbound or mismatched
```

Bound, a VPN drop stops transfers dead. Unbound, they silently continue over
your LAN — PIA leaves the normal default route in place beneath its own, so
there is no error when the tunnel disappears. Binding is by interface *name*,
which survives the address change on every reconnect.

## Usage

```bash
python -m torrent_agent "the bear season 3 1080p"
python -m torrent_agent                 # prompts interactively
python -m torrent_agent -c other.toml "dune part two 2024"
```

If your VPN is off, the agent finds the torrent but stops and tells you to start
PIA first — it will not add the download.

## Preferences

Edit the `[preferences]` block in `config.toml`: resolution order, preferred
codecs, max single-episode size, and the seeder floor. See
`config.example.toml` for the annotated defaults.

## Media pipeline (beyond fetching)

The repo also carries the delivery half of the pipeline, driven by Claude Code
skills (`.claude/skills/`): check download progress (`scripts/status.py`),
tidy finished downloads into clean names, rsync them to the CASAOS server
(`scripts/transfer.py`), and notify **Jellyfin** so the new media appears
without a manual library scan. Server destinations and Jellyfin settings live
in the `[server]` and `[jellyfin]` blocks of `config.toml`; the Jellyfin API
key is read from `JELLYFIN_API_KEY`.

Tidied names carry a TMDB id — `One Piece (1999) [tmdbid-37854]/Season 01/…`
for TV, `Withnail and I (1987) [tmdbid-13446].mkv` for films — which Jellyfin
reads instead of guessing from the title (its guess picks the 2023 live-action
One Piece, not the 1999 anime). `scripts/tmdb_id.py` resolves the id; it needs
no API key, falling back to Wikidata, but will use TMDB directly if
`TMDB_API_KEY` is set.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

## Layout

| File | Role |
|------|------|
| `agent.py`   | Claude tool-use loop + tool definitions |
| `search.py`  | Prowlarr / apibay clients → normalized results |
| `ranking.py` | `guessit` parse + scoring |
| `vpn.py`     | PIA / interface VPN check + tunnel device detection |
| `deluge.py`  | daemon RPC: shared connect + add magnet |
| `config.py`  | defaults ← `config.toml` ← env |
| `cli.py`     | entrypoint |
| `scripts/status.py` | Deluge download status table |
| `scripts/bind_vpn.py` | pin Deluge's sockets to the VPN tunnel (kill switch) |
| `scripts/transfer.py` | rsync to server + Jellyfin scan |
| `scripts/tmdb_id.py` | resolve a title → TMDB id for Jellyfin-readable names |
| `scripts/remove_seeding.py` | drop Seeding torrents, or specific ones with `--id` (keeps data) |
