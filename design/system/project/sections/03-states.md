# States

A component (one part of the spec, with its own branch and PRD) is in one of eight statuses, stored in `scripts/kstrl/manifest.json` (`ComponentStatus`, manifest.py:47-68).

| status | glyph | enters when | leaves to |
|---|---|---|---|
| pending | ○ | the manifest is written; a retry; crash recovery (running or verifying reset); a contract failure sends a completed part back; `ks retry` | running, once every dependency is **completed** and a slot is free |
| running | ● | the scheduler launches the engineer | verifying, when the engineer phase closes |
| verifying | ◐ | the phase chain starts | completed, merge_pending, awaiting_approval, pending (retry) or failed |
| completed | ✓ | the merge is confirmed; or, with no `gh`, the chain passes and nothing is pushed | pending, if a contract test fails |
| merge_pending | ⏸ | a merge was started and not confirmed within 300 s | completed on the next run's re-poll (which emits `pr_merged`), or failed |
| awaiting_approval | ◇ | every gate passed and the merge gate had nobody to ask; a `merge_gate` inbox item is filed | completed via `ks inbox approve` on the next factory run (the approved commit must still match the branch); failed via `ks inbox reject` |
| failed | ✗ | see the reasons below | pending, via `ks retry` (which also resets the retry counter and the cascade-skipped dependents) |
| skipped | ◌ | a dependency failed (emits `component_skipped`) | pending, when the failed dependency is retried |

Dependents are scheduled only past **completed**. A part parked in merge_pending or awaiting_approval holds its dependents.

## The phase chain

Inside running and verifying, a component runs these phases in order. Each emits `phase_started` and `phase_completed` (or `phase_skipped` with a reason).

`engineer` → `verify` → `diff` → `review` → `security` → `distill` → checkpoint → `pr`

| phase | who | notes |
|---|---|---|
| engineer | the engineer agent | up to 10 iterations |
| verify | mechanical, no LLM | tests, typecheck, lint, diff scope, bad patterns, self-critique shape |
| diff | mechanical | fetches the component's diff once for the distiller, the checkpoint, fact utilization and the PR body |
| review | reviewer agent | per-criterion verdicts against the PRD, blocking findings |
| security | security agent | OWASP-mapped findings |
| distill | distiller agent | durable facts, written before the PR |
| checkpoint | a person | emits `checkpoint_requested` / `checkpoint_resolved`, not a phase event. Runs only with `create_prs` on, single-PR off and the merge gate on |
| pr | `gh` | push, open, merge, poll every 10 s |

## Why a part fails

Retries exhausted; rejected at the checkpoint; rejected from the inbox; token or cost budget; adversarial-call budget; its PR was closed; no-progress breaker tripped; not converging (only when `convergence_attempts` is set); push, create or merge failure; scheduler timeout; aborted by shutdown (phase `aborted`); merge gate with the inbox disabled; the approved commit no longer matches the branch.

Other transitions: a merge conflict retries against the freshly merged base; crash recovery resets running and verifying to pending.

## Standing conditions, read beside the states

- **Liveness**: the age of the last heartbeat and of the last output.
- **Attempt and iteration**: try n of 4, iteration n of 10.
- **Spend**: per axis (tokens, dollars), marked as a lower bound when any call did not report that axis.
- **Safe mode**: four signals (below).
- **Autonomy**: the level, and whether it was clamped by `max_level` or by a disabled policy.

## Known disagreements between the stream and the manifest

- `component_retrying` folds to *running* in the reducer, while the manifest says *pending* between attempts (reducer.py:555-557). The board shows running.
- The board can show *unknown* (`?`) for a component the stream cannot place.

## For design

- Read status from the manifest and time from the event stream. Do not derive one from the other.
- merge_pending and awaiting_approval are both waiting on something outside the run, and both hold dependents. Give them a shared visual family (they share `violet`) and distinct words.
