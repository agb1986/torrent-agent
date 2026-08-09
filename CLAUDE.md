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
- **VPN gate:** PIA must report `Connected` (`piactl`) or `add_torrent` refuses.
  Bring the whole stack up with `scripts/start.sh` (PIA → bind → deluged →
  compose).
- **VPN binding (the actual kill switch):** the `check_vpn` gate only covers
  add-time. `scripts/bind_vpn.py` pins Deluge's `listen_interface` and
  `outgoing_interface` to the live tunnel **device**, so a mid-download VPN
  drop kills transfers instead of rerouting them. `start.sh` applies it via
  `deluge_bind_vpn` before `deluge_up`; `--check` verifies (also in `health`).

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
