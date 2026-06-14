#!/usr/bin/env bash
# Restart the torrent-agent stack: full teardown, then startup.
# Flags pass through to stop.sh (e.g. --vpn to also cycle the VPN).
#
#   ./scripts/restart.sh          # restart docker + deluge, keep VPN up
#   ./scripts/restart.sh --vpn    # also disconnect/reconnect PIA
set -uo pipefail
HERE="$(dirname "${BASH_SOURCE[0]}")"

"$HERE/stop.sh" "$@"
echo
"$HERE/start.sh"
