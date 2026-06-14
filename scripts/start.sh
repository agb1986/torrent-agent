#!/usr/bin/env bash
# Start the full torrent-agent stack, in dependency order:
#   PIA VPN  ->  Deluge daemon  ->  Prowlarr + FlareSolverr
# Each step is best-effort; the closing health check shows what actually came up.
#
#   ./scripts/start.sh
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

vpn_up    || true
deluge_up || true
stack_up  || true
health
