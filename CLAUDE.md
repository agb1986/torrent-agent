# CLAUDE.md

Guidance for working in this repo. See `README.md` for user-facing docs and the
file-by-file layout table.

## What this is

A Claude tool-use agent: takes a request (TV/movie), searches indexers, ranks
results, and adds the best one(s) to Deluge — **refusing to add anything unless
the VPN is active**. Entry point: `python -m torrent_agent "<request>"`.
The model drives a search → rank → check_vpn → add_torrent loop (`agent.py`);
magnets are kept server-side keyed by short ids (`r1`…) so they stay out of the
context window.

## Running it (non-obvious setup)

- **Virtualenv:** create with `virtualenv .venv`, NOT `python -m venv` — this box
  has no `ensurepip`, so `python -m venv` fails. Deps: `.venv/bin/pip install -r requirements.txt`.
- **API key:** `ANTHROPIC_API_KEY` lives in `~/.bashrc`, behind the standard
  non-interactive `return` guard, so only an **interactive** shell sees it.
  Tool/CI shells are non-interactive and won't. Run the agent via an interactive
  shell or source a local `.env`:
  ```bash
  bash -ic 'set -a; . .env 2>/dev/null; set +a; .venv/bin/python -m torrent_agent "<request>"'
  ```
  `.env` (gitignored) can hold `ANTHROPIC_API_KEY=...`.
- **Deluge daemon:** `deluged` must be running on `127.0.0.1:58846`. Creds are
  read from `~/.config/deluge/auth` automatically.
- **VPN gate:** a tunnel must be carrying traffic or `add_torrent` refuses.
  The host is on **ProtonVPN** (`proton0`) as of Aug 2026; PIA is still
  installed but idle. `provider = "pia"` in config means "the host tunnel", not
  the vendor — `vpn_status` only trusts `piactl` when it says `Connected`, and
  otherwise falls through to a route check that covers Proton and anything
  else. Bring the stack up with `scripts/start.sh` (VPN → bind → deluged →
  compose).
- **VPN binding (the actual kill switch):** the `check_vpn` gate only covers
  add-time. `scripts/bind_vpn.py` pins Deluge's `listen_interface` and
  `outgoing_interface` to the live tunnel **device**, so a mid-download VPN
  drop kills transfers instead of rerouting them. `start.sh` applies it via
  `deluge_bind_vpn` before `deluge_up`; `--check` verifies (also in `health`).
- **Forwarded port (gluetun stack only):** gluetun negotiates a port and
  firewalls exactly that one, while Deluge ships `random_port: true` and
  listens elsewhere — result is **zero inbound peers and no error anywhere**.
  `scripts/sync_pf_port.py` reads `GET /v1/portforward` and sets Deluge's
  `listen_ports` over RPC, turning `random_port` off so it cannot drift back.
  Proton's NAT-PMP lease rotates, so run `--watch` alongside the stack;
  `--check` reports and exits 1 on mismatch.

## The unattended layer (`server/`)

Four systemd **user** units run on the CasaOS server — not the laptop, which
was the rehearsal and is now torn down (data kept, see `~/srv-rehearsal`).
Units live in `deploy/systemd/`, and their paths assume
`~/workspace/repos/torrent-agent`; the server checkout is at
`~/workspace/torrent-agent`, so installs `sed` the path.

| Unit | Does |
|---|---|
| `bot` | Telegram: `/get`, `/status`, `/cancel`, `/sub` |
| `notifier` | Watches Deluge; announces completions, and with `TORRENT_AGENT_AUTODELIVER=1` runs the whole delivery pipeline |
| `sub` | Reconciles followed series — fetches episodes as they air |
| `pfsync` | Keeps Deluge's listen port on gluetun's rotating forwarded port |

Plus three **timers** (oneshot service + `.timer`, enabled via the timer, not
the service): `doctor` (health check, alerts on change), `backup` (state
snapshot), `prune` (disk reclaim, **disarmed by default**).

- **After `git pull` on the server, restart *all four*.** Python holds the old
  module in memory, and this has bitten twice: a fix deployed but not running
  looks exactly like a fix that did not work. `deploy/update.sh` does it, and
  `deploy/install-hooks.sh` installs a `post-merge` hook so a bare `git pull`
  does too. update.sh also re-renders installed unit files — they are
  `__REPO__` templates expanded at install time, so a unit change in git is
  invisible until someone reinstalls it.
- **`scripts/prune.py` is the only code here that deletes the user's media.**
  Three independent brakes, all load-bearing: `[prune] enabled` is false by
  default; it exits early while free space is above `min_free_gb`; and a
  candidate must satisfy *both* `min_seed_hours` and `min_ratio` — either
  alone is a bad rule, since a popular release hits ratio 2.0 in an hour and
  an unpopular one never does. It stops as soon as the free-space target is
  met, longest-seeded first (not largest — that greedily eats box sets). Don't
  "simplify" any of these away.
- **`doctor_alert.py` messages on change, never on state.** Persisted failing
  set in `tmp/doctor_alerts.json`; a fault identical to yesterday's sends
  nothing. A daily "still broken" is how a monitor becomes something you swipe
  away. It also refuses to record state when the send failed — otherwise an
  undelivered alert counts as old news and is never re-sent.
- **`backup.py` excludes Deluge's `auth`.** Same reasoning as the fixture
  rule: an archive gets copied around far more casually than the file it came
  from. Media is excluded too — a backup that cannot finish is not a backup.
- **`server/pipeline.py` refuses rather than guesses.** `torrent_agent/tidy.py`
  builds a plan and is confident only with one show name across all files, a
  TVmaze episode for each, and an unambiguous TMDB id. Short of that nothing
  moves and the user is told why — a confident wrong answer files a show under
  the wrong programme and is invisible until someone watches it. Do not "fix"
  an escalation by loosening the checks.
- **`/sub` reconciles, it does not schedule.** No cron: the watcher asks "which
  episodes have aired and are missing?" each tick, so a missed tick costs a
  delay rather than a season. IMDb links only — an ambiguous subscription
  fetches the wrong programme for weeks.
- **The bot's Telegram traffic goes through gluetun's HTTP proxy**
  (`127.0.0.1:8888`, `HTTPS_PROXY` in `.env.bot`). The ISP blocks
  `api.telegram.org`; the laptop never noticed because its traffic already went
  via ProtonVPN. `NO_PROXY=127.0.0.1,localhost` is load-bearing — `vpn.py` uses
  urllib, which honours those vars and would otherwise proxy its own control
  server call.
- **Never put a real credential in a test fixture.** A test written to prove
  the bot token is never leaked used the live token, and publishing it to a
  public repo tripped GitHub secret scanning. Fixtures look like credentials
  without being them.

## Search stack

Prowlarr (preferred) → apibay/TPB fallback, set in `config.toml`
(`config.example.toml` is the template; `config.toml` is gitignored — holds the
Prowlarr API key). Prowlarr + FlareSolverr run via `docker-compose.yml`.

- CloudFlare-protected indexers (1337x, EZTV) must carry the Prowlarr **`cf`
  tag** so they route through FlareSolverr; without it they fail with
  "blocked by CloudFlare Protection". YTS needs no tag.

## Gotchas learned the hard way

- **Bind Deluge by interface NAME, never by IP.** Verified live: a PIA
  reconnect moved the tunnel from `10.11.3.195` to `10.189.2.207`, and
  `piactl get vpnip` reports the *public exit* IP (155.2.x.x), which is on no
  local interface. A name binding survived the reconnect and libtorrent
  rebound the listen socket by itself — **no deluged restart needed** after a
  VPN flap.
- **Why an unbound Deluge leaks:** PIA overlays `0.0.0.0/1` + `128.0.0.0/1` on
  tun0 and leaves the LAN default route intact underneath. When the tunnel
  drops those two routes vanish and traffic silently resumes over `wlp1s0` —
  no error, no interruption. Confirmed by test: bound, the byte counter froze
  while disconnected even though the route had flipped to the LAN device.
  The same reasoning applies to Proton — the device name changes (`proton0`),
  the argument does not, which is why the check is route-based rather than
  vendor-specific.
- **ProtonVPN breaks `casaos.local` (and every other `.local` name).** Proton
  takes DNS over completely: the LAN link is left with *no* resolver and all
  queries go to systemd-resolved, which — with mDNS disabled — answers `.local`
  with an authoritative NXDOMAIN instead of routing it to multicast. avahi
  knows the answer the whole time; nothing ever asks it. PIA never did this,
  because it left the LAN link's DNS intact under its split routes. Symptom:
  `scripts/transfer.py` and the Jellyfin scan fail while the server is
  perfectly healthy by IP. Fix:
  `sudo resolvectl mdns wlp1s0 yes` plus `MulticastDNS=yes` in
  `/etc/systemd/resolved.conf.d/mdns.conf` to survive reboot. Note it then
  resolves to an IPv6 link-local address, which is fine for ssh/rsync/curl.
  Both steps, and the verification, are in `deploy/host/`.
- **A dead binding looks identical to a good one.** After the Proton switch
  Deluge was still bound to `tun0`, which no longer existed. Nothing errored:
  transfers simply never started, which fails safe but is invisible. Only
  `bind_vpn.py --check` catches it — run it after *any* VPN change. Beware
  checking its exit code through a pipe: `... | tail; echo $?` reports the
  exit code of `tail`, not the script.
- **Deluge's `core.conf` is not plain JSON** — it's two concatenated objects
  (`{"file","format"}` then the settings). `json.load()` fails on it; use
  `JSONDecoder.raw_decode` twice (see `deluge._binding_from_file`). Also,
  deluged rewrites the file on shutdown, so edits to a *running* daemon's
  config get clobbered — set it over RPC instead (`bind_vpn.py` does both).

- **EZTV magnets:** Prowlarr returns EZTV's `magnetUrl` as a *redirect* URL
  (`http://localhost:9696/...`), not a real magnet — the actual magnet is in the
  `guid` field. `search.py` only accepts a value starting with `magnet:`
  (magnetUrl → guid), else rebuilds from `infoHash`. Don't revert this or adds
  fail with `Unsupported scheme: b''`.
- **1337x throttling:** heavy querying trips CloudFlare to a hard `403`; it
  recovers on its own. A missing "complete season pack" is usually 1337x being
  down, leaving only EZTV's individual episodes.
- **Codec preference:** `guessit` emits `H.265`/`H.264`; configs say
  `h265`/`x265`/`hevc`. `ranking._codec_matches` strips non-alphanumerics on both
  sides so they compare equal — don't substring-match raw.
- **Multi-add:** the system prompt picks a single best release by default, but the
  model will add one torrent per episode when explicitly asked for "all episodes".

## Media server (CASAOS + Jellyfin)

- `scripts/transfer.py` rsyncs to the CASAOS server and then notifies
  **Jellyfin** (`http://casaos.local:8096`, host networking) via
  `POST /Library/Media/Updated`. Server/Jellyfin settings live in the
  `[server]`/`[jellyfin]` blocks of `config.toml`; the API key comes from
  `JELLYFIN_API_KEY` (behind the same interactive-shell guard as the other
  keys).
- The Jellyfin container mounts libraries **separately**:
  `/mnt/data/tv → /media/tv`, `/mnt/data/film → /media/movies`. It is not a
  single-prefix map — path translation uses `[jellyfin.path_map]`.
- **TMDB ids in names.** `tidy-files` names output
  `One Piece (1999) [tmdbid-37854]/` (TV) and
  `Withnail and I (1987) [tmdbid-13446].mkv` (film) so Jellyfin matches by id
  rather than guessing. `scripts/tmdb_id.py` resolves it: TMDB API if
  `TMDB_API_KEY` is set, else **Wikidata** — `P4947` (film) / `P4983` (TV),
  which double as the type filter since a film never carries P4983. Use
  Wikidata's plain API (`wbsearchentities`, and `haswbstatement:P345=<imdb>`
  for the exact IMDb route), **not** SPARQL: `query.wikidata.org` returns 502s
  and 20s timeouts often enough to be unusable. Pass TVmaze's
  `externals.imdb` when you have it — title search alone can't tell a film
  from its remake.
- **Shows already on the server are untagged** — rsyncing a newly tagged
  directory alongside an old bare `Show Name/` gives Jellyfin two entries with
  the episodes split. Rename the remote directory first (ask before touching
  the user's library).

## Conventions

- Stdlib + `requests`/`anthropic`/`deluge-client`/`guessit` only; no framework
  (`pytest` is dev-only).
- Models: default `claude-opus-5`, adaptive thinking, effort high, top-level
  prompt caching (`agent.py`).
- Search result ids are namespaced per search (`s1r1`, `s2r1`, …) and the
  registry is append-only — don't revert to bare `r1` ids, stale ids from an
  earlier search would silently resolve to the wrong torrent.
- Tests: `.venv/bin/python -m pytest tests/`. After touching search/ranking,
  also sanity-check against live Prowlarr — several bugs here only show up
  with real indexer responses.
