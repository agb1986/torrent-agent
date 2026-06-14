#!/usr/bin/env bash
# Tear down the torrent-agent stack (reverse order):
#   Prowlarr + FlareSolverr  ->  Deluge daemon
#
# The VPN is LEFT CONNECTED by default — other things on the machine may rely
# on it. Pass --vpn to also disconnect PIA.
#
#   ./scripts/stop.sh           # stop docker + deluge, keep VPN
#   ./scripts/stop.sh --vpn     # also disconnect PIA
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WITH_VPN=0
for a in "$@"; do [ "$a" = "--vpn" ] && WITH_VPN=1; done

stack_down  || true
deluge_down || true
if [ "$WITH_VPN" = "1" ]; then vpn_down || true; fi

echo; ok "Teardown complete"
