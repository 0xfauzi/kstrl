#!/usr/bin/env bash
# Advance the kstrl system atlas to the current tree.
#
# The atlas tracks the default branch: that is what makes the committed
# atlas.json diff between two commits a readable record of what changed about
# the system. This script is the one place that advance happens, so a git hook,
# CI, and a person typing it by hand all do exactly the same thing.
#
# It also regenerates the two lesson figures under docs/atlas/ and the static
# figures under docs/atlas/figures/ from the same generator, so a figure can
# never lag the page it cites.
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

if uv run python scripts/atlas/extract_atlas.py --check >/dev/null 2>&1 \
   && uv run python scripts/atlas/render_html.py --check >/dev/null 2>&1 \
   && uv run python scripts/atlas/figures.py --check >/dev/null 2>&1; then
  say "atlas: already current"
  exit 0
fi

say "atlas: refreshing against the working tree"
if ! uv run python scripts/atlas/extract_atlas.py >/dev/null 2>&1; then
  echo "atlas: extract failed" >&2
  exit 1
fi
if ! uv run python scripts/atlas/check_layout.py; then
  echo "atlas: layout check failed; fix logical_model.py or relations.py" >&2
  exit 1
fi
if ! uv run python scripts/atlas/render_html.py >/dev/null 2>&1; then
  echo "atlas: render failed" >&2
  exit 1
fi

# The lesson figures committed beside the page: the plain system, and the
# reach of the R10 control-loop plan (docs/control-loop-design.md).
r10="ServeDaemon,GitHubIntake,FlowControl,Steering,PRD,RetryContext,EngineerLoop"
r10="$r10,OperatorContext,Reviewer,Calibration,Sense,Pipeline,Dampener,AutonomyLadder"
r10="$r10,SafeMode,HealthTrending"
uv run python scripts/atlas/lesson_svg.py --mode system --interactive \
  --out docs/atlas/system.html >/dev/null || {
  echo "atlas: system figure failed" >&2
  exit 1
}
uv run python scripts/atlas/lesson_svg.py --components "$r10" --interactive \
  --caption "The system, with the components the R10 control-loop plan reaches as the figure. Drawn by scripts/atlas/schematic.py from docs/atlas/atlas.json." \
  --out docs/atlas/r10-reach.html >/dev/null || {
  echo "atlas: r10 figure failed" >&2
  exit 1
}

# The static figures README.md and ARCHITECTURE.md embed as images.
uv run python scripts/atlas/figures.py >/dev/null || {
  echo "atlas: static figures failed" >&2
  exit 1
}

say "atlas: updated docs/atlas/atlas.json, index.html, system.html, r10-reach.html, figures/"
say "atlas: commit docs/atlas/ to record this state of the system"
exit 0
