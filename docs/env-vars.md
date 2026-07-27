# kstrl Environment Variables Reference

> Rename note (2026-07-20): ``KSTRL_*`` is the primary namespace. The
> legacy ``KSTRL_*`` spelling of every variable below is honored for one
> release with a DeprecationWarning (see ``kstrl/envcompat.py``). Bare
> ``FACTORY_*`` names remain accepted for the factory family.

Every config dataclass has a `from_env()` classmethod that reads env vars, and a `load(root_dir)` classmethod that overlays env on top of `kstrl.toml` (env wins). This doc enumerates every variable the harness consults.

Precedence: **CLI flag > env var > `kstrl.toml` > dataclass default**.

## Global / kstrlConfig (`[agent]`, `[run]`, `[paths]`, `[git]`, `[ui]`)

| Env var | Type | Default | Notes |
|---|---|---|---|
| `MAX_ITERATIONS` | int | 10 | Per-component max agent iterations |
| `PROMPT_FILE` | path | `scripts/kstrl/prompt.md` | |
| `PRD_FILE` | path | `scripts/kstrl/prd.json` | |
| `PROGRESS_FILE` | path | `scripts/kstrl/progress.txt` | |
| `CODEBASE_MAP_FILE` | path | `scripts/kstrl/codebase_map.md` | |
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
| `KSTRL_UI` | str | auto | `auto\|rich\|plain` |
| `KSTRL_NO_TUI` | bool | unset | `1` disables the embedded factory dashboard (plain output) |
| `NO_COLOR` | bool flag | false | Disables colors |
| `KSTRL_ASCII` | bool | false | ASCII-only UI |

## TimeoutConfig (`[timeout]`)

All values are seconds; 0 or less disables that limit.

| Env var | Type | Default | Notes |
|---|---|---|---|
| `KSTRL_TIMEOUT_GIT` | float | 30 | Per git subprocess |
| `KSTRL_TIMEOUT_AGENT_ITERATION` | float | 1800 | One engineer iteration |
| `KSTRL_TIMEOUT_COMPONENT` | float | 7200 | Wall clock per component across iterations |
| `KSTRL_TIMEOUT_VERIFY` | float | 300 | Each Phase 1 check subprocess (also read by `VerifyConfig.subprocess_timeout`) |
| `KSTRL_TIMEOUT_REVIEW` | float | 600 | Phase 2 reviewer call |
| `KSTRL_TIMEOUT_CONTRACT` | float | 600 | Phase 3 contract test run (also read by `ContractConfig.timeout`) |
| `KSTRL_TIMEOUT_DEFAULT` | float | 60 | Any other subprocess |
| `KSTRL_TIMEOUT_BACKSTOP_MARGIN` | float | 60 | Extra slack before the scheduler declares a worker dead |

## FactoryConfig (`[factory]`)

| Env var | Type | Default |
|---|---|---|
| `FACTORY_MAX_PARALLEL` | int | 4 |
| `FACTORY_MAX_RETRIES` | int | 3 |
| `FACTORY_RETRY_DELAY` | float | 5.0 |
| `FACTORY_MERGE_TIMEOUT` | float | 300.0 |
| `KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS` | int | 0 (unbounded) |
| `KSTRL_FACTORY_MAX_TOTAL_TOKENS` | int | 0 (unbounded) |
| `KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE` | bool (`1`/`true`/`yes`) | false |
| `KSTRL_FACTORY_PROGRESS_LOG_ENABLED` | bool | true |
| `KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE` | bool | false |

The two safety knobs (E4 `max_adversarial_calls`, E6 `pause_before_pr_merge`) are reachable via all three surfaces since R2.2: the env vars above, `[factory]` keys in kstrl.toml, and the `--max-adversarial-calls` / `--pause-before-pr-merge` CLI flags.

### What `max_total_tokens` guarantees (R8)

It is a **stop-before-the-next-unit-of-work** limit, not a hard cap. It is
evaluated in two places:

- **Between engineer iterations** (`kstrl/loop.py`, `LoopBudget.halt_reason`).
  The worker is launched with the cap plus the run's spend as of that moment,
  so the loop refuses to start another iteration once the total reaches the
  cap. This is the only check that fires while the spend is being incurred.
- **At phase boundaries** in the parent (`pipeline.process_result`, the review
  / security / distill gates, and the scheduling gate). These stop the next
  phase or the next component.

Either route halts the component loudly and identically: a
`budget_exceeded` event, a typed `infrastructure_error` finding, and exactly
one `budget_overrun` inbox item.

What is **not** bounded:

| Gap | Why |
|---|---|
| The iteration already running | Nothing interrupts a single agent call mid-flight. Overshoot is up to one iteration per running worker; `KSTRL_TIMEOUT_AGENT_ITERATION` bounds that in wall clock, never in tokens |
| Concurrent workers | Each worker sees the run total as of its own launch. With `FACTORY_MAX_PARALLEL = N`, up to N iterations can be in flight past the cap |
| Unreported spend | Every token figure is a CLI self-report. Calls that report nothing count as zero, so totals are lower bounds whenever `unreported_calls > 0` and the halt can arrive late. A loop that reports *nothing* is a separate case and halts outright - see below |

Unknown usage is deliberately **not** silently treated as zero in the one case
where that would make the cap undeliverable. The loop halts with a
`token budget unenforceable` reason when **both** hold:

1. **this engineer loop** has reported no token count on any of its calls, so
   the run total cannot grow while it runs (the spend recorded before this
   worker launched is frozen at launch); **and**
2. the **engineer** has now made two calls that reported no token count -
   counted across the run's engineer loops, so the threshold does not reset on
   every attempt or component, while another role's timed-out call never
   counts (it is no evidence about the engineer's adapter).

Reported *cost* is not token evidence: a result carrying `total_cost_usd` with
no `usage` dict is "known" to the meter but can never move a token total, so
the rule counts token-bearing calls (`token_calls`), not reporting calls.

The threshold counts only the calls that reported **no tokens**, so a lone
unparseable result in an otherwise-reporting run is still treated as an
incident, not a dead adapter. The flip side, on purpose: once a run has
accumulated one tokenless call anywhere, the engineer's first tokenless
iteration reaches the threshold and halts. Two independent tokenless calls in
one run is adapter behavior (a custom `agent_cmd` never reports usage), and a
loud, recoverable halt beats spending under a ceiling that cannot fire.

`KSTRL_AGENT_BUDGET_USD` is the only genuine in-turn ceiling, and only the
`claude-sdk` adapter enforces it; `claude-code`, `codex`, and custom commands
ignore it.

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
| `KSTRL_DEAD_CODE_CLEANUP` | bool (`1`) | false |
| `KSTRL_DEAD_CODE_CMD` | str | unset |
| `KSTRL_MUTATION_TESTING` | bool (`1`) | false |
| `KSTRL_MUTATION_THRESHOLD` | float | 50 |
| `KSTRL_MUTATION_TIMEOUT` | float | 600 |
| `KSTRL_TIMEOUT_VERIFY` | float | 300 |
| `KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE` | bool (`1`) | false |
| `KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS` | int | 3 |
| `KSTRL_VERIFY_PROGRESS_FILE` | path | `scripts/kstrl/progress.txt` |

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

## ContractConfig (`[contract]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_CONTRACT_MODE` | str | `tier` (`tier\|final\|skip`) |
| `KSTRL_CONTRACT_TEST_CMD` | str | `uv run pytest` |
| `KSTRL_TIMEOUT_CONTRACT` | float | 600 |

Invalid mode raises ValueError (Phase B8).

## SecurityConfig (`[security]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_SECURITY_MODE` | str | `skip` (`skip\|advisory\|hard`) |
| `KSTRL_SECURITY_AGENT_CMD` | str | unset |
| `KSTRL_SECURITY_AGENT_TYPE` | str | unset |
| `KSTRL_SECURITY_MODEL` | str | unset |
| `KSTRL_SECURITY_TIMEOUT` | float | 600 |
| `KSTRL_SECURITY_FAIL_THRESHOLD` | str | `high` (`critical\|high\|medium\|low`) |

Invalid mode or threshold raises ValueError (Phase B8). The default mode is `skip` everywhere (dataclass, env, CLI); enable the pass with `advisory` or `hard`.

## KnowledgeConfig (`[knowledge]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_KNOWLEDGE_ENABLED` | bool (`1`/`true`) | true |
| `KSTRL_KNOWLEDGE_MAX_CORE_TOKENS` | int | 2000 |
| `KSTRL_KNOWLEDGE_MAX_DEPENDENCY_TOKENS` | int | 1000 |
| `KSTRL_KNOWLEDGE_MAX_SIBLING_TOKENS` | int | 500 |
| `KSTRL_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS` | float | 300 |
| `KSTRL_KNOWLEDGE_DISTILL_MODEL` | str | falls back to `MODEL` |
| `KSTRL_KNOWLEDGE_MAX_FACTS_PER_DISTILL` | int | 7 |
| `KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE` | str | `direct` (`direct\|transitive`) |

`dependency_scope` (E8) controls whether the full-text "Dependencies" tier in `build_knowledge_context` surfaces only direct manifest dependencies (`direct`, default) or the transitive closure (`transitive`). Transitive deps excluded from the full-text tier still appear in the sibling first-sentence summary tier - downgraded, not hidden. Invalid values raise ValueError.

## FeedforwardConfig (`[feedforward]`)

| Env var | Type | Default |
|---|---|---|
| `KSTRL_FEEDFORWARD_ENABLED` | bool | true |
| `KSTRL_FEEDFORWARD_MODULE_MAP` | bool | true |
| `KSTRL_FEEDFORWARD_PUBLIC_INTERFACES` | bool | true |
| `KSTRL_FEEDFORWARD_DEPENDENCY_GRAPH` | bool | true |
| `KSTRL_FEEDFORWARD_CONVENTIONS` | bool | true |
| `KSTRL_FEEDFORWARD_MAX_TOKENS` | int | 4000 |

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

## Calibration

| Env var | Default | Notes |
|---|---|---|
| `KSTRL_RUN_CALIBRATION` | unset | Set to `1` to enable real-LLM calibration tests under `tests/test_calibration.py` |
| `KSTRL_CALIBRATION_MODEL` | `haiku` | Fast model used by the calibration suite. Changing it triggers the R5.5 model-drift warning until a fresh baseline is captured (H2-extended) |
| `KSTRL_CALIBRATION_RUNS` | `3` | Runs per fixture (R5.1). The suite gates on majority-of-runs consistency; use `1` for a cheap smoke, keep `3` for baseline capture |

## Patterns

- Boolean env vars accept `1`, `true`, `yes` (case-insensitive). Anything else is false.
- Path env vars are resolved against the factory's `root_dir`, not the process cwd. If absolute, used as-is.
- Enum env vars (`KSTRL_SECURITY_MODE`, `KSTRL_CONTRACT_MODE`, `KSTRL_SECURITY_FAIL_THRESHOLD`) validate in `__post_init__`. A typo raises ValueError at startup rather than silently defaulting.
