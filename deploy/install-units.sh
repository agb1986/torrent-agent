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
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
ALL=(bot notifier pfsync sub)

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
  installed+=("torrent-agent-$name")
  echo "  installed torrent-agent-$name  ->  $REPO"
done

[ ${#installed[@]} -eq 0 ] && { echo "nothing installed" >&2; exit 1; }

systemctl --user daemon-reload
echo
echo "Enable and start them with:"
echo "  systemctl --user enable --now ${installed[*]}"
echo
echo "To survive logout and start at boot (needs root once):"
echo "  sudo loginctl enable-linger \"\$USER\""
