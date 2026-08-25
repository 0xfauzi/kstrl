#!/usr/bin/env bash
# Advance the kstrl system atlas to the current tree.
#
# The atlas tracks the default branch: that is what makes the committed
# atlas.json diff between two commits a readable record of what changed about
# the system. This script is the one place that advance happens, so a git hook,
# CI, and a person typing it by hand all do exactly the same thing.
#
# Exit codes: 0 = atlas is current (refreshed or already fresh), 1 = it could
# not be refreshed. Callers that must not fail a merge should ignore the code.
set -uo pipefail

repo=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "atlas: not a git repository" >&2
  exit 1
}
cd "$repo" || exit 1

if [ ! -f scripts/atlas/extract_atlas.py ]; then
  echo "atlas: scripts/atlas is missing" >&2
  exit 1
fi
command -v uv >/dev/null 2>&1 || {
  echo "atlas: uv is not on PATH" >&2
  exit 1
}

quiet=0
[ "${1:-}" = "--quiet" ] && quiet=1
say() { [ "$quiet" -eq 1 ] || printf '%s\n' "$*"; }

if uv run python scripts/atlas/extract_atlas.py --check >/dev/null 2>&1; then
  say "atlas: already current"
  exit 0
fi

say "atlas: refreshing against the working tree"
if ! uv run python scripts/atlas/extract_atlas.py >/dev/null 2>&1; then
  echo "atlas: extract failed" >&2
  exit 1
fi
if ! uv run python scripts/atlas/render_html.py >/dev/null 2>&1; then
  echo "atlas: render failed" >&2
  exit 1
fi

say "atlas: updated docs/atlas/atlas.json and docs/atlas/index.html"
say "atlas: commit docs/atlas/ to record this state of the system"
exit 0
