#!/usr/bin/env bash
#
# Owner experiment for docs/continuous-intake.md section 7: does an
# in-flight poll sleep keep counting while the machine is suspended?
# Run from anywhere in the checkout; it needs `uv` and a real suspend.
#
# Usage:
#   scripts/sleep_poll_experiment.sh [cycles] [poll_seconds]
#
# Defaults: 2 cycles at a 300s poll. Only the ONE poll in flight when the
# lid closes is affected by a suspend, so the margin between the two
# answers is that poll's remaining seconds; more cycles cost more time
# and prove nothing. Close the lid about 10s after the run starts and
# leave it shut for longer than the poll.
set -euo pipefail
cd "$(dirname "$0")/.."

CYCLES="${1:-2}"
POLL="${2:-300}"
NEEDED=$(( (CYCLES - 1) * POLL ))

ROOT="$(mktemp -d)"
printf '[serve]\npoll_interval_seconds = %s\nmax_open_prs = 0\n' "${POLL}" >"${ROOT}/kstrl.toml"

echo "scratch root: ${ROOT}"
echo "plan: ${CYCLES} cycles at a ${POLL}s poll, so ${NEEDED}s of whatever the poll counts."
echo "Close the lid ONCE about 10s in, keep it shut for more than ${POLL}s, then open it and wait."
read -r -p "press return to start " _ || true

START_EPOCH="$(date +%s)"
START_ISO="$(date -r "${START_EPOCH}" '+%Y-%m-%d %H:%M:%S')"
echo "start: ${START_ISO}"
set +e
uv run ks serve --root "${ROOT}" --max-cycles "${CYCLES}" --no-color 2>&1 | tee "${ROOT}/serve.log"
RC="${PIPESTATUS[0]}"
set -e
END_EPOCH="$(date +%s)"
END_ISO="$(date -r "${END_EPOCH}" '+%Y-%m-%d %H:%M:%S')"
WALL=$(( END_EPOCH - START_EPOCH ))

# pmset's Sleep lines carry the suspend's length ("... 975 secs"); sum
# them inside the window. Zero suspends means the run measured nothing.
SLEEPS="$(pmset -g log | awk -v s="${START_ISO}" -v e="${END_ISO}" '($1" "$2) >= s && ($1" "$2) <= e' | grep -E ' Sleep +' || true)"
COUNT="$(printf '%s\n' "${SLEEPS}" | grep -c . || true)"
SUSPENDED="$(printf '%s\n' "${SLEEPS}" | grep -oE '[0-9]+ secs' | awk '{s+=$1} END {print s+0}')"
AWAKE=$(( WALL - SUSPENDED ))

echo
echo "end:        ${END_ISO}"
echo "exit code:  ${RC}"
grep -E '^[[:space:]]*cycles:' "${ROOT}/serve.log" || echo "cycles:     LINE NOT PRINTED (the run did not finish; the measurement is void)"
echo "wall:       ${WALL}s"
echo "suspends:   ${COUNT} (${SUSPENDED}s)"
[ -n "${SLEEPS}" ] && printf '%s\n' "${SLEEPS}"
echo "awake:      ${AWAKE}s against ${NEEDED}s needed"
if [ "${COUNT}" -eq 0 ]; then
  echo "VOID: no suspend inside the window."
elif [ "${COUNT}" -gt 1 ]; then
  echo "WARNING: ${COUNT} suspends; the arithmetic tolerates one unknown, so treat this as a hint."
elif [ "${AWAKE}" -ge "${NEEDED}" ]; then
  echo "branch 1: the poll counted AWAKE time only (suspend paused it)."
else
  echo "branch 2: $(( NEEDED - AWAKE ))s of suspend was credited against the poll."
fi
echo "Write the numbers and the branch into docs/continuous-intake.md section 7. Log: ${ROOT}/serve.log"
