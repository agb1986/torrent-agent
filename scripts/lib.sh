# Shared helpers for the torrent-agent stack control scripts.
# Sourced by start.sh / stop.sh / restart.sh — not meant to be run directly.
#
# The stack has three independently-managed parts:
#   1. PIA VPN          (piactl)            — downloads are refused unless it's up
#   2. Deluge daemon    (deluged, :58846)   — runs as you, ~/.config/deluge (Path B)
#   3. Prowlarr + FlareSolverr (docker compose) — search backend

set -uo pipefail

# --- paths / endpoints -----------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROWLARR_URL="${PROWLARR_URL:-http://localhost:9696}"
FLARESOLVERR_URL="${FLARESOLVERR_URL:-http://localhost:8191}"
DELUGE_PORT="${DELUGE_PORT:-58846}"
VENV_PY="$REPO_DIR/.venv/bin/python"

# --- logging ---------------------------------------------------------------
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''; C_RST=''
fi
log()  { printf '%s==>%s %s\n' "$C_DIM" "$C_RST" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_WARN" "$C_RST" "$*"; }
err()  { printf '%sfail%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; }

# --- docker (compose needs sudo unless you're in the docker group) ----------
docker_cmd() {
  if docker info >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi
}

# --- generic ---------------------------------------------------------------
port_in_use() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# --- 1. PIA VPN ------------------------------------------------------------
vpn_state()  { piactl get connectionstate 2>/dev/null || echo "Unknown"; }
vpn_region() { piactl get region 2>/dev/null || echo "?"; }

vpn_up() {
  command -v piactl >/dev/null 2>&1 || { warn "piactl not found — skipping VPN"; return 0; }
  log "Connecting PIA…"
  piactl connect >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do [ "$(vpn_state)" = "Connected" ] && break; sleep 1; done
  if [ "$(vpn_state)" = "Connected" ]; then
    local region; region="$(vpn_region)"
    ok "PIA connected — region=$region ip=$(piactl get vpnip 2>/dev/null)"
    case "$region" in
      uk-*) warn "UK exits sit behind court-ordered torrent-site blocks (HTTP 451)." \
                 "Switch with:  piactl set region netherlands" ;;
    esac
  else
    err "PIA never reached Connected (state=$(vpn_state)). Is the PIA app/daemon running?"; return 1
  fi
}

vpn_down() {
  command -v piactl >/dev/null 2>&1 || return 0
  log "Disconnecting PIA…"
  piactl disconnect >/dev/null 2>&1 || true
  ok "PIA disconnected"
}

# --- 2. Deluge daemon ------------------------------------------------------
# Bind Deluge's peer traffic to the live tunnel device, so a VPN drop kills
# transfers instead of silently rerouting them over the LAN (PIA keeps the LAN
# default route underneath its 0.0.0.0/1 + 128.0.0.0/1 split).
#
# MUST run while the VPN is up and BEFORE deluge_up: if the device is missing
# when deluged starts, libtorrent has nothing to bind to and won't listen.
deluge_bind_vpn() {
  [ -x "$VENV_PY" ] || { warn "no .venv — skipping VPN binding"; return 0; }
  local out
  if out="$("$VENV_PY" "$REPO_DIR/scripts/bind_vpn.py" 2>&1)"; then
    ok "Deluge bound to $(printf '%s' "$out" | grep -o 'to [a-z0-9]*' | head -1 | cut -d' ' -f2)"
  else
    warn "Could not bind Deluge to the VPN: $out"
  fi
}

deluge_up() {
  command -v deluged >/dev/null 2>&1 || { warn "deluged not installed — skipping"; return 0; }
  if port_in_use "$DELUGE_PORT"; then ok "Deluge already listening on $DELUGE_PORT"; return 0; fi
  log "Starting deluged…"
  deluged   # self-daemonizes; binds 127.0.0.1:58846 using ~/.config/deluge
  for _ in $(seq 1 15); do port_in_use "$DELUGE_PORT" && break; sleep 1; done
  if port_in_use "$DELUGE_PORT"; then
    ok "Deluge daemon up on 127.0.0.1:$DELUGE_PORT"
  else
    err "deluged failed to bind $DELUGE_PORT (GTK Classic Mode or system service holding the session?)"; return 1
  fi
}

deluge_down() {
  if ! port_in_use "$DELUGE_PORT"; then ok "Deluge daemon already stopped"; return 0; fi
  log "Stopping deluged…"
  pkill -u "$(id -u)" -x deluged 2>/dev/null || true
  for _ in $(seq 1 10); do port_in_use "$DELUGE_PORT" || break; sleep 1; done
  if port_in_use "$DELUGE_PORT"; then
    warn "Port $DELUGE_PORT still bound — likely the system deluged.service (sudo systemctl stop deluged)"
  else
    ok "Deluge daemon stopped"
  fi
}

# --- 3. Prowlarr + FlareSolverr (docker compose) ---------------------------
stack_up() {
  log "Bringing up Prowlarr + FlareSolverr…"
  if ( cd "$REPO_DIR" && docker_cmd compose up -d ); then ok "Compose stack up"; else err "compose up failed"; return 1; fi
}

stack_down() {
  log "Stopping Prowlarr + FlareSolverr…"
  if ( cd "$REPO_DIR" && docker_cmd compose down ); then ok "Compose stack down"; else err "compose down failed"; return 1; fi
}

# --- health check ----------------------------------------------------------
prowlarr_key() {
  if [ -n "${PROWLARR_API_KEY:-}" ]; then printf '%s' "$PROWLARR_API_KEY"; return; fi
  local cfg="$HOME/.config/prowlarr/config.xml"
  [ -f "$cfg" ] && grep -o '<ApiKey>[^<]*</ApiKey>' "$cfg" | sed 's/<[^>]*>//g'
}

health() {
  echo; log "Health:"
  if command -v piactl >/dev/null 2>&1; then
    local st; st="$(vpn_state)"
    if [ "$st" = "Connected" ]; then ok "PIA           Connected (region $(vpn_region))"
    else warn "PIA           $st"; fi
  fi
  if port_in_use "$DELUGE_PORT"; then ok "Deluge        listening on 127.0.0.1:$DELUGE_PORT"
  else warn "Deluge        not listening on $DELUGE_PORT"; fi
  if [ -x "$VENV_PY" ]; then
    local bind_out
    bind_out="$("$VENV_PY" "$REPO_DIR/scripts/bind_vpn.py" --check 2>&1)"
    if printf '%s' "$bind_out" | grep -q '^OK:'; then
      ok "VPN binding   $(printf '%s' "$bind_out" | grep '^OK:' | sed 's/^OK: Deluge is //')"
    else
      warn "VPN binding   $(printf '%s' "$bind_out" | grep -E '^(MISMATCH|No VPN|Could not)' | head -1)"
    fi
  fi
  local key code; key="$(prowlarr_key)"
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "X-Api-Key: $key" "$PROWLARR_URL/api/v1/system/status" 2>/dev/null)"
  if [ "$code" = "200" ]; then ok "Prowlarr      HTTP 200 at $PROWLARR_URL"
  else warn "Prowlarr      HTTP ${code:-no-response} at $PROWLARR_URL"; fi
  if curl -s --max-time 5 "$FLARESOLVERR_URL/" 2>/dev/null | grep -q "FlareSolverr is ready"; then
    ok "FlareSolverr  ready at $FLARESOLVERR_URL"
  else
    warn "FlareSolverr  not ready at $FLARESOLVERR_URL"
  fi
}
