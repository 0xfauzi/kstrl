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

Run-milestone shell hooks (R3.2), each fired at most once per run. The hook command runs via the shell with `KSTRL_NOTIFY_EVENT` (`run_complete` | `first_failure` | `merge_pending`), `KSTRL_NOTIFY_RUN_ID`, `KSTRL_NOTIFY_PROJECT`, `KSTRL_NOTIFY_COMPONENT` and `KSTRL_NOTIFY_DETAIL` set in its environment.

| Env var | Type | Default |
|---|---|---|
| `KSTRL_NOTIFY_ON_COMPLETE` | str | unset (hook disabled) |
| `KSTRL_NOTIFY_ON_FIRST_FAILURE` | str | unset (hook disabled) |
| `KSTRL_NOTIFY_HOOK_TIMEOUT` | float | 30 |

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
