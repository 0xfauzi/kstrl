# During a run

Six questions an operator asks mid-run, each with the measurement kstrl writes for it, and what is not written.

| question | the measurement | what is missing |
|---|---|---|
| **Is it converging?** | The failing gate's count per attempt: for verify, the parsed failures (or 1 per failing check); for review and security, the blocking-finding count. Carried in `verification_result` and `review_result` events. `review_divergence` fires when the diff grew while the blocking findings stayed a superset of last time (#265). | The convergence check (#233) is off by default, and its history is in memory only and resets when the failing phase changes. |
| **Is it alive?** | `worker_heartbeat` every 15 s; the last output time; iteration n of 10. | Nothing acts on staleness. The TUI's 45 s and 60 s thresholds are labels. |
| **Where is each part?** | Status (manifest), phase (`phase_started` / `phase_completed`), try, iteration, and which dependencies it waits on. | Between attempts the board says running while the manifest says pending. |
| **Did the sensors agree with the claim?** | Per story: the engineer's `passes` flag in the component's `prd.json`; the reviewer's per-criterion verdict with a `story_id`, rolled up per story, and a claim-agreement check (review.py:442). Verify is per component per attempt, contract tests per tier, CI per merge commit. | Per-story verdicts are **not persisted**: events carry counts; the full list survives only in the PR body. A per-story view needs new persistence first. The TUI shows no per-story data. |
| **What has it cost?** | Usage per component per phase (`component_usage` with `phase`), rolled up at run end and in the evolution journal. Lower bound per axis. Run-level ceilings: tokens, dollars, adversarial calls. Reference cost per engineer iteration: about $1.70 to $2.60 on a first attempt and $3.99 to $7.42 on a retry (serve.py:5-7; measured in docs/env-vars.md). | The reducer folds usage per component and per run, not per phase. The adversarial-call count is kept in memory and never emitted: only its cap is known. All three ceilings default to off. |
| **Is anything not being measured?** | Safe mode (`safemode.py`), four signals: the control directory is untrusted; autonomy fell back to L1 or was clamped; the queue is paused; review or security did not run in the newest finished run. Per component, `phase_skipped` records the phase and the reason (`--no-verify`, `adversarial LLM budget exhausted`, `security review not configured`, `mode=skip`). | Safe mode reports a count per phase, not which components. |

What the operator does, most frequent first: nothing; read a stuck part's output; answer a checkpoint; retry with scope; write a line of guidance; stop the run; open the PR; raise a limit.

## For design

- Draw the measurement, not a summary word. A skipped reviewer must look like an absence, never like a pass.
- A figure with no ceiling has no percentage. A count that is not emitted (adversarial calls) is shown as its cap and *count not recorded*, never as 0.
