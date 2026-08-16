#!/usr/bin/env bash
# Pull and restart the unattended stack, on the machine that runs it.
#
#   ./deploy/update.sh
#
# Restarts every service, not the ones that look like they changed. Python
# holds the old module in memory after a pull, and a half-restart has bitten
# twice: a fix that is deployed but not running looks exactly like a fix that
# did not work. Restarting all four is cheap and none holds state in memory
# that matters — the notifier and sub persist to disk, the bot's queue is
# empty between runs.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

SERVICES=(torrent-agent-bot torrent-agent-notifier torrent-agent-pfsync torrent-agent-sub)

echo "==> pulling"
git pull --ff-only || { echo "pull failed — not restarting anything" >&2; exit 1; }
git log --oneline -1

echo "==> tests"
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m pytest tests/ -q || {
    echo "tests failed — not restarting anything" >&2; exit 1; }
fi

echo "==> restarting"
for s in "${SERVICES[@]}"; do
  if systemctl --user list-unit-files "$s.service" >/dev/null 2>&1 &&
     systemctl --user is-enabled "$s" >/dev/null 2>&1; then
    systemctl --user restart "$s" && echo "  restarted $s"
  else
    echo "  skipped $s (not installed here)"
  fi
done

sleep 5
echo "==> status"
for s in "${SERVICES[@]}"; do
  printf '  %-28s %s\n' "$s" "$(systemctl --user is-active "$s" 2>/dev/null || echo absent)"
done
