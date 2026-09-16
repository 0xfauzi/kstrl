#!/usr/bin/env bash
#
# Owner experiment for docs/continuous-intake.md sections 5 and 7 (#203
# items 1 and 2): does a caffeinate assertion hold the machine up
# against a DARK WAKE's return to sleep?
#
# A dark wake ends on a SleepService timer, not on an idle timer, and
# `caffeinate -i` asserts PreventUserIdleSystemSleep. Interval mode can
# fire `ks serve --once` inside a 2-second dark wake, which is fine for
# an empty cycle at 0.08-0.12s and is not fine for a factory run at
# 10-20 minutes.
#
# macOS only, needs a real suspend, and costs no LLM spend: the child is
# /bin/sleep.
#
# Usage:
#   scripts/dark_wake_caffeinate_experiment.sh [seconds] [flag]
#
# Defaults: 1800 seconds under -i. Run it twice, once with -i and once
# with -s, and write both answers into docs/continuous-intake.md section
# 7. Run it on BATTERY with the lid shut: on AC with the lid open there
# is no dark wake to measure. 1800s because the SleepService dark wakes
# on this machine on 2026-09-15 came 17, 28, 18 and 17 minutes apart
# (`pmset -g log | grep rtc/SleepService`), so a shorter window can end
# before one lands and report VOID.
set -euo pipefail
cd "$(dirname "$0")/.."

HOLD="${1:-1800}"
FLAG="${2:--i}"

if ! command -v pmset >/dev/null 2>&1; then
  echo "pmset not found; this experiment is macOS-only."
  exit 2
fi

echo "plan: caffeinate ${FLAG} /bin/sleep ${HOLD}"
echo "Unplug, start, then close the lid within ~30s and leave it shut for"
echo "longer than ${HOLD}s. Open it at the end and let this finish."
read -r -p "press return to start " _ || true

START_EPOCH="$(date +%s)"
START_ISO="$(date -r "${START_EPOCH}" '+%Y-%m-%d %H:%M:%S')"
caffeinate "${FLAG}" /bin/sleep "${HOLD}" &
CHILD="$!"
# Kill only the child this script started, and only if it outlives the
# script (a ^C before the wait below).
trap 'kill "${CHILD}" 2>/dev/null || true' EXIT
sleep 2

echo "start:     ${START_ISO}"
echo "child pid: ${CHILD}"
ROW="$(pmset -g assertions | grep -B1 "(pid ${CHILD})" || true)"
if [ -z "${ROW}" ]; then
  echo "VOID: no power assertion names pid ${CHILD}, so nothing was held"
  echo "and nothing is being measured. Check \`caffeinate ${FLAG}\` by hand."
  exit 1
fi
printf '%s\n' "${ROW}"
echo
echo "every sleep assertion held right now, this run's helper included:"
pmset -g assertions | grep -E "PreventUserIdleSystemSleep|PreventSystemSleep" |
  grep -v "^ *Prevent" || true

wait "${CHILD}" || true
trap - EXIT
END_EPOCH="$(date +%s)"
END_ISO="$(date -r "${END_EPOCH}" '+%Y-%m-%d %H:%M:%S')"
WALL=$(( END_EPOCH - START_EPOCH ))

# The window is start-to-child-exit, and the assertion lives exactly as
# long as the child, so every line inside the window happened while the
# assertion was held. That is the whole point of reading it this way.
WINDOW="$(pmset -g log | awk -v s="${START_ISO}" -v e="${END_ISO}" '($1" "$2) >= s && ($1" "$2) <= e')"
DARK="$(printf '%s\n' "${WINDOW}" | grep -E ' DarkWake +' | grep 'SleepService' || true)"
BACK="$(printf '%s\n' "${WINDOW}" | grep 'Sleep Service Back to Sleep' || true)"
SLEEPS="$(printf '%s\n' "${WINDOW}" | grep -E ' Sleep +' || true)"
DARK_N="$(printf '%s\n' "${DARK}" | grep -c . || true)"
BACK_N="$(printf '%s\n' "${BACK}" | grep -c . || true)"
SLEEP_N="$(printf '%s\n' "${SLEEPS}" | grep -c . || true)"

echo
echo "end:       ${END_ISO}"
echo "wall:      ${WALL}s against a ${HOLD}s child"
echo "sleeps:    ${SLEEP_N}"
echo "dark wakes on a SleepService timer: ${DARK_N}"
echo "returns to sleep while the assertion was held: ${BACK_N}"
[ -n "${SLEEPS}" ] && printf '%s\n' "${SLEEPS}"
[ -n "${DARK}" ] && printf '%s\n' "${DARK}"
[ -n "${BACK}" ] && printf '%s\n' "${BACK}"

echo
if [ "${BACK_N}" -gt 0 ]; then
  echo "branch B: ${FLAG} did NOT hold. The machine entered 'Sleep Service Back"
  echo "to Sleep' ${BACK_N} time(s) while the assertion was held, so a factory"
  echo "run started inside a dark wake would have been suspended."
  echo "Next: run this again with -s. If -s gives branch A, switch"
  echo "caffeinate_prefix in kstrl/serve.py to -s and accept that the machine"
  echo "stays awake for the length of a run. If -s also gives branch B,"
  echo "interval mode is not safe unattended on a laptop and section 5 must"
  echo "say so."
elif [ "${DARK_N}" -gt 0 ]; then
  echo "branch A: ${FLAG} HELD. ${DARK_N} dark wake(s) on a SleepService timer"
  echo "and no return to sleep while the assertion was held, so a run started"
  echo "inside a dark wake finishes. Document it and change nothing."
elif [ "${SLEEP_N}" -gt 0 ]; then
  echo "VOID for this question: the machine slept ${SLEEP_N} time(s) but no dark"
  echo "wake on a SleepService timer landed inside the window, so the"
  echo "transition being asked about never came up. Run it longer, on battery."
else
  echo "VOID: no suspend inside the window. Nothing was measured."
fi
echo
echo "Write the branch and the lines above into docs/continuous-intake.md"
echo "section 7, and delete the NOT-verified bullet for #203."
