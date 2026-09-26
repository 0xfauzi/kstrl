# kstrl Environment Variables Reference

> Rename note (2026-07-20): ``KSTRL_*`` is the primary namespace. The
> legacy ``KSTRL_*`` spelling of every variable below is honored for one
> release with a DeprecationWarning (see ``kstrl/envcompat.py``). Bare
> ``FACTORY_*`` names remain accepted for the factory family.

Every config dataclass has a `from_env()` classmethod that reads env vars, and a `load(root_dir)` classmethod that overlays env on top of `kstrl.toml` (env wins). This doc enumerates every variable the harness consults.

Precedence: **CLI flag > env var > `kstrl.toml` > dataclass default**.

When is a bad value caught? At command entry, before the command builds
or spends anything. Every section below is resolved once by
`kstrl/config_preflight.py`, and a value that will not parse stops the
command with an `error:` line naming the section, the key or environment
variable, and the value (exit 2). `[evolution]` is the one section that
warns and continues, because the journal is an optional audit trail;
losing it costs the record and nothing else. `ks evolve` is the one
command that section is fatal for, because there the journal is the work.

`ks init` skips the check: it writes the file, and refusing to replace a
file it cannot parse would take away the recovery path. `ks config show`
and `ks check` skip only the entry seam and run the same check
themselves: `config show` prints every row it can resolve, then names
each rejected section with its key and value and exits 1, and `ks check`
reports through its documented exit 2 (with a JSON error document for
`ks check --json`).

When everything else refuses, `ks config show` is the command guaranteed
to run and tell you which section, key and value to fix.

## Global / kstrlConfig (`[agent]`, `[run]`, `[paths]`, `[git]`, `[ui]`)

| Env var | Type | Default | Notes |
|---|---|---|---|
| `MAX_ITERATIONS` | int | 10 | Per-component max agent iterations |
| `PROMPT_FILE` | path | `scripts/kstrl/prompt.md` | |
| `PRD_FILE` | path | `scripts/kstrl/prd.json` | |
| `PROGRESS_FILE` | path | `scripts/kstrl/progress.txt` | Setting it forces that path on every factory component; unset, each component's engineer writes `progress.txt` beside its own PRD, inside the component's `allowedPaths` |
| `CODEBASE_MAP_FILE` | path | `scripts/kstrl/codebase_map.md` | |
| `KSTRL_GOLDEN_PATTERNS_FILE` | path | `scripts/kstrl/golden-patterns.md` | Operator-authored patterns injected into every factory engineer prompt and every `ks run` prompt (not `ks feature` or `ks understand`); absent, empty, unreadable or still the unedited `ks init` scaffold costs nothing, and past 6000 characters the block keeps the start of the file and drops the end, with an announcement naming both |
| `KSTRL_MEMORY_FILE` | path | `scripts/kstrl/memory.md` | Operator-authored standing feedback injected into every factory engineer prompt and every `ks run` prompt (not `ks feature` or `ks understand`), AFTER the retry context and before the `CLAUDE.md` prepend; absent, empty, unreadable or still the unedited `ks init` scaffold costs nothing, and past 4000 characters the block keeps the END of the file and drops the start, so the newest entries survive, with an announcement naming both |
| `SLEEP_SECONDS` | float | 2.0 | Inter-iteration sleep |
| `INTERACTIVE` | bool | false | Pause between iterations for human input |
| `ALLOWED_PATHS` | comma-list | empty | Restrict agent writes to these prefixes |
| `KSTRL_BRANCH` | str | unset | Override branch checkout; `""` means skip checkout |
| `KSTRL_AUTO_CHECKOUT` | bool | true | When false, run loop skips branch resolution |
| `AGENT_CMD` | str | unset | Custom shell command for the agent (overrides type) |
| `MODEL` | str | unset | Model name passed to the agent |
| `MODEL_REASONING_EFFORT` | str | unset | `low\|medium\|high\|max` |
| `KSTRL_AGENT_TYPE` | str | unset | `claude-code\|claude-sdk\|codex\|auto` (`claude-sdk` needs the `sdk` extra: `uv sync --extra sdk`) |
| `KSTRL_AGENT_BUDGET_USD` | float | unset | In-loop USD budget ceiling; enforced per turn by the `claude-sdk` adapter only (R7.6). Non-positive or unparseable values are ignored |
| `KSTRL_AGENT_PROBE` | bool | true | Liveness-probe the agent CLI before any spend (#262): one trivial turn per model family per process, in a scratch directory so no project config participates. `0` skips it and restores the pre-#262 behaviour of trusting PATH. **Env only** - deliberately no `kstrl.toml` key, because the cross-family probe runs where no config object is in scope. Measured per attempt: claude 4.2s / $0.025 with `--model haiku`, 4.1s / $0.149 for the fallback attempt that drops `--model` (only run when the first fails); codex 6.1s and no cost reported. `ks understand` and `ks feature` probe one family; `ks run`, `ks factory` and `ks retry` probe up to two, and skip the second entirely when no adversarial phase can dispatch. `ks serve` runs a fresh `ks factory` per queue item, so set this to `0` there if the per-item toll matters |
| `KSTRL_UI` | str | auto | `auto\|rich\|plain` |
| `KSTRL_NO_TUI` | bool | unset | `1` disables the embedded factory dashboard (plain output) |
| `NO_COLOR` | bool flag | false | Disables colors |
| `KSTRL_ASCII` | bool | false | ASCII-only UI |
| `GUM_FORCE` | bool (`1`) | unset | Legacy name: `1` makes `--ui auto` use the rich renderer even when stderr is not a terminal |
| `XDG_STATE_HOME` | path | `~/.local/state` | Base for R8.9 control state (`$XDG_STATE_HOME/kstrl/<repo-id>/`: autonomy, inbox, spend, pause, GitHub processed ledger). Must stay outside the repo tree for L3+ |

## TimeoutConfig (`[timeout]`)

All values are seconds; 0 or less disables that limit. When a limit on how long kstrl's work may take is not set, there is no limit (#467): every work limit here and in `[verify]`, `[security]`, `[contract]` and `[knowledge]` defaults to 0. The run header and `ks config show` print an unset limit as `no limit`.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_TIMEOUT_AGENT_ITERATION` | float | 0 (no limit) | One engineer iteration |
| `KSTRL_TIMEOUT_COMPONENT` | float | 0 (no limit) | Wall clock per component across iterations |
| `KSTRL_TIMEOUT_BACKSTOP_MARGIN` | float | 60 | Extra slack before the scheduler declares a worker dead. Applies only when `component_total` is set |

With no work limit, an agent that hangs without output holds its component slot until Ctrl-C or `[serve] factory_timeout_seconds`. It spends nothing while idle.

## FactoryConfig (`[factory]`)

| Env var | Type | Default |
|---|---|---|
| `FACTORY_MAX_PARALLEL` | int | 4 |
| `FACTORY_MAX_RETRIES` | int | 3 |
| `FACTORY_RETRY_DELAY` | float | 5.0 |
| `FACTORY_MERGE_TIMEOUT` | float | 300.0 (hang guard, kept when unset) |
| `KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS` | int | 0 (no limit) |
| `KSTRL_FACTORY_MAX_TOTAL_TOKENS` | int | 0 (no limit) |
| `KSTRL_FACTORY_MAX_COST_USD` | float | 0 (no limit) |
| `KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE` | bool (`1`/`true`/`yes`) | false |
| `KSTRL_FACTORY_PROGRESS_LOG_ENABLED` | bool | true |
| `KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE` | bool | false |
| `KSTRL_FACTORY_INTEGRATION_REVIEW` | bool | true |
| `KSTRL_FACTORY_INTEGRATION_BLOCKING` | bool | false |
| `KSTRL_FACTORY_INTEGRATION_MAX_ROUNDS` | int, at least 1 | 1 |
| `KSTRL_FACTORY_CONVERGENCE_ATTEMPTS` | int, at least 0 | 0 (off) |
| `KSTRL_FACTORY_CLAIM_AGREEMENT` | `advisory` \| `block` | advisory |

The two safety knobs (E4 `max_adversarial_calls`, E6 `pause_before_pr_merge`) are reachable via all three surfaces since R2.2: the env vars above, `[factory]` keys in kstrl.toml, and the `--max-adversarial-calls` / `--pause-before-pr-merge` CLI flags.

Setting `pause_before_pr_merge` on any of those three surfaces is an EXPLICIT request, and since #195 the autonomy ladder may not remove it. At L3 and L4 the ladder's flag bundle permits auto-merge; that is a permission, not an instruction, so a gate the operator asked for stays up and the run records `gate retained by explicit request` in its log and in the `autonomy_level_applied` event. The reverse still does not hold: an explicit `false` does not lower a gate the ladder raised at L1 or L2, because the ladder may always withhold autonomy. A value nobody wrote is not a request, so with no key, no env var and no flag the level alone decides.

Provenance is key PRESENCE, never a value comparison: `KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE=0` is an explicit `false`, not an absent setting. The `[factory] pause_before_pr_merge` toml key is read strictly for the same reason `[autonomy] enabled` is: `pause_before_pr_merge = "false"` is a string and `= 0` is an integer, and coercing either would manufacture an explicit request nobody wrote. A non-boolean is refused at command entry as a configuration problem rather than loaded, and the message names the type it read rather than guessing which spelling the operator used. The env var is unchanged and stays lenient (`1`/`true`/`yes` are true, anything else false), because an env var is a string by construction.

Which commands honour the merge gate (#207): `pause_before_pr_merge` applies only to per-component PR creation - `ks factory` with `single_pr` off, and `ks serve` (which passes the gate decision through to its `ks factory` invocations). It is NOT honoured by `ks run` (a local, single-component, no-PR invocation that forces `create_prs = false`) or in `single_pr` mode (the aggregate PR is created without a checkpoint). Whenever the FINAL resolved config - checked after the autonomy ladder resolves, since the L1/L2 bundle can force the gate on - has `pause_before_pr_merge = true` while the checkpoint is unreachable, `run_factory` prints a startup warning rather than silently ignoring the flag.

### The two run-level ceilings: `max_total_tokens` and `max_cost_usd` (R8)

Both are configurable, both may be set at once, and whichever is reached first
halts the run. They are **not** interchangeable.

#### Why `max_total_tokens` is a poor proxy for cost

`UsageTotals.total_tokens` counts **cache reads at par** with input tokens, and
cache reads cost roughly an order of magnitude less. A real run halted on
`max_total_tokens = 500000`; its own journal recorded:

| Field | Value |
|---|---|
| `input_tokens` | 52 |
| `output_tokens` | 20,855 |
| `cache_read_tokens` | **1,781,669 (95.6%)** |
| `cache_creation_tokens` | 61,505 |
| `total_tokens` | 1,864,081 |
| `cost_usd` | **1.216512** |

The operator who set a 500k "budget" expecting a spend ceiling was stopped at
**$1.22**. The token cap measures something real, but nearly uncorrelated with
money. Set `max_cost_usd` when what you mean is "do not spend more than $X".
`max_total_tokens` remains supported and is still the right knob for bounding
context throughput rather than spend.

#### What either ceiling guarantees

A **stop-before-the-next-unit-of-work** limit, not a hard cap. `max_cost_usd`
carries exactly the same guarantee and exactly the same gaps as
`max_total_tokens` - it is not stronger for being denominated in dollars. Each
is evaluated in two places:

- **Between engineer iterations** (`kstrl/loop.py`, `LoopBudget.halt_reason`).
  The worker is launched with the ceilings plus the run's spend as of that
  moment, so the loop refuses to start another iteration once a total reaches
  its ceiling. This is the only check that fires while the spend is being
  incurred.
- **At phase boundaries** in the parent (`pipeline.process_result`, the review
  / security / distill gates, and the scheduling gate). These stop the next
  phase or the next component.

Either route halts the component loudly and identically: a `budget_exceeded`
event (carrying `ceiling`, which names the one that tripped), a typed
`infrastructure_error` finding, and exactly one `budget_overrun` inbox item.

What is **not** bounded, for both ceilings:

| Gap | Why |
|---|---|
| The iteration already running | Nothing interrupts a single agent call mid-flight. Overshoot is up to one iteration per running worker; `KSTRL_TIMEOUT_AGENT_ITERATION` bounds that in wall clock, never in tokens and never in dollars. Measured: the run above overshot its entire 500k cap by **3.7x inside one engineer call of 376s** |
| Concurrent workers | Each worker sees the run total as of its own launch. With `FACTORY_MAX_PARALLEL = N`, up to N iterations can be in flight past the ceiling |
| Unreported spend | Every token and cost figure is a CLI self-report. Calls that report nothing count as zero, so totals are lower bounds whenever `unreported_calls > 0` and the halt can arrive late. A loop that reports *nothing* is a separate case and halts outright - see below |
| Roles whose adapter reports one axis and not the other | A ceiling only counts calls that reported the figure it is denominated in, so it can bound part of a run while reading as healthy - see below |

#### A ceiling covers only the calls that report its figure

Measured on a paid run that set `--max-cost-usd 25.0`. Its own per-phase
`component_usage` events:

| phase | calls | tokens | cost | cost_calls |
|---|---|---|---|---|
| engineer | 5 | 8,036,800 | $9.9929 | 5 |
| review | 2 | 78,157 | $0.0000 | 0 |
| engineer | 1 | 7,939,537 | $7.3448 | 1 |
| review | 3 | 115,476 | $0.0000 | 0 |
| engineer | 1 | 7,404,185 | $7.4238 | 1 |
| engineer | 1 | 2,947,879 | $3.9930 | 1 |

The run's total cost equalled the engineer total **exactly**: 193,633 reviewer
tokens over 5 calls contributed **$0**. Tokens were counted across every role
(26,522,034 run vs 26,328,401 engineer) - only the dollar figure under-counted,
because the cross-family reviewer (codex) reports a token total and no cost.
That is an adapter capability gap, not a mis-wired meter.

Nothing was breached and no ceiling was *unenforceable* (the engineer reports
cost on every call), so every surface reported the run as healthy - including
the rollup's lower-bound footer, which keys off `unreported_calls` and saw 0
because every call reported *something*.

kstrl now states the gap instead of implying it. **No price is ever inferred for
an uncovered call**: the uncovered magnitude is reported in tokens, because
converting it to dollars would need a price table the harness does not have, and
a fabricated cost in an audit trail is worse than a missing one.

| Surface | When |
|---|---|
| `Cost ceiling` / `Token ceiling` lines in the run preflight | At the plan stage, stating what the ceiling counts. Deliberately says nothing about *which roles* will be covered - no call has been made yet, so that would be a prediction |
| `budget_coverage` event (`events.jsonl` **and** `progress.jsonl`) plus a `BUDGET COVERAGE:` warning | Once per ceiling per run, at the first phase whose calls report nothing on that axis - the earliest point the evidence exists |
| `coverage` on the `budget_exceeded` event, the component error, and the `budget_overrun` inbox item | At the halt. Recorded for **every** configured named ceiling, including fully-covered ones, so an absent field means "written before this landed" rather than "no gap" |
| Per-axis `note:` lines under the usage rollup | At the run summary, naming the uncovered roles |

The ceiling's **semantics are unchanged**: `max_cost_usd` still halts on reported
dollars only. Whether a partially-covered ceiling should instead refuse to run is
a policy question, not a reporting one, and is deliberately left open.

#### Unenforceable ceilings are per-ceiling

Unknown usage is deliberately **not** silently treated as zero in the case where
that would make a ceiling undeliverable. A ceiling is *unenforceable* when
**both** hold:

1. **this engineer loop** has reported none of the figures that ceiling needs on
   any of its calls (`token_calls == 0` for the token ceiling, `cost_calls == 0`
   for the cost one), so its run total cannot grow while the loop runs (the
   spend recorded before this worker launched is frozen at launch); **and**
2. the **engineer** has now made two calls that reported that figure not at all
   - counted across the run's engineer loops, so the threshold does not reset on
   every attempt or component, while another role's timed-out call never counts
   (it is no evidence about the engineer's adapter).

The run halts as unenforceable only when **every configured ceiling** is dead.
The two axes have genuinely separate coverage:

| Adapter behavior | Token ceiling | Cost ceiling |
|---|---|---|
| codex: token total, no cost | enforceable | unenforceable |
| claude with a missing `usage` dict: `total_cost_usd`, no tokens | unenforceable | enforceable |
| custom `agent_cmd`: reports nothing | unenforceable | unenforceable |

Only the last row halts. An adapter that reports cost but not tokens still
enforces `max_cost_usd`, and killing the run because the token ceiling died
would discard a ceiling that still works. This also fixes an inconsistency: the
old rule counted only `token_calls`, so a cost-only adapter was condemned even
though it could have enforced a spend ceiling perfectly well.

A loop that emits the completion marker returns before its own budget check, so
a component that finishes on a single silent call cannot halt itself. The
scheduling gate catches that case instead: once every configured ceiling is
dead, the run refuses to start further components. Spend is therefore bounded by
the component already in flight, not by zero.

The threshold counts only the calls that reported **nothing for that axis**, so a
lone unparseable result in an otherwise-reporting run is still treated as an
incident, not a dead adapter. The flip side, on purpose: once a run has
accumulated one such call, the engineer's next silent iteration reaches the
threshold and halts. Two independent silent calls in one run is adapter behavior
(a custom `agent_cmd` never reports usage), and a loud, recoverable halt beats
spending under a ceiling that cannot fire.

#### `max_cost_usd` is not `[agent] budget_usd`

`KSTRL_AGENT_BUDGET_USD` / `[agent] budget_usd` is **adapter-internal**: it is
enforced inside a single turn by the `claude-sdk` adapter only, and
`claude-code`, `codex`, and custom commands ignore it entirely. It knows nothing
about the run. `max_cost_usd` is the run-level ceiling across every phase and
every component, enforced by the harness. `budget_usd` is the only genuine
in-turn ceiling kstrl has, and that is exactly what `max_cost_usd` is not - use
both if you want the in-flight iteration bounded too.

### Usage accounting vs progress logging

`FACTORY_PROGRESS_LOG_ENABLED=0` (or `[factory] progress_log_enabled = false`)
turns off `progress.jsonl` and the run's `events.jsonl`. It does **not** turn
off usage accounting: every run still allocates
`.kstrl/runs/<run_id>/components/<id>/engineer_usage.json`, a small snapshot
the engineer loop rewrites at each iteration boundary so a worker killed by a
shutdown does not take its spend to the grave. An observability opt-out may
drop the narration; it must never drop the meter.

## BreakerConfig (`[breaker]`)

No-progress circuit breaker (R7.5): the engineer loop halts loudly when N
consecutive iterations produce an unchanged diff hash AND an unchanged
test-failure signature.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_BREAKER_ITERATIONS` | int | 3 | Consecutive no-progress iterations before the halt; 0 disables |
| `KSTRL_BREAKER_TEST_CMD` | str | unset | Stall-probe command; unset falls back to the explicit `[verify]` test_command, else diff-hash only |
| `KSTRL_BREAKER_TEST_TIMEOUT` | float | 300 | Seconds before the stall probe is killed |

## SandboxConfig (`[sandbox]`)

OS-level agent sandboxing (R7.5), applied by the claude-code and codex
adapters (ignored, loudly, for custom agent commands). Write scope is the
agent's worktree by construction on both CLIs.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_SANDBOX_ENABLED` | bool | false | Opt-in OS sandbox for agent subprocesses |
| `KSTRL_SANDBOX_ALLOW_NETWORK` | bool | false | Re-open outbound network inside the sandbox |

## VerifyConfig (`[verify]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_VERIFY_TEST_CMD` | str | unset (uses `uv run pytest`) |
| `KSTRL_VERIFY_TYPECHECK_CMD` | str | unset (uses `uv run mypy .`) |
| `KSTRL_VERIFY_LINT_CMD` | str | unset (uses `uv run ruff check .`) |
| `KSTRL_VERIFY_TEST_TOOL` | `pytest` \| `vitest` | unset = run both parsers and union the failures |
| `KSTRL_VERIFY_TYPECHECK_TOOL` | `mypy` \| `tsc` | unset = run both parsers and union the failures |
| `KSTRL_VERIFY_LINT_TOOL` | `ruff` \| `eslint` | unset = run both parsers and union the failures |
| `KSTRL_DEAD_CODE_CLEANUP` | bool (`1`) | false |
| `KSTRL_DEAD_CODE_CMD` | str | unset |
| `KSTRL_MUTATION_TESTING` | bool (`1`) | false |
| `KSTRL_MUTATION_THRESHOLD` | float | 50 |
| `KSTRL_MUTATION_TIMEOUT` | float | 0 (no limit) |
| `KSTRL_TIMEOUT_VERIFY` | float | 0 (no limit) |
| `KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE` | bool (`1`) | false |
| `KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS` | int | 3 |
| `KSTRL_VERIFY_PROGRESS_FILE` | path | unset = the progress log beside the component's PRD |
| `KSTRL_VERIFY_FAST_ITERATION_CHECKS` | comma-separated gate names (`test_suite`, `typecheck`, `linter`) | unset or empty = off |

`[verify] mutation_testing` (#391) scores every non-test Python file the diff changed through `mutmut junitxml`, never the text `mutmut results` prints (which carries no killed count under any flag). It now requires `[verify] test_command` to be a single pytest invocation mutmut's `--runner` can wrap - a behaviour change from before #391, when this check ignored `test_command` entirely - and reports `tool_missing` for any command it cannot wrap, the same refusal `[adequacy] diff_mutation` already made. Since the #391 simplify pass on PR #392 (A1/A2) it also shares its mutation cap and its two pre-spend refusals with `[adequacy] diff_mutation`, which runs first and takes what it needs of `[verify] mutation_timeout` before this check gets what is left - see that key's own paragraph for the full arithmetic.

## FixturesConfig (`[fixtures]`)

Phase 1 approved-fixtures oracle (R7.2). Off by default: fixtures execute PRD-supplied commands and import PRD-named modules, so the operator must opt in explicitly.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_FIXTURES_ENABLED` | bool | false |
| `KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS` | bool | true |
| `KSTRL_FIXTURES_SNAPSHOT_DIR` | path | `.kstrl/snapshots` (relative = against the repo root) |
| `KSTRL_FIXTURES_TIMEOUT` | float | 30 |

## PolicyConfig (`[policy]`)

Phase 1 policy envelope (R8.1): declarative merge guardrails enforced on artifacts (git diff, `uv.lock`), never agent self-report. Opt-in; when enabled a violation blocks the merge. List fields (`paths_deny`, `secret_patterns`, `enforcement_paths_extra`, `license_allow`, `license_deny_partial`) are toml-only. Set a numeric cap negative to disable it.

Two invariants worth knowing: modifying **enforcement machinery** (the policy file, CI workflows, or the kstrl verifier code) is a non-overridable halt that no config can disable - `enforcement_paths_extra` only ADDS to that set. And every knob that can change a verdict is a `PolicyConfig` field, so it is covered by the `policy_hash` recorded in the run manifest; the env vars below resolve into those fields before the hash is computed.

The license gate resolves a new dependency's SPDX license from uv's cache, then PyPI. When no source resolves it, `license_unresolved` decides: `block` (default, fail-closed) or `advisory`.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_POLICY_ENABLED` | bool (`1`) | false |
| `KSTRL_POLICY_MAX_FILES` | int | 40 |
| `KSTRL_POLICY_MAX_LINES` | int | 1500 |
| `KSTRL_POLICY_DEPS_ALLOW_NEW` | bool (`1`) | false |
| `KSTRL_POLICY_LICENSE_NET` | bool (`0` = uv cache only) | true (uv cache + PyPI) |
| `KSTRL_POLICY_LICENSE_UNRESOLVED` | `block` \| `advisory` | `block` |
| `KSTRL_POLICY_DEPLOY` | bool (`1`) | false (reserved for R8.7) |

## AutonomyConfig (`[autonomy]`)

Autonomy ladder (R8.2): one ordered level (L1-L4) replaces the scatter of independent autonomy flags. The level lives in `.kstrl/autonomy.json` (not in config) and derives a flag bundle at run start; a config flag that contradicts the bundle is logged as a manual override and the bundle wins. Opt-in, because L1 is *stricter* than the harness defaults - it forces the merge gate on.

Promotion requires evidence **and** a recorded human ack (`ks autonomy promote --actor <you> --ack <why>`); demotion is automatic and immediate, followed by a cool-down before re-promotion. Every entry threshold is an **unmeasured placeholder** until `ks autonomy replay` is run against real history and the result recorded in `docs/dark-factory-roadmap.md`.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_AUTONOMY_ENABLED` | bool (`1`) | false |
| `KSTRL_AUTONOMY_MAX_LEVEL` | int (1-4) | 4 |
| `KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION` | bool (`1`) | false |
| `KSTRL_AUTONOMY_DEMOTE_ON_HEALTH` | bool (`1`) | false |

Both demotion switches are advisory first: a calibration regression or a health
control-limit breach always opens an inbox item while the ladder is enabled, and
revokes a level only when the matching switch is on. They default off because
every ladder threshold is still an unmeasured placeholder.

## InboxConfig (`[inbox]`)

Exception inbox (R8.3): one surface for everything awaiting a human - policy exceptions (R8.1), halted runs, unconfirmed merges, budget overruns, and autonomy demotions (R8.2). On by default, because recording an exception changes no behaviour and an inbox that is off silently loses the record of decisions you still had to make.

Items are append-only in `.kstrl/inbox.jsonl` and actioned with `ks inbox approve|reject|snooze|retry`. Notifications are one-way (kstrl runs no inbound HTTP surface); only action-required kinds and demotions notify, so success stays silent.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_INBOX_ENABLED` | bool (`1`) | true |
| `KSTRL_INBOX_OPEN_CAP` | int (0 = unbounded) | 50 |
| `KSTRL_INBOX_SNOOZE_HOURS` | float | 24.0 |
| `KSTRL_INBOX_NOTIFY` | bool (`1`) | true |

`KSTRL_INBOX_NOTIFY` gates whether an item is *offered* to the notifier at all; the push itself only happens if `[notify].on_inbox_item` is set. Both are required, so the default is silent.

Push notifications reuse the existing `[notify]` machinery rather than adding a service, but get their own command. An ntfy.sh example (self-hostable, priority tiers, no inbound surface on your side):

```toml
[notify]
on_inbox_item = "curl -fsS -H 'Priority: high' -d \"$KSTRL_NOTIFY_EVENT $KSTRL_NOTIFY_COMPONENT\" https://ntfy.sh/your-topic"
```

`KSTRL_NOTIFY_EVENT` arrives as `inbox_<kind>` (for example `inbox_merge_gate`), so one command can route by kind. It is a separate key from `on_first_failure` on purpose: a failing component fires the failure hook and raises an inbox item for the same event, and one event must not page twice.

Then triage with `ks inbox ls`. Notifications never carry an action link: decisions happen locally, which is what keeps kstrl free of an inbound HTTP endpoint.

## AdequacyConfig (`[adequacy]`)

Test-suite adequacy gate (R8.5), **Layer 0 and Layer 1 so far**. Layer 0 reads the diff and the changed test files - no test execution, no coverage run, no mutation tooling, no historical data. It catches two things: a diff that WEAKENS the suite (deleted tests, up to and including a deleted test FILE; added `skip`/`xfail`, whether as a decorator, a `pytest.skip()` in a body, a module-level `pytestmark`, or `marks=` inside `pytest.param`; more assertion lines removed than added) and new tests that assert nothing falsifiable.

"Falsifiable" is a deliberately low bar: a comparison against an expected value, or an asserted exception. Shape-only checks like `assert result is not None` are counted as weak because they pass for a plausible-looking wrong answer, which is the agent-written-test failure mode the layer exists for. So is truthiness however it is spelled - `assert bool(x)`, `assert compute()` and `assert a is not None or a == 3` are all weak - while a call whose arguments state an expectation (`assert all(x > 0 for x in xs)`) is strong. `unittest` and `mock` assertion methods count as assertions: `assertEqual` / `assert_called_once_with` strong, `assertTrue` / `assert_called` weak, so a `TestCase` file is not misread as asserting nothing. It does **not** judge whether an expected value is correct - nothing static can; that is the fixtures oracle's job.

`require_strong_oracle` is a rule about **new** test files (git status `A`). Editing a file whose tests predate the gate never trips it; what the diff adds to that file still does, and every diff-discipline check applies to every changed test file.

**Measured false-positive profile** (kstrl's own suite, ~60 test files, at the head of PR #178): **one** file is flagged - `tests/test_tui_snapshots.py`, whose only oracle is `assert snap_compare(...)`. A custom assertion helper that returns a bool is indistinguishable, statically, from `assert flag_set(0)`, so it reads as weak. The same applies to value-constraining predicates like `assert s.startswith("x")` and `assert re.match(...)`, though neither occurs as a file's sole oracle in this repo. Since one strong test carries the whole file and the floor applies only to NEWLY ADDED files, the rate is low - but it is a real class, and a repo whose tests lean on custom assertion helpers should expect it before switching `layer0` to `block`.

Opt-in and **advisory first**: findings are recorded without failing, so turning it up later starts from evidence rather than a guess. With `[autonomy]` enabled, Layer 0 blocks from L1 up - autonomy may tighten this gate, never loosen it. Findings reach the component's finding stream (PR body, journal, evolution) either way. A **blocking** finding additionally opens an R8.3 inbox item (kind `test_adequacy`, deduped by category and location so a repeat collapses onto one item); an advisory finding does not, because the inbox is a queue of decisions and an advisory asks for none.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_ADEQUACY_ENABLED` | bool (`1`) | false |
| `KSTRL_ADEQUACY_LAYER0` | `advisory` \| `block` | `advisory` |

`[adequacy] patch_coverage` (#152) is Layer 1: an opt-in, toml-only key (no env var, matching `require_strong_oracle` and `flag_assertionless_tests`), off by default. On, and only when `enabled` is also true, it runs the project's own test command a SECOND time under `--cov=. --cov-report=` (data only, no report), then a third spawn (`coverage json --include=<changed files>`) turns that data into a report narrowed to the diff, and reports what fraction of the lines this diff ADDED to non-test Python files the suite executed - patch coverage, restricted to changed non-test lines rather than the whole file or run. Advisory always: there is no floor key, nothing blocks, and the finding is emitted at every percentage including 100%, because the distribution a floor will later be set from is the point of shipping this now. It costs a second full test run either way: measured on kstrl's own suite (6844 tests) against the single-spawn `--cov-report=json:<tmp>` design this replaced, the baseline run was 457.04s and the same run under coverage was 536.79s (993.83s total, 2.17x); the two-spawn split measured here costs no more per file (`tests/test_atomicio.py` alone: 8.17s / 448MB for the old single JSON-report spawn vs 2.31s / 157MB + 0.10s / 34MB for the two new ones), so the full-suite total is expected at or below the figure above, not re-measured end to end.

`[adequacy] diff_mutation` (#152) is Layer 2: an opt-in, toml-only key, off by default, and REFUSED at config load unless `patch_coverage` is also `true` - Layer 2 mutates only the lines Layer 1 measured as changed AND covered, and runs no coverage pass of its own. On, it hands mutmut a synthetic patch naming exactly that line set (`--use-patch-file`, since `--use-coverage` and `--use-patch-file` cannot be combined and `--use-coverage` would need a `.coverage` file Layer 1's own D3 refuses to write into the project tree), then filters the reported mutants back to the same set before scoring, so mutmut's own selection is never trusted for the number. At most one mutant counts per line - the lowest-id mutant with a killed-or-survived status. The mutation SPAWN itself draws from `[verify] mutation_timeout` (default: no limit; the arithmetic below applies when it is set) as ONE PHASE-LEVEL BUDGET shared with `[verify] mutation_testing`, not two independent copies of that number (#391 simplify pass on PR #392, A2): Layer 2 runs FIRST (`run_mechanical_verification` calls `_diff_mutation_checks` before `_mutation_checks`) and is bounded by the FULL configured value; Layer 1 then gets whatever that call's own wall clock actually left of it. Both checks now reach mutmut through the same driver (#391), differing only in the target selector they pass it (Layer 1: every changed non-test file; Layer 2: the synthetic patch above) and, since A2, in how much of the shared cap each gets. On a run that FINISHES, the check's own real wall-clock ceiling is higher than its share of that number: `run_scrubbed`'s timeout path costs the cap plus up to two `_SCRUB_TERM_GRACE_SECONDS` (5s each, SIGTERM then SIGKILL) on the mutation spawn, plus the fixed `_MUTATION_REPORT_TIMEOUT` (30s) and its own grace for the report spawn afterward - about 650s total for Layer 2 at a 600s cap, an 8% overrun (#152 simplify pass, A4; `check_diff_mutation`'s own docstring already stated this, this file previously did not). Both checks also refuse before spending anything, symmetrically since A2 (Layer 1's guard is the byte-for-byte twin of Layer 2's, `_mutation_checks` docstring): when the Phase 1 `test_suite` check already failed (mutmut's own baseline run would only run the suite a third time to abort), and when Layer 1's own coverage-run duration already meets or exceeds what remains of the shared cap (mutmut always pays that same suite's baseline in full before mutating a single line). Measured on kstrl's own suite (533.15s, re-measured against the repo's own recorded 457.04s) at a 600s cap: the baseline alone is already 89% of the budget, so the FIRST surviving mutant - which runs the suite to completion rather than exiting early - guarantees the cap fires, and this gate can only ever produce a SAMPLED score here, never a complete one; the pre-spend refusal above does not fire only because 533s is still (barely) under 600s. A cap that FIRES is now, always, a `timed_out` sidecar with no row (#391, D4): measured, mutmut 2.5.1's junitxml cannot read a truncated cache - it raises `ValueError: Obtained null mutant` under `--untested-policy=error` (the policy this driver always passes), and under any other policy an un-run mutant renders exactly like a killed one, so no safe read of a truncated run exists. `sampled` therefore now has ONE cause, not two: fewer target lines reached a definite status than mutmut reported a mutant for, on a run that otherwise completed. Surviving lines are recorded as `path:line` in the finding and the check's details, as concrete test targets - feeding them into an automatic remediation iteration is not built. Advisory always: no floor key, nothing blocks, no autonomy level reads it. It rewrites the source files it mutates (restored from the `.bak` mutmut itself writes, after every run including a timed-out one) and so, unlike Layer 1, does NOT run under `ks check` or any other `read_only` verification.

Layer 3 (fixtures required at L3+) is not built; see `docs/dark-factory-roadmap.md` for why it waits on measured thresholds.

## DivergenceConfig (`[divergence]`)

Across-attempt divergence detector (#265). `NoProgressBreaker` (`[breaker]`) watches one engineer loop and halts when consecutive iterations change nothing; this watches the retry loop ABOVE it and halts on the mirror-image failure, where every attempt changes a great deal and none of it helps. The review fails, the engineer answers the findings by writing more code, the change gets larger, the reviewer comes back no happier, and the next retry costs another full engineer run.

The predicate needs no threshold on size. Over the last `growth_steps + 1` **consecutive** attempts in which the reviewer ran and failed the component, it fires when the change got larger at every step AND not one of the reviewer's blocking findings was retired at any step. So a trip says something narrow and strong: the change keeps growing and nothing the reviewer objected to has gone away.

The retirement half is deliberately weak, because of what a reviewer does on a changed diff: it raises something new almost every time. A stricter test ("the new finding set is a proper subset of the old one") reads the ordinary converging trajectory as failure - retire A, keep B, draw C; retire B, keep C, draw D - and would condemn exactly the component that was working. Identity rather than count, because a count cannot tell a genuinely retired finding from a new one that replaced it.

Size is lines changed against the base (`git diff --numstat`), deliberately not hunk or diff-chunk size: #266 proposes dropping the pasted diff entirely, and a detector built on chunking would then measure a quantity nothing computes. It is counted through `policy.count_diff_size`, the same helper as the R8.1 size caps, so the two agree and the detector inherits their exclusion of machine-generated lockfiles - without which a dependency bump could supply the size half of a trip on its own.

`lines_changed` is git's own sense of the phrase, **lines added plus lines removed**, the quantity `[policy] max_lines_changed` caps. It is churn, not file growth: deleting 300 pre-existing lines and writing 300 better ones raises it by 600. That is intended, because a component that keeps rewriting one region without answering a single objection is diverging exactly as much as one that keeps appending, but it means the number is never a claim that the artifact got bigger. Files touched is recorded as operator evidence and is not part of the predicate.

**Which way the identity heuristic fails.** The size half is exact. The retirement half reconstructs a finding's identity from what the reviewer wrote: story id plus criterion text for a criterion, category plus file plus explanation for a concern. The weak reset buys one saving property: a trip requires that EVERY previously-blocking key still be present, so any instability in a key (a reworded criterion, a moved line, a rephrased explanation) makes an old key vanish, which counts as a retirement and resets the streak. **Key instability can therefore only produce misses, never false trips.**

**What it would not have caught.** #265 motivated the detector; the shipped predicate would not have fired on that run, and saying otherwise would be inventing a result. The run went 6 blocking findings, then 1, then 10. Attempt 2 retired at least five of attempt 1's six whatever their identities were, so the streak resets there and one bad step afterwards is not two. Checked by exhaustive search rather than argued: over all 128,128 trajectories of shape 6 to 1 to 10 across a 16-key universe, the predicate trips on none. That is the deliberate price of not condemning the converging trajectory above.

**Advisory by default.** `mode = "advisory"` records the trip as a finding and a `review_divergence` event and keeps retrying; `mode = "block"` fails the component instead of paying for another engineer run; `mode = "skip"` does not measure at all. The honest cost of the default is that it saves no money on the run it fires on, and the money was the point of the issue. It ships that way because of the paragraph above: a gate that cannot be shown to fire on the run it was built from, whose retirement half is a heuristic with no measured false-positive rate, has not earned the right to end a component. `docs/loop-design.md` sets the rule - a gate graduates to blocking when the operator has seen its output on real runs and can name what it caught and what it flagged wrongly - and advisory mode is what produces that evidence.

Where to read that evidence: the `review_divergence` event in `.kstrl/runs/<run_id>/events.jsonl` carries the per-attempt series and a `blocked` flag; the finding reaches `.kstrl/evolution.jsonl` as `findings_superseded` when the attempt is retried, so it survives a component that later passes; and the message is printed as a warning line. It never reaches the PR body, and cannot: it is only ever recorded on a failing attempt, and `begin_attempt` clears the finding stream before the passing attempt that builds the PR.

Unlike `[adequacy] layer0` and `[factory] claim_agreement`, the autonomy ladder deliberately does not harden this gate at L1 and above. Those gates ask whether an independent check confirmed a claim, and a run spending less human attention should insist on that harder. This one forecasts, from a heuristic with no measured false-positive rate, that further retries are not worth buying, and auto-hardening it at exactly the levels where nobody is watching is how an unattended run loses components to a gate whose output no operator has read.

Under `mode = "block"` a trip routes through `FailureAction.FAIL` and opens an R8.3 inbox item through the generic halted-run path. Unlike the other FAIL sites, which are proofs (an adversarial budget only shrinks, so retrying provably cannot recover it), this one is a forecast. With the default `[factory] max_retries = 3` it forecloses exactly one remaining attempt, and `ks retry` starts a fresh run with an empty reading history, so an operator who disagrees pays one command. `docs/runbook.md` carries the triage entry.

`growth_steps` must be >= 1; a non-positive value is rejected at load rather than quietly disabling the gate, because `mode = "skip"` is the way to turn it off. The default of 2 is a **structural minimum, not a measured number**, and is recorded as unmeasured. One step is the ordinary shape of a converging retry (a finding is answered by writing code, and the new code draws a finding of its own), so a single step cannot tell a trend from a step; two consecutive steps is the smallest window in which "monotonic" carries information beyond "changed".

Every "cannot tell" path declines to record a reading rather than guessing one - a crashed reviewer, a failed `git diff --numstat`, a failure whose blocking findings cannot be keyed. The predicate needs consecutive attempts, so a missing reading breaks the streak by itself and the loop keeps its retries.

In `single_pr` mode every component shares one branch, so the reported numbers include components that already landed. The predicate survives it (`max_parallel` is forced to 1 there, so the offset is constant across one component's attempts and strict inequality is offset-invariant), but the numbers in the message are the branch's rather than the component's.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_DIVERGENCE_MODE` | `skip` \| `advisory` \| `block` | `advisory` |
| `KSTRL_DIVERGENCE_GROWTH_STEPS` | int (>= 1) | 2 |

## ContractConfig (`[contract]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_CONTRACT_MODE` | str | `tier` (`tier\|final\|skip`) |
| `KSTRL_CONTRACT_TEST_CMD` | str | `uv run pytest` |
| `KSTRL_TIMEOUT_CONTRACT` | float | 0 (no limit) |

Invalid mode raises ValueError (Phase B8).

## SecurityConfig (`[security]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_SECURITY_MODE` | str | `skip` (`skip\|advisory\|hard`) |
| `KSTRL_SECURITY_AGENT_CMD` | str | unset |
| `KSTRL_SECURITY_AGENT_TYPE` | str | unset |
| `KSTRL_SECURITY_MODEL` | str | unset |
| `KSTRL_SECURITY_TIMEOUT` | float | 0 (no limit) |
| `KSTRL_SECURITY_FAIL_THRESHOLD` | str | `high` (`critical\|high\|medium\|low`) |

Invalid mode or threshold raises ValueError (Phase B8). The default mode is `skip` everywhere (dataclass, env, CLI); enable the pass with `advisory` or `hard`.

## KnowledgeConfig (`[knowledge]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_KNOWLEDGE_ENABLED` | bool (`1`/`true`) | true |
| `KSTRL_KNOWLEDGE_MAX_CORE_TOKENS` | int | 2000 |
| `KSTRL_KNOWLEDGE_MAX_DEPENDENCY_TOKENS` | int | 1000 |
| `KSTRL_KNOWLEDGE_MAX_SIBLING_TOKENS` | int | 500 |
| `KSTRL_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS` | float | 0 (no limit) |
| `KSTRL_KNOWLEDGE_DISTILL_MODEL` | str | falls back to `MODEL` |
| `KSTRL_KNOWLEDGE_MAX_FACTS_PER_DISTILL` | int | 7 |
| `KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE` | str | `direct` (`direct\|transitive`) |

`dependency_scope` (E8) controls whether the full-text "Dependencies" tier in `build_knowledge_context` surfaces only direct manifest dependencies (`direct`, default) or the transitive closure (`transitive`). Transitive deps excluded from the full-text tier still appear in the sibling first-sentence summary tier - downgraded, not hidden. Invalid values raise ValueError.

## CodebaseScanConfig (`[codebase_scan]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_CODEBASE_SCAN_ENABLED` | bool | true |
| `KSTRL_CODEBASE_SCAN_MODULE_MAP` | bool | true |
| `KSTRL_CODEBASE_SCAN_PUBLIC_INTERFACES` | bool | true |
| `KSTRL_CODEBASE_SCAN_DEPENDENCY_GRAPH` | bool | true |
| `KSTRL_CODEBASE_SCAN_CONVENTIONS` | bool | true |
| `KSTRL_CODEBASE_SCAN_MAX_TOKENS` | int | 4000 |

## EvolutionConfig (`[evolution]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_EVOLUTION_ENABLED` | bool | true |
| `KSTRL_EVOLUTION_JOURNAL_PATH` | path | `.kstrl/evolution.jsonl` |
| `KSTRL_EVOLUTION_LOOKBACK_RUNS` | int | 10 |

## NotifyConfig (`[notify]`)

Run-milestone shell hooks (R3.2), each condition fired at most once per run. The hook command runs via the shell with `KSTRL_NOTIFY_EVENT` (`run_complete` | `first_failure` | `merge_pending` | `inbox_<kind>`), `KSTRL_NOTIFY_RUN_ID`, `KSTRL_NOTIFY_PROJECT`, `KSTRL_NOTIFY_COMPONENT` and `KSTRL_NOTIFY_DETAIL` set in its environment.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_NOTIFY_ON_COMPLETE` | str | unset (hook disabled) |
| `KSTRL_NOTIFY_ON_FIRST_FAILURE` | str | unset (hook disabled) |
| `KSTRL_NOTIFY_ON_INBOX_ITEM` | str | unset (hook disabled) |
| `KSTRL_NOTIFY_HOOK_TIMEOUT` | float | 30 |

`on_inbox_item` (R8.3) fires once per inbox item *kind* raised during a run, and is deliberately NOT a reuse of `on_first_failure`: a failing component fires the failure hook and raises an inbox item for the same event, so one shared command would page twice for one thing. Leave it empty unless you want per-item pushes; see `[inbox]` above for an ntfy.sh example.

## LinearConfig (`[linear]`)

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_LINEAR_ENABLED` | bool | false | |
| `KSTRL_LINEAR_TEAM_ID` | str | empty | Linear team UUID; required when enabled |
| `KSTRL_LINEAR_TOKEN_ENV` | str | `KSTRL_LINEAR_TOKEN` | NAME of the env var holding the token (indirection so the secret itself never appears in config) |
| `KSTRL_LINEAR_TOKEN` | secret | unset | The API key / OAuth token (default token env var; never logged) |
| `KSTRL_LINEAR_AUTH_MODE` | str | `auto` | `auto\|api_key\|oauth`; auto sniffs the `lin_api_` key prefix |
| `KSTRL_LINEAR_API_URL` | str | `https://api.linear.app/graphql` | |
| `KSTRL_LINEAR_DRY_RUN` | bool | false | Record mutations instead of sending |
| `KSTRL_LINEAR_TIMEOUT` | float | 30 | Per-request timeout (seconds) |
| `KSTRL_LINEAR_MIN_INTERVAL` | float | 0.5 | Client-side throttle between requests |

## QueueConfig (`[queue]`)

The work queue `ks queue` manages and `ks serve` drains (R8.6).

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_QUEUE_MAX_ATTEMPTS` | int | 3 | Execution attempts per item before it is poisoned; must be >= 1 |
| `KSTRL_QUEUE_LEASE_TTL` | float | 3600.0 | Seconds a claim stays valid without a heartbeat; the reaper recovers anything older |

## ServeConfig (`[serve]`)

The continuous-intake daemon (R8.6). Booleans here are true only for `1`.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_SERVE_POLL_INTERVAL` | float | 60.0 | Seconds between poll cycles |
| `KSTRL_SERVE_DAILY_BUDGET_USD` | float | 0.0 (no limit) | Unattended spend per day; any positive value is a hard stop, and it counts only cost an adapter reports |
| `KSTRL_SERVE_MAX_CONSECUTIVE_POISON` | int | 3 | Poisoned items in a row before the queue pauses |
| `KSTRL_SERVE_CAFFEINATE` | bool (`1`) | true | Hold `caffeinate -i` for each run (macOS) |
| `KSTRL_SERVE_FACTORY_TIMEOUT` | float | 0.0 (no limit) | Seconds before a run is killed |
| `KSTRL_SERVE_ALLOW_UNCOVERED_COST` | bool (`1`) | false | Run unattended even when no adapter reports cost, so the budget cannot be enforced |
| `KSTRL_SERVE_MAX_OPEN_PRS` | int | 1 | Scheduled admission stops while this many kstrl PRs are open; 0 = no limit |
| `KSTRL_SERVE_REQUIRE_TIMEOUT` | bool (`1`) | unset | Set by a scheduled LaunchAgent, not by hand: `ks serve` then refuses to start while `factory_timeout_seconds` is 0 |

## GitHubIntakeConfig (`[intake_github]`)

GitHub Issues as the remote inbox (R8.6). Off by default. Booleans here are true only for `1`.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_INTAKE_GITHUB_ENABLED` | bool (`1`) | false | |
| `KSTRL_INTAKE_GITHUB_REPO` | str | empty | `owner/name`; empty resolves from the checkout |
| `KSTRL_INTAKE_GITHUB_QUEUED_LABEL` | str | `kstrl:queued` | The label that authorizes work |
| `KSTRL_INTAKE_GITHUB_LABEL_PREFIX` | str | `kstrl:` | Prefix of the state labels written back |
| `KSTRL_INTAKE_GITHUB_MAX_ITEMS` | int | 5 | Items admitted per sync |
| `KSTRL_INTAKE_GITHUB_PRIORITY` | int | 0 | Queue priority of an admitted item |
| `KSTRL_INTAKE_GITHUB_COMMENT` | bool (`1`) | true | Post the verdict back to the source issue |
| `KSTRL_INTAKE_GITHUB_DRY_RUN` | bool (`1`) | false | Poll and log, send no writebacks |
| `KSTRL_INTAKE_GITHUB_TIMEOUT` | float | 60.0 | Seconds per `gh` call |
| `KSTRL_INTAKE_GITHUB_ALLOWED_ACTORS` | comma-list | empty | Logins allowed to apply the trigger label; empty = anyone who can label |
| `KSTRL_INTAKE_GITHUB_STEER_ENABLED` | bool (`1`) | false | Act on `/memory` and `/iterate` comments on open kstrl PRs |

## SignalsConfig (`[signals]`)

The runtime signal poller (R8.8). It records and classifies; it queues nothing.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_SIGNALS_ENABLED` | bool | false | |
| `KSTRL_SIGNALS_PRODUCT` | str | empty | The product name recorded on every ledger row |
| `KSTRL_SIGNALS_BASE_URL` | str | `http://127.0.0.1:8000` | The tracker's root URL |
| `KSTRL_SIGNALS_PROJECT_ID` | str | empty | The tracker's project id |
| `KSTRL_SIGNALS_TOKEN_ENV` | str | `KSTRL_SIGNALS_TOKEN` | NAME of the env var holding the tracker's bearer token |
| `KSTRL_SIGNALS_TOKEN` | secret | unset | The bearer token, read from the variable `token_env` names |
| `KSTRL_SIGNALS_HTTP_TIMEOUT` | float | 10.0 | Per-request timeout (seconds) |
| `KSTRL_SIGNALS_NEW_ISSUE_EVENTS` | int | 3 | Advisory threshold: labels a new issue, gates nothing |
| `KSTRL_SIGNALS_REPEAT_GROWTH_EVENTS` | int | 10 | Advisory threshold: labels a repeat, gates nothing |

## LearningConfig (`[learning]`)

The global playbook opt-out (#217). An unreadable `kstrl.toml` makes `contribute` false for that run whatever these say.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_LEARNING_CONTRIBUTE` | bool | true | Append this project's lessons to the global playbook |
| `KSTRL_LEARNING_CONSUME` | bool | true | Read global playbook lessons into this project's prompts |

## Calibration

| Env var | Default | Notes |
|---|---|---|
| `KSTRL_RUN_CALIBRATION` | unset | Set to `1` to enable real-LLM calibration tests under `tests/test_calibration.py` |
| `KSTRL_CALIBRATION_MODEL` | `haiku` | Fast model used by the calibration suite. Changing it triggers the R5.5 model-drift warning until a fresh baseline is captured (H2-extended) |
| `KSTRL_CALIBRATION_RUNS` | `3` | Runs per fixture (R5.1). The suite gates on majority-of-runs consistency; use `1` for a cheap smoke, keep `3` for baseline capture |
| `KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE` | unset | Agent type for the reviewer and security calibration agents (R7.1), so the suite can measure a reviewer from the other model family; the architect keeps the base calibration agent |
| `KSTRL_CALIBRATION_REVIEWER_MODEL` | unset | Model for those two agents, set with the agent type above |

## Patterns

- Boolean env vars accept `1`, `true`, `yes` (case-insensitive). Anything else is false.
- Path env vars are resolved against the factory's `root_dir`, not the process cwd. If absolute, used as-is.
- Enum env vars (`KSTRL_SECURITY_MODE`, `KSTRL_CONTRACT_MODE`, `KSTRL_SECURITY_FAIL_THRESHOLD`) validate in `__post_init__`. A typo raises ValueError at startup rather than silently defaulting.
