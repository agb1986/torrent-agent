# systemd user units

Long-running services that previously had nothing keeping them alive:

| Unit | What it does | Why it must not die quietly |
|---|---|---|
| `torrent-agent-bot.service` | The Telegram control bot (`python -m server.bot`) | A dead bot is invisible — messages simply go unanswered |
| `torrent-agent-pfsync.service` | `scripts/sync_pf_port.py --watch` | Proton's NAT-PMP lease rotates; when it moves and nothing re-syncs, Deluge silently drops to **zero inbound peers** with nothing in any log |
| `torrent-agent-sub.service` | Fetches new episodes of followed series (`/sub`) | Nothing else watches a running show; without it, following a series means remembering to ask each week |
| `torrent-agent-notifier.service` | Watches Deluge, messages Telegram on completion | Nothing else polls: the `fetch-to-jellyfin` skill only watches while a session drives it, and Deluge's Execute plugin is disabled. Downloads finished and sat in `complete/` with nobody told |

And two periodic ones, each a `Type=oneshot` service plus a `.timer`:

| Timer | What it does | Why on a schedule |
|---|---|---|
| `torrent-agent-doctor.timer` | `scripts/doctor_alert.py` daily | Every check in the doctor is for something that fails *silently*. Until this existed they only ran when a human thought to type them — which is the moment they are least likely to be needed |
| `torrent-agent-prune.timer` | `scripts/prune.py --apply` daily | Disk fills monotonically otherwise. **Deletes nothing** until `[prune] enabled = true` — see below |

It is the **timer** that gets enabled, not the oneshot service. The oneshots
carry no `[Install]` section on purpose: enabling one directly would try to run
it at boot and never again, and `systemctl enable` on it fails in a way that
reads like a broken unit rather than a deliberate one.

**User** units, not system ones, so no root is needed and they run as the
account that owns `~/.config/deluge` and the virtualenv.

## Install

```bash
./deploy/install-units.sh
systemctl --user enable --now torrent-agent-bot torrent-agent-notifier \
  torrent-agent-pfsync torrent-agent-sub
systemctl --user enable --now torrent-agent-doctor.timer \
  torrent-agent-prune.timer
```

The units are templates carrying `__REPO__`; `install-units.sh` substitutes
the checkout it is run from, so they work wherever the repo lives and cannot
drift apart. Re-run it after moving the checkout.

To survive logout and start at boot without logging in (needs root once):

```bash
sudo loginctl enable-linger "$USER"
```

## Update

```bash
./deploy/update.sh          # pull, test, re-render units, restart every service
./deploy/install-hooks.sh   # once: do that automatically after any git pull
```

Restarts all four rather than the ones that look changed: Python holds the old
module in memory after a pull, and a fix that is deployed but not running looks
exactly like a fix that did not work.

It also re-runs `install-units.sh` for whatever is already installed here. The
units are templates expanded at install time, so a change to one in git stays
invisible until it is rendered again — the same failure as the stale module,
one level down. Only units already present are touched, so running it on a
laptop installs nothing (and does not start a second bot against the same
Telegram token).

## Watch them

```bash
systemctl --user status torrent-agent-bot
journalctl --user -u torrent-agent-pfsync -f
systemctl --user list-timers 'torrent-agent-*'
journalctl --user -u torrent-agent-prune      # what it would have taken
```

## Notes

- The bot reads `.env.bot` via `EnvironmentFile`, which **must** contain
  `ANTHROPIC_API_KEY`. systemd cannot source `~/.bashrc`, where the CLI
  normally finds it behind the interactive-shell guard. If you ever go back to
  launching by hand with `bash -ic`, comment that line out again — see the note
  in `.env.bot.example`.
- `Restart=on-failure` for the bot, deliberately: it exits 1 on a missing token
  or an empty allowlist, and restarting into the same misconfiguration forever
  would bury the reason. `Restart=always` for pfsync, which has no such exit
  and is expected to outlive gluetun restarts.
- pfsync only makes sense for the **gluetun** stack — a host VPN has no control
  server to ask. It sets `TORRENT_AGENT_CONFIG` in the unit, so point that at
  whichever config describes the gluetun stack on this machine: `config.toml`
  on the server, `config.rehearsal.toml` on a laptop running the rehearsal
  stack beside a native Deluge.
- The notifier takes `TORRENT_AGENT_AUTODELIVER=1` (in `.env.bot`) to tidy and
  file finished downloads rather than only announcing them. Only set it on the
  machine the media library actually lives on.
- The oneshots have no `Restart=`. For the doctor a non-zero exit is the
  *normal* result of a failing check, and retrying it would send nothing new
  while filling the journal — the timer is the retry. Both set
  `Persistent=true`, so a run missed while the box was asleep happens at the
  next boot instead of being skipped; a monitor that only works on machines
  with perfect uptime is monitoring the wrong thing.
- **Installing the prune timer arms nothing.** It runs with `--apply`, but the
  script still refuses while `[prune] enabled = false`, which is the default.
  Read a few days of `journalctl --user -u torrent-agent-prune` to check the
  thresholds are sane *before* they start deleting.
