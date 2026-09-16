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
# Defaults: 1800 seconds under -i. Run it twice: the -i leg on BATTERY
# with the lid shut, the -s leg on AC with the lid shut (man caffeinate:
# "-s ... is valid only when system is running on AC power", so on
# battery -s is not a remedy at all - this script refuses that leg
# rather than reporting a result that measures nothing). Write both
# answers into docs/continuous-intake.md section 7. 1800s because the
# SleepService dark wakes on this machine on 2026-09-15 came 17, 28, 18
# and 17 minutes apart (`pmset -g log | grep rtc/SleepService`), so a
# shorter window can end before one lands and report VOID.
set -euo pipefail

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
SECONDS=0
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

if [ "${FLAG}" = "-s" ] && pmset -g batt | head -1 | grep -q "Battery Power"; then
  echo "VOID: caffeinate -s is valid only on AC power (man caffeinate); run"
  echo "the -s leg on AC with the lid shut."
  exit 1
fi

wait "${CHILD}" || true
trap - EXIT
END_EPOCH="$(date +%s)"
END_ISO="$(date -r "${END_EPOCH}" '+%Y-%m-%d %H:%M:%S')"
WALL="${SECONDS}"

# The window is start-to-child-exit, and the assertion lives as long as
# the child, so every line inside the window happened while the
# assertion was held.
WINDOW="$(pmset -g log | awk -v s="${START_ISO}" -v e="${END_ISO}" '($1" "$2) >= s && ($1" "$2) <= e')"
count() { printf '%s\n' "$1" | grep -c . || true; }
DARK="$(printf '%s\n' "${WINDOW}" | awk '$4=="DarkWake"' || true)"
SLEEPS="$(printf '%s\n' "${WINDOW}" | awk '$4=="Sleep"' || true)"
BACK="$(printf '%s\n' "${SLEEPS}" | grep -E "Sleep Service Back to Sleep|Maintenance Sleep" || true)"
DARK_N="$(count "${DARK}")"
SLEEP_N="$(count "${SLEEPS}")"
BACK_N="$(count "${BACK}")"

echo
echo "end:       ${END_ISO}"
echo "wall:      ${WALL}s against a ${HOLD}s child"
echo "sleeps:    ${SLEEP_N}"
echo "dark wakes: ${DARK_N}"
[ -n "${SLEEPS}" ] && printf '%s\n' "${SLEEPS}"
[ -n "${DARK}" ] && printf '%s\n' "${DARK}"

echo
if [ "${BACK_N}" -gt 0 ]; then
  echo "branch B: ${FLAG} did NOT hold. The machine entered a SleepService or"
  echo "Maintenance return to sleep ${BACK_N} time(s) while the assertion was"
  echo "held, so a factory run started inside a dark wake would have been"
  echo "suspended."
elif [ "${DARK_N}" -gt 0 ]; then
  echo "branch A: ${FLAG} HELD. ${DARK_N} dark wake(s) and no return to sleep"
  echo "while the assertion was held, so a run started inside a dark wake"
  echo "finishes."
elif [ "${SLEEP_N}" -gt 0 ]; then
  echo "VOID for this question: the machine slept ${SLEEP_N} time(s) but no"
  echo "dark wake landed inside the window, so the transition being asked"
  echo "about never came up. Run it longer, on battery."
else
  echo "VOID: no suspend inside the window. Nothing was measured."
fi
echo
echo "docs/continuous-intake.md section 7 says what each pair of results decides."
