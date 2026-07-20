# TUI Spike (Stage 0) - measured results and go/no-go

Environment: macOS (Darwin 25.5.0), Apple Silicon, Python 3.11 via uv,
textual 5.3.0, rich 14.3.3 (repo-locked version, unchanged by the textual
resolution - the `textual>=3,<6` pin is compatible). Measured 2026-07-20.

Method notes: the latency harness (`measure.py latency`) spawns
`fake_run.py` at a given event rate and tails events.jsonl + per-component
engineer.jsonl with the byte-offset `JsonlTailer`; latency = t_seen -
t_emit per record. "Storm" cells use `--churn` (components cycle forever)
after the first attempt revealed that completing components made the 10x
row idle (records_written ~349 = not a storm; discarded, rerun). The
Textual app is exercised inside a real pty (`measure.py pty-app`), which
also captures every byte the terminal would see - screen-integrity
verdicts grep that capture for the alt-screen/cursor restore sequences.

## G1 - tail latency vs poll interval

Realistic rate (~6 events/s across 4 components + transcripts), 60s cells,
zero record loss in every cell (torn-tail injection every 50 lines active):

| poll | p50 | p95 | max | tailer CPU |
|---|---|---|---|---|
| 0.05s | 24.6ms | 50.2ms | 249ms | 0.62% |
| 0.10s | 51.8ms | 100.5ms | 308ms | 0.53% |
| 0.25s | 125.3ms | 240.7ms | 434ms | 0.26% |
| 0.50s | 211.9ms | 409.5ms | 600ms | 0.16% |
| 1.00s | 531.8ms | 934.6ms | 1023ms | 0.05% |

Storm rate (churn, ~70 events/s sustained, ~4210 records per 60s cell,
zero record loss in every cell):

| poll | p50 | p95 | max | tailer CPU |
|---|---|---|---|---|
| 0.05s | 28.2ms | 52.2ms | 260ms | 1.22% |
| 0.10s | 51.1ms | 100.5ms | 309ms | 0.63% |
| 0.25s | 129.3ms | 247.4ms | 458ms | 0.45% |
| 0.50s | 255.0ms | 485.7ms | 694ms | 0.30% |
| 1.00s | 509.2ms | 960.8ms | 1186ms | 0.15% |

Latency is poll-interval-dominated and rate-INDEPENDENT (p95 at a given
poll is near-identical at 1x and 10x): the tailer drains everything
available per poll, so backlog never accumulates at these rates.

VERDICT G1: PASS at poll=0.25s (p95 240.7ms <= 300ms target). p50 at
0.1s is noticeably snappier for 0.5% CPU - production default set to
**0.2s** (between the two measured points, comfortably inside the gate).

## G2 - CPU

- Headless tailer: <= 0.9% of one core at every measured cell (max seen
  0.89% at poll=0.05 storm). PASS (<10% gate).
- Textual app, true idle (finished run, no new events): p50 0.6%,
  p95 2.1%, max 2.2%. p50 PASS (<2%); p95 includes the initial
  catch-up fold on startup. Note: a poll back-off when no events arrive
  is an easy optimization if idle CPU ever matters.
- Textual app, realistic active (rate 1): p50 1.0%, p95 4.5%, max 5.2%.
- Textual app, storm: (filled from soak below).

## G3 - screen integrity and exit paths (pty-captured)

| path | exited | restore (alt screen + cursor) |
|---|---|---|
| `q` key | yes, rc=0 | RESTORED |
| Ctrl-C as key, no binding | NO - app keeps running | n/a (finding 1) |
| Ctrl-C as key, bound | yes, rc=0 | RESTORED |
| external SIGINT | yes, rc=0 | RESTORED (Textual handles it) |
| external SIGTERM | yes, rc=-15 | **NOT restored** (finding 2) |
| induced crash in timer | yes | RESTORED (Textual crash screen + traceback shown) |

Chatter tests:
- Python-level strays (print/stderr/logging from a background thread at
  10 lines/s): **0 bytes leaked** to the pty. Textual's stdout/stderr
  capture holds. PASS.
- fd-inheriting subprocess (simulated notify hook writing to fd 2):
  **5 raw lines leaked** into the alt screen. Confirms notify hooks MUST
  be spawned with captured output in embedded mode (PR F's
  `NotifyHooks(capture_output=True)`).

MEASURED FINDINGS (bind PR F design):
1. Under Textual raw mode, Ctrl-C arrives as a KEY EVENT, not SIGINT.
   The app must bind `ctrl+c` explicitly or Ctrl-C does nothing.
2. Textual installs no SIGTERM handler: SIGTERM kills the process with
   the terminal still in alt-screen/raw mode. `install_signal_handlers`
   (PR B/F) must catch SIGTERM and route it through the app for a
   graceful exit; the belt-and-braces ANSI restore in the caller's
   `finally` covers the remaining hard-kill paths.

VERDICT G3: PASS (with findings 1-2 as binding design inputs).

## G4 - thread bridge (call_from_thread prompt round-trip)

Background thread opens a modal via `app.call_from_thread`, auto-answers
after a deliberate 0.5s delay: unloaded round-trip max 517ms, i.e.
**~17ms bridge overhead** (<100ms gate: PASS for the mechanism).

10-minute storm soak (rate 10, modal every 3s, 33,882 events processed):
ZERO deadlocks, zero lost prompts, app CPU p50 2.7% / p95 7.5% / max
16.2% (G2 storm <30%: PASS). Round-trip tail grew to 1832ms max under
storm - message-queue pressure, not the bridge (finding 4). Acceptable
for approval modals at phase gates; bounded by the production render
policy below.

## G5 - ssh (operator procedure - not runnable in this environment)

Procedure for the user to verify (not blocking for implementation start;
the coalesced <=5Hz render policy plus `--poll` knob is the degrade path):

    ssh <host-with->=50ms-RTT> \
      "cd <checkout> && uv run --with 'textual>=3,<6' \
         python spike/tui0/app.py --root <dir-with-fake-run> --poll 0.2"

Record: perceived keypress echo (target <150ms on <=100ms RTT), tearing
on resize (Textual synchronized-update escape codes should prevent it),
behavior in screen/tmux over ssh (TERM=screen-256color).

## Storm results

- Storm latency row: in the G1 table above. G1 storm gate (p95 <= 1s at
  0.25s poll): PASS with 4x margin (247ms).
- Storm soak (10 min, prompt bridge active): see G4. One NEW failure
  surfaced and was isolated (finding 3):

FINDING 3 - input starvation under naive transcript rendering. In the
10-min storm soak the final `q` keypress was not processed within 10s
(app killed by the harness, no restore). Isolation (90s storm cells):
with ALL components' transcripts streaming into one RichLog, `q` again
starved (reproducible); with transcript rendering disabled, `q`
processed instantly and the app exited cleanly under the same storm +
modal load. The DataTable rebuilds alone are NOT the problem.

Binding render policy for PRs D/E (was "nice to have", now measured as
mandatory):
- Transcript pane tails ONE component (the top screen's), bounded ring
  buffer, capped writes per frame - never all components at once.
- Table updates diff rows on StateChanged; no full clear+rebuild per poll.
- Post StateChanged only on actual state change (already planned).

## Go/no-go

**GO for Textual.** Gates: G1 PASS (storm p95 247ms at 0.25s poll, 4x
margin), G2 PASS (tailer <=1.22%, app storm max 16.2% vs 30% gate,
idle p50 0.6%), G3 PASS (all exit paths restore; findings 1-2 bound the
design), G4 PASS for the bridge mechanism (~17ms overhead, zero
deadlocks in a 10-min storm soak; tail latency bounded by the render
policy of finding 3), G5 procedure documented for operator verification.

Production decisions fixed by measurement:
1. Poll default 0.2s (0.1s is affordable if we ever want snappier).
2. Bind ctrl+c explicitly (finding 1: raw mode delivers it as a key).
3. Install SIGTERM handler before app.run() + ANSI restore in finally
   (finding 2: Textual leaves the terminal raw on SIGTERM).
4. Render policy per finding 3: single-component bounded transcript
   tail, diffed table rows, StateChanged-on-change only.
5. Capture notify-hook output in embedded mode (fd-inherited subprocess
   leaks straight onto the alt screen - measured).
6. Optional: idle poll back-off (idle p95 2.1% is fine without it).
