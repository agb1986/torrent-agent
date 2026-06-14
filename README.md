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
python3 -m venv .venv && source .venv/bin/activate
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

## Layout

| File | Role |
|------|------|
| `agent.py`   | Claude tool-use loop + tool definitions |
| `search.py`  | Prowlarr / apibay clients → normalized results |
| `ranking.py` | `guessit` parse + scoring |
| `vpn.py`     | PIA / interface VPN check |
| `deluge.py`  | add magnet via daemon RPC |
| `config.py`  | defaults ← `config.toml` ← env |
| `cli.py`     | entrypoint |
