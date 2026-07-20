# Spec: Harness Engineering for Ralph

> Rename note (2026-07-20): the project was renamed Ralph -> kstrl (package `kstrl`, CLI `ks`, config `kstrl.toml`, state `.kstrl/`, env `KSTRL_*`). Historical entries below keep the names that were current when they were written.

**Status**: Draft
**Date**: 2026-04-09
**Source**: Bockeleer, "Harness Engineering for Coding Agent Users" (martinfowler.com, April 2026); hwchase17/autoresearch-agents; aiming-lab/AutoResearchClaw

---

## 1. Problem statement

Ralph's factory mode has strong feedback sensors (3-phase verification: mechanical, review, contract) but weak feedforward controls. The agent runs, then we check. When it fails, we inject failure context and retry - but we never improve the harness itself between runs. This creates two problems:

1. **Wasted iterations**: The agent frequently makes mistakes that could be prevented by giving it structural information before it starts (type signatures, module boundaries, codebase conventions). Each wasted iteration costs time and tokens.

2. **No learning across runs**: Ralph retries with context but never accumulates reusable knowledge. The same architectural mistakes recur across components and across factory runs. AutoResearchClaw's "evolution directory" and autoresearch-agents' `results.tsv` both demonstrate that structured experiment journals dramatically improve convergence.

Additionally, factory mode is currently a secondary command (`ralph factory`) despite being the more capable execution path. Single-component `ralph run` should be a degenerate case of factory, not the default.

---

## 2. Design principles

These are drawn directly from the article's cybernetics framework:

- **Feedforward + feedback in balance**: "You get either an agent that keeps repeating the same mistakes (feedback-only) or an agent that encodes rules but never finds out whether they worked (feedforward-only)." Ralph today is feedback-dominant.

- **Computational before inferential**: Deterministic checks (tests, linters, type checkers, structural analysis) are cheap, fast, and reliable. LLM-based checks (review, spec compliance) are expensive and non-deterministic. Run computational controls first, always.

- **Keep quality left**: Run cheap checks before expensive ones. Catch errors before they propagate. The current Phase 1 -> Phase 2 -> Phase 3 ordering is correct; we need to add a Phase 0 (feedforward) before the agent even starts.

- **Ashby's Law (variety reduction)**: "An LLM-based coding agent can produce almost anything, but committing to a topology narrows that space, making a comprehensive harness more achievable." Constrain the agent's output space through structural context, not open-ended generation.

- **Leverage existing tools, don't reinvent them**: Ruff, mypy, pytest, eslint already catch problems. Ralph's job is to make their output actionable for agents (structured parsing, source context, convention awareness) - not to build a parallel rule engine.

- **The steering loop improves the harness, not just the output**: When issues recur, the controls themselves should be updated. This is the meta-loop that transforms ralph from a retry system into an adaptive one.

---

## 3. Changes

### 3.1 Factory mode as default

**What**: `ralph run` becomes a thin wrapper that creates a single-component manifest and delegates to factory execution. The `factory` command becomes the primary code path. `ralph run` is preserved as a convenience alias.

**Why**: Factory mode has 3-phase verification, worktree isolation, structured progress logging, and retry-with-context. Single-component mode has none of these. Users currently get the weaker path by default.

**Behavior change**:

```
# Before: ralph run uses loop.py directly, no verification
ralph run 25

# After: ralph run creates a 1-component manifest and runs factory
# with verification enabled by default
ralph run 25
```

**Implementation**:

- `ralph run` synthesizes a `Manifest` with one component from the existing PRD
- Delegates to `run_factory()` with sensible defaults:
  - `max_parallel=1` (single component, no worktree needed)
  - `use_worktrees=False` (runs in place like today)
  - `verify_config=VerifyConfig()` (mechanical checks enabled)
  - `review_mode="advisory"` (warn but don't block for single-component)
  - `contract_config=None` (no contract testing for single component)
  - `create_prs=False` (preserve current behavior - user decides when to PR)
- `ralph run --no-verify` preserves the old behavior (raw loop, no verification)
- `ralph run --legacy` invokes `run_loop()` directly as an escape hatch during migration

**Files to modify**:
- `ralph_py/cli.py`: Rewrite `run` command to build manifest and call `run_factory`
- `ralph_py/factory.py`: Support `use_worktrees=False` with `max_parallel=1` (already works, just needs testing)
- `ralph_py/manifest.py`: Add `Manifest.from_prd(prd_path, branch)` class method for single-component manifests

**Risks**:
- Users relying on `ralph run` completing faster because it skips verification. Mitigation: `--no-verify` flag.
- The factory's `PlainUI` override (line 1335-1336 of cli.py) would suppress rich output for single-component runs. Fix: only force plain UI for parallel workers, not the orchestrator.

---

### 3.2 Feedforward controls (Phase 0)

**What**: A new pre-execution phase that runs before the agent starts each component. Injects structural context, codebase intelligence, and constraint information into the prompt - computationally derived, not LLM-guessed.

**Why**: The article identifies feedforward as the most impactful gap in most coding agent harnesses: "Increase the probability that the agent creates good results in the first attempt." Every feedforward signal that prevents one failure iteration saves 1-30 minutes of agent time.

#### 3.2.1 Structural analysis injection

**What**: Before each component runs, ralph collects codebase structure and injects it into the prompt:

```
=== CODEBASE CONTEXT (auto-generated, do not modify) ===

## Module map
src/
  auth/           # Authentication (4 files, 380 LOC)
  api/            # REST endpoints (12 files, 2100 LOC)
  db/             # Database layer (6 files, 890 LOC)

## Public interfaces (files you will likely need)
src/db/models.py: class User, class Session, class Token
src/api/router.py: def register_routes(app: FastAPI) -> None
src/auth/middleware.py: class AuthMiddleware

## Dependency graph (your component's neighbors)
auth -> db (imports: User, Session)
api -> auth (imports: AuthMiddleware, require_auth)
api -> db (imports: User, get_session)

## Active conventions (from .editorconfig, ruff.toml, pyproject.toml)
- Line length: 88
- Quote style: double
- Import sort: isort-compatible (I sections)
- Type checking: mypy strict mode

=== END CODEBASE CONTEXT ===
```

**How**: Computational (no LLM calls). Use:
- `ast` module to extract public symbols from Python files
- `importlib.metadata` or `pyproject.toml` parsing for project structure
- Existing `codebase_map.md` if available (from `ralph understand`)
- `ruff.toml` / `pyproject.toml` / `.editorconfig` for convention extraction
- Git diff against base branch for "what's already changed" context

**New file**: `ralph_py/feedforward.py`

**Key function**:
```python
def build_feedforward_context(
    worktree_path: Path,
    component: Component,
    manifest: Manifest,
    config: FeedforwardConfig,
) -> str:
    """Build pre-execution context string for prompt injection.
    
    Returns a formatted string to prepend to the agent prompt.
    All analysis is computational (no LLM calls).
    """
```

**Configuration** (`ralph.toml`):
```toml
[feedforward]
enabled = true
module_map = true          # directory tree with LOC counts
public_interfaces = true   # extract public symbols from changed modules
dependency_graph = true    # import-based dependency analysis
conventions = true         # extract from config files
max_context_tokens = 4000  # cap to avoid prompt bloat
```

#### 3.2.2 Why not custom ralph rules?

The article advocates for "custom linter messages that include instructions for self-correction." This is a valid insight, but we don't need a ralph-specific rule engine to achieve it. Ruff, mypy, eslint, and other existing linters already catch the problems. What's missing is making their output actionable for agents.

Ralph addresses this through two mechanisms that already exist in this spec:

1. **Feedforward convention extraction (3.2.1)**: If the agent knows from structural analysis that "this project uses httpx, not requests," it won't make the mistake in the first place. The convention data comes from config files and codebase analysis - no custom rules needed.

2. **LLM-optimized sensor output (3.3)**: When ruff flags S608, ralph parses it into a structured message with the file path, source context, and the relevant convention from feedforward. The agent gets project-specific guidance without ralph reinventing linting.

If this proves insufficient after measuring iteration counts, a lightweight annotation mapping (ruff rule code -> project-specific fix hint) could be added to `ralph.toml` later. But start without it.

#### 3.2.3 Bootstrap scaffolding

**What**: Before the agent starts a component, ralph can optionally run a scaffolding step that creates file stubs, interface definitions, or test harnesses. This constrains the agent's solution space structurally.

**How**: A `scaffold` command or script configured per-component in the manifest:

```json
{
  "id": "auth-service",
  "scaffold": "scripts/ralph/scaffolds/auth-service.sh",
  "userStories": [...]
}
```

The scaffold script runs before the agent and can:
- Create directory structure
- Generate interface stubs from OpenAPI specs
- Copy template files
- Run code generators (protobuf, sqlalchemy models, etc.)

**Configuration**: Optional `scaffold` field on Component (manifest.py). Runs during `_launch_component` in factory.py, after worktree setup but before `_run_component`.

---

### 3.3 LLM-optimized sensor output

**What**: Parse mechanical verification output into structured, actionable context instead of dumping raw stderr. The article emphasizes sensors are "particularly powerful when they produce signals optimized for LLM consumption."

**Current behavior** (context.py `format_for_prompt`):
```
## Verification Failures
- test_suite: FAIL - Tests failed (exit code 1)
  FAILED tests/test_auth.py::test_login - AssertionError
  FAILED tests/test_auth.py::test_signup - TypeError: missing argument
  ... (50 lines of raw pytest output)
```

**New behavior**:
```
## Verification Failures

### test_suite: 2 failures

1. tests/test_auth.py::test_login (line 45)
   AssertionError: expected status 200, got 401
   FIX: The login endpoint at src/api/auth.py:login() is returning 401.
   Check that the password hash comparison uses bcrypt.checkpw().

2. tests/test_auth.py::test_signup (line 72)
   TypeError: create_user() missing required argument 'email'
   FIX: The function signature at src/db/models.py:create_user() expects
   (username, email, password_hash). The test calls it with (username, password_hash).

### typecheck: 1 error

1. src/api/auth.py:23
   error: Argument 1 to "verify_password" has incompatible type "str | None"; expected "str"
   FIX: Add a None check before calling verify_password(), or use
   `password: str = request.password or ""` with appropriate error handling.
```

**Implementation**:

- Parse pytest output into structured failure objects (file, line, assertion, message)
- Parse mypy output into (file, line, error_code, message) tuples
- Parse ruff output into (file, line, rule, message) tuples
- For each failure, attempt to include the relevant source lines (read the file, show context)
- Generate a "FIX:" line using pattern matching on common error types (no LLM needed for common cases)

**Files to modify**:
- `ralph_py/verify.py`: Each `check_*` function returns structured `CheckResult` with parsed details instead of raw output lines
- `ralph_py/context.py`: `format_for_prompt` uses structured data to generate targeted fix suggestions
- New: `ralph_py/parsers.py` for pytest, mypy, ruff output parsing

**Configuration** (`ralph.toml`):
```toml
[sensors]
parse_output = true         # enable structured parsing
include_source_context = true  # include relevant source lines in failure messages
max_failures_per_check = 10    # cap to avoid prompt bloat
```

---

### 3.4 Continuous learning (meta-steering loop)

**What**: After each factory run, ralph analyzes recurring failure patterns and proposes harness improvements. This is the meta-loop: the harness improves itself, not just the code.

**Why**: From the article: "Whenever an issue happens multiple times, the feedforward and feedback controls should be improved to make the issue less probable to occur in the future." From autoresearch-agents: experiment journals (`results.tsv`) that track what worked and what didn't enable the agent to learn which modification types are effective. From AutoResearchClaw: the "evolution directory" extracts structured lessons from each run, and MetaClaw demonstrates +18.3% robustness improvement from cross-run learning.

**Architecture**:

```
Factory Run N
    |
    v
[Execution + 3-Phase Verification]
    |
    v
[Failure Pattern Extraction]  <-- computational, runs post-factory
    |
    v
[Evolution Journal]           <-- persistent, grows across runs
    |
    v
[Harness Improvement Proposals]  <-- inferential (LLM), optional
    |
    v
[Human Review Gate]           <-- proposals shown, human approves/rejects
    |
    v
[Apply to Harness]            <-- update rules.toml, feedforward config, CLAUDE.md
    |
    v
Factory Run N+1               <-- benefits from accumulated improvements
```

#### 3.4.1 Evolution journal

Inspired by AutoResearchClaw's evolution directory. A structured JSONL file that accumulates across factory runs.

**File**: `.ralph/evolution.jsonl`

**Entry schema**:
```json
{
  "timestamp": "2026-04-09T14:30:00Z",
  "run_id": "factory-20260409-143000",
  "project": "auth-service",
  "component_id": "login-endpoint",
  "event_type": "failure_pattern",
  "category": "verification|review|contract|iteration",
  "pattern": {
    "description": "Agent used raw SQL instead of ORM in 3/5 components",
    "frequency": 3,
    "total_components": 5,
    "affected_components": ["login-endpoint", "user-profile", "session-mgmt"],
    "check_name": "linter",
    "error_signature": "S608: possible SQL injection"
  },
  "resolution": {
    "applied": true,
    "type": "feedforward_updated",
    "detail": "Added ORM convention to feedforward context extraction"
  }
}
```

**New file**: `ralph_py/evolution.py`

**Key functions**:
```python
class EvolutionJournal:
    """Persistent learning journal across factory runs."""
    
    def __init__(self, path: Path): ...
    
    def record_run(self, manifest: Manifest, factory_result: FactoryResult) -> None:
        """Extract and record patterns from a completed factory run."""
    
    def extract_failure_patterns(
        self, manifest: Manifest, min_frequency: int = 2
    ) -> list[FailurePattern]:
        """Identify recurring failures across components."""
    
    def get_recurring_patterns(
        self, lookback_runs: int = 10
    ) -> list[FailurePattern]:
        """Get patterns that recur across multiple factory runs."""
    
    def propose_harness_improvements(
        self, patterns: list[FailurePattern]
    ) -> list[HarnessProposal]:
        """Generate concrete harness improvement proposals.
        
        Computational proposals (new rules, config changes) are generated
        deterministically. Complex proposals (CLAUDE.md updates, prompt
        changes) use an LLM call.
        """
```

#### 3.4.2 Experiment tracking

Inspired by autoresearch-agents' `results.tsv`. Each factory run produces a structured result entry that enables trend analysis.

**File**: `.ralph/experiments.tsv`

**Columns**:
```
run_id | timestamp | project | components_total | completed | failed | skipped |
avg_iterations | avg_duration_s | p1_fail_rate | p2_fail_rate | p3_fail_rate |
retry_rate | common_failure | harness_changes_applied
```

This is a flat, greppable file that answers: "Are we getting better over time?" If retry rate is declining across runs, the harness is working. If it's flat or increasing, the meta-loop isn't catching the right patterns.

#### 3.4.3 Automatic harness proposals

After extracting patterns, ralph proposes concrete changes:

**Computational proposals** (deterministic, no LLM):
- Pattern: Agent keeps importing `requests` -> Propose CLAUDE.md convention entry ("use httpx")
- Pattern: Typecheck failures on Optional types -> Propose `strict = true` in mypy config
- Pattern: Tests fail on import errors -> Propose dependency check in feedforward
- Pattern: Ruff rule X fires on every component -> Propose ruff config change or CLAUDE.md guidance

**Inferential proposals** (LLM-assisted, human-gated):
- Pattern: Agents consistently misunderstand auth flow -> Propose CLAUDE.md section explaining auth
- Pattern: Review keeps flagging missing error handling -> Propose prompt addendum about error handling conventions
- Pattern: Components drift from API contract -> Propose structural test for API schema validation

**Human review gate**: Proposals are written to `.ralph/proposals/` as markdown files. The user reviews and applies them:

```bash
ralph evolve                    # analyze recent runs, generate proposals
ralph evolve --apply            # apply all approved proposals
ralph evolve --apply PROP-003   # apply a specific proposal
```

**Configuration** (`ralph.toml`):
```toml
[evolution]
enabled = true
journal_path = ".ralph/evolution.jsonl"
experiments_path = ".ralph/experiments.tsv"
min_pattern_frequency = 2      # pattern must occur N times before proposal
lookback_runs = 10             # how many past runs to analyze
auto_propose = true            # generate proposals after each factory run
auto_apply_computational = false  # auto-apply rule additions (no human gate)
```

---

### 3.5 Behavior harness strengthening

**What**: Address the article's "elephant in the room" - functional correctness verification. Ralph currently relies on agent-generated tests, which the article warns "puts a lot of faith into AI-generated tests, that's not good enough yet."

#### 3.5.1 Approved fixtures

**What**: Pre-approved input/output pairs that the agent's implementation must satisfy. These are human-written or human-approved golden tests that exist before the agent starts.

**How**: An optional `fixtures` field in the PRD:

```json
{
  "branchName": "ralph/auth",
  "fixtures": [
    {
      "description": "Login with valid credentials returns 200 + token",
      "type": "http",
      "input": {
        "method": "POST",
        "path": "/api/login",
        "body": {"username": "test", "password": "test123"}
      },
      "expected": {
        "status": 200,
        "body_contains": ["token", "expires_at"]
      }
    },
    {
      "description": "Login with wrong password returns 401",
      "type": "http",
      "input": {
        "method": "POST",
        "path": "/api/login",
        "body": {"username": "test", "password": "wrong"}
      },
      "expected": {
        "status": 401
      }
    }
  ],
  "userStories": [...]
}
```

**Verification**: Fixtures are checked during Phase 1 as a new mechanical check (`check_fixtures`). They run the application and validate input/output pairs. This provides behavioral verification that's independent of agent-generated tests.

**Types supported**:
- `http`: HTTP request/response pairs (requires app server to be running)
- `function`: Direct function call with args and expected return
- `cli`: Command-line invocation with expected stdout/exit code
- `file`: File content assertions after execution

**New file**: `ralph_py/fixtures.py`
**Modified file**: `ralph_py/verify.py` (add `check_fixtures`)
**Modified file**: `ralph_py/prd.py` (add optional `fixtures` field)

#### 3.5.2 Snapshot regression

**What**: After a component passes all verification phases, ralph snapshots key outputs (API responses, rendered HTML, CLI output). On subsequent runs or retries, regression is detected by comparing against snapshots.

**How**: Snapshots stored in `.ralph/snapshots/<component_id>/`. During Phase 1, if snapshots exist from a previous successful run, they're compared against current output.

**Configuration** (`ralph.toml`):
```toml
[fixtures]
enabled = false             # opt-in
snapshot_on_success = true  # auto-snapshot after Phase 2 pass
snapshot_dir = ".ralph/snapshots"
```

---

## 4. Execution order

These changes have dependencies. Implementation order:

```
Phase A (foundation):
  3.1 Factory as default       # restructure CLI, no new capabilities
  3.3 LLM-optimized sensors    # improve existing feedback quality

Phase B (feedforward):
  3.2.1 Structural analysis    # new feedforward.py module
  3.2.3 Bootstrap scaffold     # optional scaffold field on Component

Phase C (learning):
  3.4.1 Evolution journal      # post-run analysis
  3.4.2 Experiment tracking    # trend analysis
  3.4.3 Harness proposals      # meta-steering loop

Phase D (behavior):
  3.5.1 Approved fixtures      # human-written golden tests
  3.5.2 Snapshot regression    # automated regression detection
```

Phase A and Phase B can begin in parallel. Phase C depends on Phase A (needs factory as default to have consistent data). Phase D is independent and can begin whenever.

---

## 5. Configuration surface

All new configuration lives in `ralph.toml` under new sections. No new config files required.

```toml
# Existing sections (unchanged)
[agent]
[run]
[paths]
[git]
[ui]

# New sections
[feedforward]
enabled = true
module_map = true
public_interfaces = true
dependency_graph = true
conventions = true
max_context_tokens = 4000

[sensors]
parse_output = true
include_source_context = true
max_failures_per_check = 10

[evolution]
enabled = true
journal_path = ".ralph/evolution.jsonl"
experiments_path = ".ralph/experiments.tsv"
min_pattern_frequency = 2
lookback_runs = 10
auto_propose = true
auto_apply_computational = false

[fixtures]
enabled = false
snapshot_on_success = true
snapshot_dir = ".ralph/snapshots"
```

---

## 6. New CLI commands

```
ralph run [N]                  # now delegates to factory (single-component)
ralph run --no-verify [N]      # skip verification (lightweight mode)
ralph run --legacy [N]         # old behavior (direct loop.py)

ralph evolve                   # analyze runs, show proposals
ralph evolve --apply           # apply approved proposals
ralph evolve --status          # show experiment trends
```

---

## 7. New files

```
ralph_py/
  feedforward.py       # Phase 0: structural analysis, convention extraction
  parsers.py           # Structured output parsing for pytest, mypy, ruff
  fixtures.py          # Approved fixtures verification
  evolution.py         # Evolution journal, experiment tracking, proposals
```

---

## 8. Failure modes and mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Feedforward context bloats prompt beyond context window | Agent ignores or hallucinates from excess context | `max_context_tokens` cap; prioritize most relevant modules based on component's PRD |
| Evolution journal proposes bad harness changes | Degraded future runs | Human review gate for inferential proposals; computational proposals are reversible |
| Approved fixtures require running infrastructure | CI/local environments diverge | Fixture types include `function` (no infra needed) and `file` (filesystem only) |
| Factory-as-default breaks existing `ralph run` scripts | User CI pipelines break | `--legacy` flag for exact old behavior; `--no-verify` for lightweight factory |
| Structured output parsing fails on non-standard test runners | Falls back to raw output, losing value | Parsers are pluggable; unknown output format falls through to existing raw-dump behavior |

---

## 9. Success metrics

These should be measured empirically before and after each phase ships:

- **Iterations to completion**: Average number of iterations per component before all acceptance criteria pass. Feedforward (3.2) should reduce this.
- **First-attempt success rate**: Percentage of components that pass Phase 1 on the first attempt. Feedforward should increase this.
- **Retry rate**: Percentage of components that need retries. The meta-learning loop (3.4) should decrease this over time within a project.
- **Time to completion**: Wall-clock time per component. LLM-optimized sensors (3.3) should reduce retry time even if retry rate stays constant.
- **Cross-run improvement**: For projects with multiple factory runs, does retry rate decrease across runs? This measures whether the evolution journal is working.

Measure these on a set of reference specs before implementing each phase to establish baselines. The measurements themselves should be scripts, not estimates.

---

## 10. What this spec does NOT cover

- **Agent-internal improvements**: This spec is about the external harness, not the agent's own capabilities. We don't modify how Claude Code or Codex work internally.
- **Real-time monitoring / alerting**: No `ralph watch` or continuous background monitoring. The evolution journal runs post-hoc, not in real-time. Background monitoring can be a follow-up.
- **Multi-repo orchestration**: Factory mode operates within a single repository. Cross-repo coordination is out of scope.
- **Cost optimization**: Token usage tracking and budget guardrails (like AutoResearchClaw's cost guardrails) are valuable but separate from harness engineering. Could be a follow-up.
- **Custom ralph lint rules**: Existing linters (ruff, mypy, eslint) already catch problems. Ralph's value is making their output actionable, not reimplementing linting. See section 3.2.2 for rationale.
- **Harness templates**: Pre-built configurations for project topologies (python-api, react-app, etc.) are valuable but premature. Build the feedforward and evolution primitives first; templates are a packaging concern on top of them.
