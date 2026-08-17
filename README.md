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
                ├─ check_vpn       ─▶ host tunnel (route check) or gluetun control server
                └─ add_torrent     ─▶ Deluge daemon RPC (refuses if VPN is down)
```

The model picks the best release; magnet URIs stay server-side (referenced by a
short id) so they never bloat the context window.

A request can be an IMDb link (`https://www.imdb.com/title/tt0995832/`) or a
bare `tt0995832`, which is resolved to a title and year before searching —
useful when the name alone is ambiguous (*Fargo* is a 1996 film and a 2014
series).

Two ways to drive it: the CLI below, or a **Telegram bot** that runs as a
service and can also tidy and deliver finished downloads on its own. See
[Remote control](#remote-control-telegram).

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

Two arrangements, set by `[vpn] provider`:

- **`pia`** — the tunnel on *this* machine, whatever carries it. The name is
  historical; read it as "host". `piactl` is trusted only when it reports
  `Connected`; otherwise the check asks the kernel which device is actually
  carrying traffic, which covers ProtonVPN (`proton0`), WireGuard and anything
  else. Asking the route rather than looking for a `tun*` interface matters: a
  stale interface left behind by a dead session would otherwise read as
  protected.
- **`gluetun`** — Deluge runs inside the VPN container's network namespace and
  has no other route out, so containment is structural rather than a setting.
  Status comes from gluetun's control server. See
  `deploy/server/docker-compose.yml`.

Under `pia`, that check only gates the *moment of adding*. For protection
against the VPN dropping mid-download, Deluge's peer sockets are bound to the
tunnel device itself:

```bash
python scripts/bind_vpn.py          # detect tunnel + bind (start.sh does this)
python scripts/bind_vpn.py --check  # verify; exit 1 if unbound or mismatched
```

Bound, a VPN drop stops transfers dead. Unbound, they silently continue over
your LAN — a VPN client typically leaves the normal default route in place
beneath its own, so there is no error when the tunnel disappears. Binding is by
interface *name*, which survives the address change on every reconnect.

Under `gluetun` there is nothing to bind: stopping the VPN container destroys
the namespace Deluge lives in, so transfers fail rather than reroute. That
stack also needs its forwarded port kept in step with Deluge's listen port —
they do not agree by default, which costs you every inbound peer with nothing
in any log to say so:

```bash
python scripts/sync_pf_port.py --check   # exit 1 on mismatch
python scripts/sync_pf_port.py --watch   # resync as the lease rotates
```

## Usage

```bash
python -m torrent_agent "the bear season 3 1080p"
python -m torrent_agent                 # prompts interactively
python -m torrent_agent -c other.toml "dune part two 2024"
python -m torrent_agent "https://www.imdb.com/title/tt0903747/"
```

If your VPN is off, the agent finds the torrent but stops and tells you to
start the VPN first — it will not add the download.

## Remote control (Telegram)

`server/` is a long-polling Telegram bot, so it needs no inbound port and works
behind NAT. Three commands: `/get <title or IMDb link>`, `/status` (the live
Deluge session), `/cancel`.

```bash
cp .env.bot.example .env.bot && chmod 600 .env.bot   # token + allowlist
python -m server.chat_id                             # find your chat id
python -m server.bot
```

Following a running series:

```
/sub https://www.imdb.com/title/tt10986410/   follow (IMDb links only)
/sub list                                     progress and next air date
/sub stop tt10986410                          stop following
```

`server/sub.py` reconciles rather than schedules: on each tick it asks which
episodes of a followed show have aired and are not here yet, so a missed tick
costs a delay rather than a season. It catches up on episodes that aired
before you subscribed, paces itself so a backlog does not exhaust the daily
budget at once, retries every 12h when no release exists yet, matches the
first episode's resolution and release group, and unsubscribes when the season
ends. IMDb links only — an ambiguous subscription would fetch the wrong
programme for weeks.

Guard rails, because it holds an API key unattended: an allowlist checked
before anything is parsed, a per-day request cap that survives restarts and
counts attempts rather than successes, and a JSONL audit log.

A companion watcher reports finished downloads, and optionally files them:

```bash
python -m server.notifier             # announce completions
python -m server.notifier --deliver   # also tidy, deliver, tell Jellyfin
```

`--deliver` is opt-in because filing things into a media library is only safe
on the machine the library lives on. It refuses to act whenever it is not
certain how to name something, leaving the download untouched and explaining
why — a confident wrong answer files a show under the wrong programme, and
nobody notices until they try to watch it.

Run all three under systemd: see [`deploy/systemd/`](deploy/systemd/README.md).

## Preferences

Edit the `[preferences]` block in `config.toml`: resolution order, preferred
codecs, max single-episode size, and the seeder floor. See
`config.example.toml` for the annotated defaults.

## Media pipeline (beyond fetching)

The repo also carries the delivery half: rename a finished download into a
layout Jellyfin matches on (`scripts/tidy.py`), put it in the library
(`scripts/transfer.py`), and tell Jellyfin so it appears without a manual
scan. Each runs standalone, is driven end to end by `server/pipeline.py` when
the notifier has `--deliver`, or by the Claude Code skills in `.claude/skills/`.

`transfer.py` picks its own mode: a **local move** when the configured server
is this machine — downloads and the library are then one filesystem, so it is
a rename rather than a copy — and rsync over SSH otherwise. Server
destinations and Jellyfin settings live in the `[server]` and `[jellyfin]`
blocks of `config.toml`; the Jellyfin API key is read from
`JELLYFIN_API_KEY`.

Tidying refuses rather than guesses. It needs one show name across every file,
a TVmaze episode for each, and an unambiguous TMDB id; short of that it
changes nothing and says what was unclear.

Tidied names carry a TMDB id — `One Piece (1999) [tmdbid-37854]/Season 01/…`
for TV, `Withnail and I (1987) [tmdbid-13446].mkv` for films — which Jellyfin
reads instead of guessing from the title (its guess picks the 2023 live-action
One Piece, not the 1999 anime). `scripts/tmdb_id.py` resolves the id; it needs
no API key, falling back to Wikidata, but will use TMDB directly if
`TMDB_API_KEY` is set.

## Housekeeping (unattended)

Two periodic jobs, installed as systemd **timers** by
`deploy/install-units.sh` alongside the long-running services. Each also runs
by hand.

| Timer | Does | Armed? |
|---|---|---|
| `doctor` | Runs `scripts/doctor.py` daily and messages Telegram **when the result changes** | yes |
| `prune` | Removes torrents that have finished seeding, to reclaim disk | **no** — see below |

**The doctor alert messages on change, not on state.** A check that starts
failing gets a message; one that stops failing gets a message saying so; the
same failures as yesterday get silence. That is what makes an empty inbox mean
"healthy" rather than "I stopped reading these".

**Pruning is off until you turn it on.** It is the only thing here that
deletes your media, so installing the timer arms nothing — it reports into the
journal until `[prune] enabled = true`. It does nothing at all while there is
more than `min_free_gb` free, and a torrent is only a candidate once it has
**both** seeded `min_seed_hours` and reached `min_ratio` — either test alone is
wrong, since a popular release hits ratio 2.0 within the hour and an unpopular
one never does. Watch it first:

```bash
python scripts/prune.py           # says what it would take, removes nothing
journalctl --user -u torrent-agent-prune
```

## Keeping a deployment current

```bash
./deploy/update.sh          # pull, test, re-render units, restart everything
./deploy/install-hooks.sh   # once per machine: do that automatically on pull
```

`update.sh` restarts **every** service rather than the ones that look changed:
Python holds the old module in memory, so "pulled but not restarted" produces
exactly the same symptom as "the fix did not work". It also re-renders any
installed unit file, since the units are templates expanded at install time
and a change to one in git is otherwise invisible. The `post-merge` hook runs
the same path after any `git pull`, and is silent on a machine with no units
installed.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

## Layout

| File | Role |
|------|------|
| `torrent_agent/agent.py`   | Claude tool-use loop + tool definitions |
| `torrent_agent/search.py`  | Prowlarr / apibay clients → normalized results |
| `torrent_agent/ranking.py` | `guessit` parse + scoring |
| `torrent_agent/vpn.py`     | host-tunnel / gluetun VPN check + tunnel device detection |
| `torrent_agent/deluge.py`  | daemon RPC: shared connect, add magnet, list torrents |
| `torrent_agent/config.py`  | defaults ← `config.toml` ← env |
| `torrent_agent/imdb.py`    | IMDb link or `tt…` id → searchable title and year |
| `torrent_agent/tidy.py`    | plan a rename, and refuse when anything is unclear |
| `torrent_agent/cli.py`     | entrypoint |
| `server/bot.py` | Telegram bot: `/get`, `/status`, `/cancel` |
| `server/sub.py` | follow a running series; fetch episodes as they air |
| `server/notifier.py` | watch for finished downloads; announce or deliver |
| `server/pipeline.py` | finished → out of Deluge → tidy → deliver → Jellyfin |
| `server/runner.py` | invoke the agent, plus the daily spend cap |
| `server/telegram.py` | minimal Bot API client (long polling) |
| `scripts/status.py` | Deluge download status table |
| `scripts/bind_vpn.py` | pin Deluge's sockets to the VPN tunnel (kill switch) |
| `scripts/sync_pf_port.py` | keep Deluge's listen port on gluetun's forwarded port |
| `scripts/tidy.py` | CLI for the rename plan (`--dry-run`) |
| `scripts/transfer.py` | deliver to the library (local move or rsync) + Jellyfin scan |
| `scripts/tmdb_id.py` | resolve a title → TMDB id for Jellyfin-readable names |
| `scripts/remove_seeding.py` | drop Seeding torrents, or specific ones with `--id` (keeps data) |
| `scripts/doctor.py` | 12 checks for the wiring between components, with fixes |
| `scripts/doctor_alert.py` | run the doctor on a timer; Telegram on *change* only |
| `scripts/prune.py` | reclaim disk from torrents that have finished seeding (opt-in) |
| `deploy/server/` | gluetun + Deluge compose, with Deluge inside the tunnel |
| `deploy/server/.env.example` | where data lives, and which VPN — no secrets |
| `deploy/systemd/` | user units: four services, plus timers for the housekeeping |
| `deploy/hooks/` | `post-merge`: restart the stack after a pull, so deployed means running |
| `deploy/host/` | machine-level setup the stack needs but cannot apply itself |

## License

MIT — see [`LICENSE`](LICENSE).
