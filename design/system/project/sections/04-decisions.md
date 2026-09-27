# Decisions

Every decision kstrl can ask of a person, grouped by what the person actually decides. The last column is the state of the channel at `main@33104a9`; where it is broken or missing, a new surface must fix it or say so, not draw around it.

| decision | loop | choices and effect | what waits | channel today |
|---|---|---|---|---|
| **Answer a spec escalation** | before 3 | The architect refused a product, scope or risk judgement. Edit the spec and re-run decompose. The item closes itself when a later decompose escalates nothing. | Everything for that spec: no manifest. Decompose exits 2. | `spec_escalation` inbox item (#449); the answer itself happens outside kstrl, in the spec file |
| **Merge this part** (E6 checkpoint) | 2 | Approve: push, open the PR, merge; completes on a confirmed merge. Reject: fails, dependents skipped, nothing pushed. Retry: one more attempt with a fixed note that a human asked for changes (your words are not passed on). Later (TUI Esc): leaves it pending. | That component, with no timeout. The run stays blocked. | TUI checkpoint dialog. With no interactive UI: parked as awaiting_approval with a `merge_gate` item, answered by `ks inbox approve/reject` on the next run. **Fails open in one case**: a prompt whose answerer went away, or an out-of-range answer, merges. |
| **Retry a failed part** | 2, 3 | Retry within the recorded scope (resets its cascade-skipped dependents and the retry counter), or leave it failed. | Its dependents stay skipped. | TUI retry screen (previews on a copy, changes nothing until Start retry). `ks retry` saves the manifest and deletes the worktree and branch **before** its confirm. `ks inbox retry` has no confirm. |
| **Authorise more spend** | 2, 4 | Raise a run limit (`--max-cost-usd`, `--max-total-tokens`, `--max-adversarial-calls`; a retry refuses to drop one); reset a poisoned queue item (`ks queue retry --reset-attempts`); resume a paused queue. | The run halts; the queue pauses until midnight (budget) or until resumed (poison). | CLI only. `budget_overrun` inbox items are record-only. No queue screen in the TUI. |
| **Let an exception through** (policy, test adequacy) | 2 | Approve or reject the item. | The part has already failed. | **Record-only**: nothing reads the answer back. Widening a policy is a config edit. |
| **Stop or steer a run** | 3 | Stop: SIGINT or TUI `q` then confirm; agent process groups killed; running parts fail with phase `aborted`; exit 130. Steer: a line of standing guidance in `scripts/kstrl/memory.md`, which the engineer prompt reads after the retry context; on GitHub, `/memory` and `/iterate`. | Nothing. These are yours to take. | Stop works everywhere. Guidance has no local UI. |
| **Queue work** | 4 | `ks queue add` (priority as an integer; stop at PR by default, `--auto-merge` still gated by the level), `ls`, `show`, `retry`, `rm` (confirms), `pause`, `resume`, `sync` from GitHub (synced items always stop at the PR). | The next serve cycle. | CLI only. |
| **Change the autonomy level** | 5 | Promote one level with an acknowledgement, from a terminal, when the thresholds are met or forced (recorded as forced). Demote by hand. | Nothing: the level applies at the next run. | `ks autonomy`, TTY only. |
| **Accept a lesson** | 6 | None. `ks evolve` prints candidate lessons "no writer until the playbook ships"; `--apply` was removed (#507). | Nothing. | **Missing.** |

## Inbox kinds (inbox.py:55-70)

Nine kinds. Six are action-required and notify: `spec_escalation`, `merge_gate` (two forms: a park, and *merge unconfirmed*), `halted_run`, `budget_overrun`, `policy_exception`, `test_adequacy`. Three inform and batch into a digest: `demotion_notice`, `calibration_drift`, `health_breach`. Snooze defaults to 24 h.

## Blocking prompts outside the inbox

The E6 checkpoint; the disallowed-changes guard (Quit / Revert and continue / Continue anyway); the iteration pause (Continue / Skip interactive / Quit); the feature gate; the factory confirm; the retry confirm; and `ks queue rm`'s plain confirm.

## For design

- A decision surface must call the same code path as the CLI and must not invent a channel. Where the channel is record-only, say *recorded, not acted on* next to the button.
- Every choice states its consequence before it is taken, in the factory's words (see the checkpoint's *what each choice does*).
- Informational items get a place, not a button.
