# Ralph Environment Variables Reference

Every config dataclass has a `from_env()` classmethod that reads env vars, and a `load(root_dir)` classmethod that overlays env on top of `ralph.toml` (env wins). This doc enumerates every variable the harness consults.

Precedence: **CLI flag > env var > `ralph.toml` > dataclass default**.

## Global / RalphConfig (`[agent]`, `[run]`, `[paths]`, `[git]`, `[ui]`)

| Env var | Type | Default | Notes |
|---|---|---|---|
| `MAX_ITERATIONS` | int | 10 | Per-component max agent iterations |
| `PROMPT_FILE` | path | `scripts/ralph/prompt.md` | |
| `PRD_FILE` | path | `scripts/ralph/prd.json` | |
| `PROGRESS_FILE` | path | `scripts/ralph/progress.txt` | |
| `CODEBASE_MAP_FILE` | path | `scripts/ralph/codebase_map.md` | |
| `SLEEP_SECONDS` | float | 2.0 | Inter-iteration sleep |
| `INTERACTIVE` | bool | false | Pause between iterations for human input |
| `ALLOWED_PATHS` | comma-list | empty | Restrict agent writes to these prefixes |
| `RALPH_BRANCH` | str | unset | Override branch checkout; `""` means skip checkout |
| `RALPH_AUTO_CHECKOUT` | bool | true | When false, run loop skips branch resolution |
| `AGENT_CMD` | str | unset | Custom shell command for the agent (overrides type) |
| `MODEL` | str | unset | Model name passed to the agent |
| `MODEL_REASONING_EFFORT` | str | unset | `low\|medium\|high\|max` |
| `RALPH_AGENT_TYPE` | str | unset | `claude-code\|codex\|auto` |
| `RALPH_TIMEOUT_AGENT_ITERATION` | float | 1800 | Per-agent.run() timeout (seconds) |
| `RALPH_TIMEOUT_COMPONENT` | float | 7200 | Per-component total timeout |
| `RALPH_TIMEOUT_DEFAULT` | float | 60 | Generic subprocess timeout |
| `RALPH_UI` | str | auto | `auto\|rich\|plain` |
| `NO_COLOR` | bool flag | false | Disables colors |
| `RALPH_ASCII` | bool | false | ASCII-only UI |

## FactoryConfig (`[factory]`)

| Env var | Type | Default |
|---|---|---|
| `FACTORY_MAX_PARALLEL` | int | 4 |
| `FACTORY_MAX_RETRIES` | int | 3 |
| `FACTORY_RETRY_DELAY` | float | 5.0 |

Plus the new `max_adversarial_calls` (E4) and `pause_before_pr_merge` (E6) which are configured via CLI / programmatic construction; no env var today.

## VerifyConfig (`[verify]`)

| Env var | Type | Default |
|---|---|---|
| `RALPH_VERIFY_TEST_CMD` | str | unset (uses `uv run pytest`) |
| `RALPH_VERIFY_TYPECHECK_CMD` | str | unset (uses `uv run mypy .`) |
| `RALPH_VERIFY_LINT_CMD` | str | unset (uses `uv run ruff check .`) |
| `RALPH_DEAD_CODE_CLEANUP` | bool (`1`) | false |
| `RALPH_DEAD_CODE_CMD` | str | unset |
| `RALPH_MUTATION_TESTING` | bool (`1`) | false |
| `RALPH_MUTATION_THRESHOLD` | float | 50 |
| `RALPH_MUTATION_TIMEOUT` | float | 600 |
| `RALPH_TIMEOUT_VERIFY` | float | 300 |
| `RALPH_VERIFY_REQUIRE_SELF_CRITIQUE` | bool (`1`) | false |
| `RALPH_VERIFY_SELF_CRITIQUE_MIN_BULLETS` | int | 3 |
| `RALPH_VERIFY_PROGRESS_FILE` | path | `scripts/ralph/progress.txt` |

## ContractConfig (`[contract]`)

| Env var | Type | Default |
|---|---|---|
| `RALPH_CONTRACT_MODE` | str | `tier` (`tier\|final\|skip`) |
| `RALPH_CONTRACT_TEST_CMD` | str | `uv run pytest` |
| `RALPH_TIMEOUT_CONTRACT` | float | 600 |

Invalid mode raises ValueError (Phase B8).

## SecurityConfig (`[security]`)

| Env var | Type | Default |
|---|---|---|
| `RALPH_SECURITY_MODE` | str | `advisory` (`skip\|advisory\|hard`) |
| `RALPH_SECURITY_AGENT_CMD` | str | unset |
| `RALPH_SECURITY_AGENT_TYPE` | str | unset |
| `RALPH_SECURITY_MODEL` | str | unset |
| `RALPH_SECURITY_TIMEOUT` | float | 600 |
| `RALPH_SECURITY_FAIL_THRESHOLD` | str | `high` (`critical\|high\|medium\|low`) |

Invalid mode or threshold raises ValueError (Phase B8). Note: the `ralph_py factory` CLI defaults `--security-mode` to `skip` for backward compat; the dataclass default of `advisory` only applies to programmatic construction.

## KnowledgeConfig (`[knowledge]`)

| Env var | Type | Default |
|---|---|---|
| `RALPH_KNOWLEDGE_ENABLED` | bool (`1`/`true`) | true |
| `RALPH_KNOWLEDGE_MAX_CORE_TOKENS` | int | 2000 |
| `RALPH_KNOWLEDGE_MAX_DEPENDENCY_TOKENS` | int | 1000 |
| `RALPH_KNOWLEDGE_MAX_SIBLING_TOKENS` | int | 500 |
| `RALPH_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS` | float | 300 |
| `RALPH_KNOWLEDGE_DISTILL_MODEL` | str | falls back to `MODEL` |
| `RALPH_KNOWLEDGE_MAX_FACTS_PER_DISTILL` | int | 7 |

## FeedforwardConfig (`[feedforward]`)

| Env var | Type | Default |
|---|---|---|
| `RALPH_FEEDFORWARD_ENABLED` | bool | true |
| `RALPH_FEEDFORWARD_MODULE_MAP` | bool | true |
| `RALPH_FEEDFORWARD_PUBLIC_INTERFACES` | bool | true |
| `RALPH_FEEDFORWARD_DEPENDENCY_GRAPH` | bool | true |
| `RALPH_FEEDFORWARD_CONVENTIONS` | bool | true |
| `RALPH_FEEDFORWARD_MAX_TOKENS` | int | 4000 |

## EvolutionConfig (`[evolution]`)

| Env var | Type | Default |
|---|---|---|
| `RALPH_EVOLUTION_ENABLED` | bool | true |
| `RALPH_EVOLUTION_JOURNAL_PATH` | path | `.ralph/evolution.jsonl` |
| `RALPH_EVOLUTION_LOOKBACK_RUNS` | int | 10 |

## Calibration

| Env var | Default | Notes |
|---|---|---|
| `RALPH_RUN_CALIBRATION` | unset | Set to `1` to enable real-LLM calibration tests under `tests/test_calibration.py` |
| `RALPH_CALIBRATION_MODEL` | `haiku` | Fast model used by the calibration suite |

## Patterns

- Boolean env vars accept `1`, `true`, `yes` (case-insensitive). Anything else is false.
- Path env vars are resolved against the factory's `root_dir`, not the process cwd. If absolute, used as-is.
- Enum env vars (`RALPH_SECURITY_MODE`, `RALPH_CONTRACT_MODE`, `RALPH_SECURITY_FAIL_THRESHOLD`) validate in `__post_init__`. A typo raises ValueError at startup rather than silently defaulting.
