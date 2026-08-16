# Shared helpers for the torrent-agent stack control scripts.
# Sourced by start.sh / stop.sh / restart.sh — not meant to be run directly.
#
# The stack has three independently-managed parts:
#   1. Host VPN         (route check)       — downloads are refused unless it's up
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

# --- 1. Host VPN -----------------------------------------------------------
# Route-based rather than tied to one vendor's CLI. ProtonVPN's GNOME app has
# no scriptable connect/disconnect the way piactl did, so these ask the kernel
# which device is carrying traffic instead of asking a client what it thinks.
# That is the stronger question anyway — it is what bind_vpn.py checks, and it
# stays true for PIA, Proton, or a hand-rolled wg-quick tunnel.

# Live tunnel device name (proton0 / tun0 / wgpia0), or empty if none.
vpn_device() {
  [ -x "$VENV_PY" ] || return 0
  "$VENV_PY" -c 'from torrent_agent.vpn import tunnel_device; print(tunnel_device() or "")' 2>/dev/null
}

vpn_state() { [ -n "$(vpn_device)" ] && echo "Connected" || echo "Disconnected"; }

# Best-effort only: piactl exposed a region, the Proton app does not. Falls
# back to the exit IP, which is the thing you actually want to eyeball.
vpn_region() {
  if command -v piactl >/dev/null 2>&1 && [ "$(piactl get connectionstate 2>/dev/null)" = "Connected" ]; then
    piactl get region 2>/dev/null && return
  fi
  curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo "?"
}

# Advisory. Connecting is the client's job — piactl could be driven headlessly,
# the Proton GNOME app cannot, so this verifies rather than pretends to control.
vpn_up() {
  if command -v piactl >/dev/null 2>&1 && [ "$(piactl get connectionstate 2>/dev/null)" = "Connected" ]; then
    ok "VPN up — piactl reports Connected, device $(vpn_device)"
    return 0
  fi
  local dev; dev="$(vpn_device)"
  if [ -n "$dev" ]; then
    ok "VPN up — traffic leaving via $dev (exit $(vpn_region))"
    return 0
  fi
  err "No VPN tunnel is carrying traffic. Connect ProtonVPN (tray icon or" \
      "'protonvpn-app'), then re-run. Downloads are refused without it."
  return 1
}

vpn_down() {
  if command -v piactl >/dev/null 2>&1; then
    log "Disconnecting PIA…"
    piactl disconnect >/dev/null 2>&1 || true
  fi
  [ -n "$(vpn_device)" ] && warn "A tunnel is still up — disconnect it from the VPN app." \
                         || ok "No tunnel carrying traffic"
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
  local dev; dev="$(vpn_device)"
  if [ -n "$dev" ]; then ok "VPN           carrying traffic on $dev"
  else warn "VPN           no tunnel — downloads will be refused"; fi
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
