# systemd user units

Two long-running pieces that previously had nothing keeping them alive:

| Unit | What it does | Why it must not die quietly |
|---|---|---|
| `torrent-agent-bot.service` | The Telegram control bot (`python -m server.bot`) | A dead bot is invisible — messages simply go unanswered |
| `torrent-agent-pfsync.service` | `scripts/sync_pf_port.py --watch` | Proton's NAT-PMP lease rotates; when it moves and nothing re-syncs, Deluge silently drops to **zero inbound peers** with nothing in any log |
| `torrent-agent-sub.service` | Fetches new episodes of followed series (`/sub`) | Nothing else watches a running show; without it, following a series means remembering to ask each week |
| `torrent-agent-notifier.service` | Watches Deluge, messages Telegram on completion | Nothing else polls: the `fetch-to-jellyfin` skill only watches while a session drives it, and Deluge's Execute plugin is disabled. Downloads finished and sat in `complete/` with nobody told |

**User** units, not system ones, so no root is needed and they run as the
account that owns `~/.config/deluge` and the virtualenv.

## Install

```bash
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now torrent-agent-bot torrent-agent-pfsync \
  torrent-agent-notifier torrent-agent-sub
```

All three use `%h/workspace/repos/torrent-agent` — edit `WorkingDirectory` and
`ExecStart` if the checkout lives elsewhere.

To survive logout and start at boot without logging in (needs root once):

```bash
sudo loginctl enable-linger "$USER"
```

## Watch them

```bash
systemctl --user status torrent-agent-bot
journalctl --user -u torrent-agent-pfsync -f
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
