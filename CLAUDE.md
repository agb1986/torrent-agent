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
  Bring the whole stack up with `scripts/start.sh` (PIA → deluged → compose).

## Search stack

Prowlarr (preferred) → apibay/TPB fallback, set in `config.toml`
(`config.example.toml` is the template; `config.toml` is gitignored — holds the
Prowlarr API key). Prowlarr + FlareSolverr run via `docker-compose.yml`.

- CloudFlare-protected indexers (1337x, EZTV) must carry the Prowlarr **`cf`
  tag** so they route through FlareSolverr; without it they fail with
  "blocked by CloudFlare Protection". YTS needs no tag.

## Gotchas learned the hard way

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

## Conventions

- Stdlib + `requests`/`anthropic`/`deluge-client`/`guessit` only; no framework.
- Models: default `claude-opus-4-8`, adaptive thinking, effort high (`agent.py`).
- After touching search/ranking, sanity-check against live Prowlarr rather than
  only unit tests — several bugs here only show up with real indexer responses.
