# The loops

kstrl is a set of nested loops, not a pipeline. Each loop has a clock, something that acts, and something that measures the result without having produced it. The rule under all of them: what acts never measures its own result.

Values below are the defaults at `main@33104a9`, with the file that sets them.

| # | loop | clock and bounds | acts | measures | module |
|---|---|---|---|---|---|
| 1 | implement | 2 s between iterations (`sleep_seconds`, config.py:287), up to 10 iterations per attempt (`max_iterations`, config.py:255) | the engineer agent, in its own process group, writing only its worktree | the no-progress breaker (trips after 3 iterations without progress, breaker.py:62), the scope fence | `loop.py` |
| 2 | accept | up to 4 attempts per component (1 + `max_retries=3`), 5 s between (`retry_delay`, factory.py:216-217) | a retry, carrying a level-triggered context: *current failures*, *not re-measured*, and a count of *resolved or superseded* (context.py) | verify (mechanical, including diff scope), review, security | `pipeline.py`, `context.py` |
| 3 | integrate | up to 4 components in parallel (`max_parallel`, forced to 1 without worktrees or in single-PR mode); scheduler slices of 0.5 s; merge polled every 10 s for up to 300 s (`merge_timeout`) | schedule, open PR, merge, reset | contract tests per tier, integration review, CI on the merge commit | `factory.py`, `pr.py`, `contract.py` |
| 4 | intake (`ks serve`) | sleeps 60 s after each cycle; a cycle admits at most one queue item and runs its factory to the end before sleeping | queue admission | the admission gates below | `serve.py`, `workqueue.py`, `intake_github.py` |
| 5 | trust | once per run, at the end, only when `[autonomy] enabled` (default **off**) | the autonomy level | decisive runs, merges, policy violations | `autonomy.py`, `calibration_ladder.py` |
| 6 | learn | when a person runs `ks evolve` or calibration; the evolution journal and fact utilization are written automatically by every run | knowledge facts, the playbook (`ks learn`) | fact utilization, planted-bug detection rate | `evolution.py`, `knowledge.py`, `calibration.py` |
| 7 | the operator | minutes to days | answers, limits, guidance | everything above | you |

## Admission gates of `ks serve`, in the order a cycle evaluates them

1. `[serve]` and `[queue]` config readable.
2. Spend ledger readable.
3. (Not gates: stale leases reaped, GitHub intake synced.)
4. Not paused (pause marker).
5. Three refusals evaluated together: poison streak below 3; cost coverage (only when a budget is set); daily budget (`daily_budget_usd`, default 0 = off; a breach pauses until the next local midnight).
6. Wait gates: inbox below 50 open items; factory lock free; no merges parked for approval; open PRs below 1.
7. The next ready item.
8. Merge-gate resolution for that item (a refusal poisons it).
9. `queue.start` refuses an item past `max_attempts=3`.

A poison-streak pause has no resume time: it waits for `ks queue resume`.

## Autonomy levels

| level | label | the person's remaining role | offered after (placeholders) |
|---|---|---|---|
| L1 | Supervised | approves the plan and the merge | start |
| L2 | Gated-merge | gates the merge | 5 components merged at L1 |
| L3 | Enveloped auto-merge | none while green and inside the policy envelope | 15 consecutive clean merges at L2 |
| L4 | Deploy | none; adds the release stage | 30 components merged at L3 |

Every threshold is labelled an UNMEASURED PLACEHOLDER in the code (autonomy.py:131-141). Any automatic transition needs 8 decisive runs. Promotion is never automatic: `ks autonomy promote --actor --ack [--force]`, one level per call, from a terminal only; a forced promotion is recorded as `forced_over_blockers`. Demotion drops one level and locks re-promotion for 10 decisive runs; by default only a policy violation demotes automatically. Without `[policy] enabled`, L2 is the ceiling.

## Defaults that change what a screen can show

- **Run budgets are off by default**: `max_total_tokens`, `max_cost_usd` and `max_adversarial_calls` are all 0. A spend figure usually has no denominator. Token and cost ceilings are checked between iterations, so a run can overshoot one.
- **Work timeouts are off by default**: `agent_iteration` and `component_total` are 0 (no limit). `kstrl.toml.example` still shows 1800 and 7200.
- **The merge checkpoint is off by default** (`pause_before_pr_merge = false`, `create_prs = true`): the factory opens and merges PRs without asking. L1 and L2 force it on.
- **Autonomy is off by default**; the config's own flags then stand.
- **Convergence check is off by default** (`convergence_attempts = 0`).

## For design

- A value can be a default, a configured limit, or absent. Show which. A cap of 0 means *no cap*, never *0 allowed*.
- The periods run from 2 seconds to weeks. Nothing on a screen should refresh faster than the loop it shows.
