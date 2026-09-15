#!/usr/bin/env bash
#
# Owner experiment for docs/continuous-intake.md section 7: does an
# in-flight poll sleep keep counting while the machine is suspended?
#
# `ks serve` paces its loop with a plain `sleep(poll_interval_seconds)`
# in `kstrl/serve.py::serve`, where `sleep` is `time.sleep`. Nothing in
# kstrl recomputes how much of the interval has elapsed, so the answer
# belongs to the OS and only a real suspend can settle it.
#
# Costs no LLM spend: the scratch queue is paused and empty, so every
# cycle is a no-op gate check.
#
# Usage:
#   scripts/sleep_poll_experiment.sh [cycles] [poll_seconds]
#   KS="uv run ks" scripts/sleep_poll_experiment.sh 8 60
#
# The defaults reproduce the 2026-08-03 run: 8 cycles at a 60s poll.
set -euo pipefail

CYCLES="${1:-8}"
POLL="${2:-60}"

# Split KS on whitespace so `KS="uv run ks"` works from a checkout that
# has not installed the console script.
IFS=' ' read -r -a KS_CMD <<<"${KS:-ks}"

if ! command -v "${KS_CMD[0]}" >/dev/null 2>&1; then
  echo "cannot find '${KS_CMD[0]}' on PATH; try KS=\"uv run ks\" $0" >&2
  exit 2
fi

NEEDED=$(( (CYCLES - 1) * POLL ))

ROOT="$(mktemp -d)"
cat >"${ROOT}/kstrl.toml" <<TOML
[serve]
poll_interval_seconds = ${POLL}
caffeinate = false
max_open_prs = 0
TOML

"${KS_CMD[@]}" queue pause --root "${ROOT}" --reason "sleep/poll experiment" --no-color

cat <<TXT

scratch root: ${ROOT}
plan:         ${CYCLES} cycles at a ${POLL}s poll.
              ${CYCLES} cycles cost $(( CYCLES - 1 )) polls, so the run needs
              ${NEEDED}s of whatever time the poll sleep counts.

Close the lid ONCE, about 30s after the run starts, and leave it shut for
longer than ${NEEDED}s. Open it again and wait for the run to exit. One
suspend, not three: the arithmetic below has room for a single unknown.

TXT

read -r -p "press return to start the run " _ || true

START_EPOCH="$(date +%s)"
START_ISO="$(date '+%Y-%m-%d %H:%M:%S')"
echo "start: ${START_ISO}"

set +e
"${KS_CMD[@]}" serve --root "${ROOT}" --max-cycles "${CYCLES}" --no-color 2>&1 |
  tee "${ROOT}/serve.log"
RC="${PIPESTATUS[0]}"
set -e

END_EPOCH="$(date +%s)"
END_ISO="$(date '+%Y-%m-%d %H:%M:%S')"
WALL=$(( END_EPOCH - START_EPOCH ))

echo
echo "end:       ${END_ISO}"
echo "exit code: ${RC}"
echo "wall:      ${WALL}s"
grep -E '^[[:space:]]*cycles:' "${ROOT}/serve.log" ||
  echo "cycles: LINE NOT PRINTED (the run did not finish)"
echo "log:       ${ROOT}/serve.log"

cat <<TXT

Now read the sleep accounting for this window:

    pmset -g log | awk '\$0 >= "${START_ISO}" && \$0 <= "${END_ISO}"' | grep -E 'Sleep|Wake'

Add up the suspended intervals, then:

    awake = ${WALL} - suspended

How to read it:

  * awake >= ${NEEDED}  the poll sleep counts AWAKE time only; a suspend
                        pauses it and the interval finishes on wake.
  * awake <  ${NEEDED}  suspend time was credited against the poll sleep,
                        so the interval is closer to wall clock than to
                        awake time. Record how much.

Both branches need the cycles line above to read ${CYCLES}. An exit code
of 0 with a smaller number is impossible, so a smaller number means the
run was cut short and the measurement is void.

Write the number and the branch into docs/continuous-intake.md section 7
and delete the open question there. Do not write either branch up without
the arithmetic.

The scratch root is left in place on purpose; delete it when you are
done:

    rm -rf "${ROOT}"
TXT
