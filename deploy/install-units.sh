#!/usr/bin/env bash
# Install the systemd user units, pointed at this checkout.
#
#   ./deploy/install-units.sh              # all of them
#   ./deploy/install-units.sh bot notifier # just these
#
# The units are templates: they carry __REPO__ where the checkout path goes,
# because systemd does no substitution of its own and %h only gets you as far
# as the home directory. Hardcoding a path instead meant the four units
# disagreed about where the repo lived, and the copies actually running were
# hand-edited — which drifts the moment anything changes.
#
# Two kinds live here. The long-running services (bot, notifier, pfsync, sub)
# are enabled directly. The periodic ones (doctor, prune) are a
# oneshot .service plus a .timer, and it is the *timer* that gets enabled —
# enabling the service would try to run it once at boot and never again.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
ALL=(bot notifier pfsync sub doctor prune)

wanted=("$@")
[ ${#wanted[@]} -eq 0 ] && wanted=("${ALL[@]}")

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "no virtualenv at $REPO/.venv — create it before installing units" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
installed=()
for name in "${wanted[@]}"; do
  src="$REPO/deploy/systemd/torrent-agent-$name.service"
  if [ ! -f "$src" ]; then
    echo "  no such unit: $name" >&2
    continue
  fi
  sed "s|__REPO__|$REPO|g" "$src" > "$UNIT_DIR/torrent-agent-$name.service"

  timer="$REPO/deploy/systemd/torrent-agent-$name.timer"
  if [ -f "$timer" ]; then
    sed "s|__REPO__|$REPO|g" "$timer" > "$UNIT_DIR/torrent-agent-$name.timer"
    # The timer, not the service: see the header.
    installed+=("torrent-agent-$name.timer")
    echo "  installed torrent-agent-$name (+ .timer)  ->  $REPO"
  else
    installed+=("torrent-agent-$name")
    echo "  installed torrent-agent-$name  ->  $REPO"
  fi
done

[ ${#installed[@]} -eq 0 ] && { echo "nothing installed" >&2; exit 1; }

systemctl --user daemon-reload
echo
echo "Enable and start them with:"
echo "  systemctl --user enable --now ${installed[*]}"
echo
echo "To survive logout and start at boot (needs root once):"
echo "  sudo loginctl enable-linger \"\$USER\""
echo
echo "Nothing is armed by installing prune: it refuses to remove anything"
echo "until [prune] enabled = true in config.toml. Read a few days of"
echo "  systemctl --user status torrent-agent-prune"
echo "first, to see what it would have taken."
