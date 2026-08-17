#!/usr/bin/env bash
# Install the git hooks in deploy/hooks/ into this checkout.
#
#   ./deploy/install-hooks.sh
#
# Symlinks rather than copies, so a change to a hook in git takes effect on
# the next pull without anyone remembering to reinstall it — which is the
# same failure the post-merge hook exists to prevent.
#
# Hooks are per-checkout and never travel with a clone, so this has to be run
# once on each machine that runs the stack. It is harmless on a machine that
# does not: the hooks check for installed units and exit quietly.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# --absolute-git-dir, not --git-path: the latter answers relative to the cwd,
# which makes the symlink target depend on where this was run from.
GIT_DIR="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null)" ||
  { echo "$REPO is not a git checkout" >&2; exit 1; }
HOOK_DIR="$GIT_DIR/hooks"
mkdir -p "$HOOK_DIR"

for src in "$REPO"/deploy/hooks/*; do
  [ -f "$src" ] || continue
  name="$(basename "$src")"
  dest="$HOOK_DIR/$name"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "  $name already exists and is not a symlink — leaving it alone" >&2
    continue
  fi
  chmod +x "$src"
  ln -sfn "$src" "$dest"
  echo "  linked $name -> deploy/hooks/$name"
done
