#!/usr/bin/env bash
# Pull and restart the unattended stack, on the machine that runs it.
#
#   ./deploy/update.sh
#   ./deploy/update.sh --no-pull   # everything but the pull
#
# --no-pull exists for deploy/hooks/post-merge, which runs this after a pull
# that git has already done. Same code path either way, so the hook cannot
# drift from the manual route.
#
# Restarts every service, not the ones that look like they changed. Python
# holds the old module in memory after a pull, and a half-restart has bitten
# twice: a fix that is deployed but not running looks exactly like a fix that
# did not work. Restarting all four is cheap and none holds state in memory
# that matters — the notifier and sub persist to disk, the bot's queue is
# empty between runs.
#
# Also reinstalls any unit file that is already installed here. The units are
# templates rendered at install time, so a change to one in git is invisible
# until it is rendered again — the same class of failure as the stale module,
# one level down.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
REPO="$PWD"
UNIT_DIR="$HOME/.config/systemd/user"

SERVICES=(torrent-agent-bot torrent-agent-notifier torrent-agent-pfsync torrent-agent-sub)
TIMERS=(torrent-agent-doctor torrent-agent-prune torrent-agent-backup)

PULL=1
[ "${1:-}" = "--no-pull" ] && PULL=0

if [ "$PULL" = 1 ]; then
  echo "==> pulling"
  git pull --ff-only || { echo "pull failed — not restarting anything" >&2; exit 1; }
fi
git log --oneline -1

echo "==> tests"
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m pytest tests/ -q || {
    echo "tests failed — not restarting anything" >&2; exit 1; }
fi

echo "==> unit files"
# Only what is actually *enabled*, not merely present. A unit file left behind
# by a torn-down rehearsal is still sitting in $UNIT_DIR on the laptop, and
# re-rendering those says "installed" for a machine that runs none of it.
present=()
for name in "${SERVICES[@]}"; do
  systemctl --user is-enabled "$name" >/dev/null 2>&1 && present+=("${name#torrent-agent-}")
done
for name in "${TIMERS[@]}"; do
  systemctl --user is-enabled "$name.timer" >/dev/null 2>&1 && present+=("${name#torrent-agent-}")
done
if [ ${#present[@]} -gt 0 ]; then
  ./deploy/install-units.sh "${present[@]}" | sed 's/^/  /'
else
  echo "  none installed here — nothing to render"
fi

echo "==> restarting"
for s in "${SERVICES[@]}"; do
  if systemctl --user is-enabled "$s" >/dev/null 2>&1; then
    systemctl --user restart "$s" && echo "  restarted $s"
  else
    echo "  skipped $s (not installed here)"
  fi
done

# Timers only need daemon-reload, which install-units.sh already did. Restart
# them anyway so a changed OnCalendar takes effect now rather than after the
# next boot, and so `is-active` below reflects the running schedule.
for t in "${TIMERS[@]}"; do
  if systemctl --user is-enabled "$t.timer" >/dev/null 2>&1; then
    systemctl --user restart "$t.timer" && echo "  restarted $t.timer"
  fi
done

sleep 5
echo "==> status"
for s in "${SERVICES[@]}"; do
  # `|| echo absent` would fire on top of the answer, not instead of it:
  # is-active prints "inactive" *and* exits 3, so both lines came out.
  state="$(systemctl --user is-active "$s" 2>/dev/null)"
  printf '  %-28s %s\n' "$s" "${state:-absent}"
done
for t in "${TIMERS[@]}"; do
  if systemctl --user is-enabled "$t.timer" >/dev/null 2>&1; then
    printf '  %-28s %s\n' "$t.timer" \
      "$(systemctl --user list-timers --no-legend "$t.timer" 2>/dev/null | awk '{print "next " $1, $2, $3}')"
  fi
done
