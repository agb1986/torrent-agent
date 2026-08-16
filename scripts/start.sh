#!/usr/bin/env bash
# Start the full torrent-agent stack, in dependency order:
#   Host VPN  ->  bind Deluge to the tunnel  ->  Deluge daemon  ->  Prowlarr
# Each step is best-effort; the closing health check shows what actually came up.
#
# The order is load-bearing: Deluge binds its peer sockets to the tunnel device,
# so the VPN must be up first or deluged has nothing to bind to.
#
#   ./scripts/start.sh
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

vpn_up          || true
deluge_bind_vpn || true   # after the VPN is up, before deluged binds its sockets
deluge_up       || true
stack_up        || true
health
