"""Init command for Ralph - initialize harness in a project."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git
from ralph_py.prd import PRD

if TYPE_CHECKING:
    from ralph_py.ui.base import UI

# Default file contents
DEFAULT_PRD = {
    "branchName": "ralph/feature",
    "userStories": [],
}

DEFAULT_PROMPT_VERSION = "1.1.0"

# The $prd_path / $progress_path / $codebase_map_path placeholders are
# substituted by loop.run_loop (string.Template.safe_substitute) with the
# per-component paths, so a decomposed component's agent reads the SAME
# PRD file that verify.check_prd_stories re-reads (R2.3, H-11). Before
# v1.1.0 the body hardcoded scripts/ralph/prd.json while decomposed PRDs
# live at scripts/ralph/feature/<id>/prd.json - the agent and the
# verifier disagreed on which file mattered.
DEFAULT_PROMPT = """# Ralph Agent Instructions

You are the implementing engineer in a software factory. You will be
reviewed by a hostile code reviewer when you declare done; treat that
reviewer as already reading your diff while you write it.

## Your Task (one iteration)

1. Read the PRD file for this run: `$prd_path`
2. Read `$progress_path` (check `## Codebase Patterns` first)
3. Derive a short list of keywords from the PRD intent, not just exact wording.
4. If `$codebase_map_path` exists, query it for sections relevant to your story
   using those keywords.
   - Do not load the entire file.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
5. If a feature understand file exists for this PRD, query it using the same keywords.
   - Default path: `scripts/ralph/feature/<feature_name>/understand.md`
   - If the PRD is at `scripts/ralph/feature/<feature_name>/prd.json`, use that folder name.
   - Otherwise use the PRD filename stem as `<feature_name>`.
6. Branch is pre-checked out to `branchName` from the PRD
   (verify only; do not switch)
7. Pick the highest priority story where `passes` is `false` (lowest `priority` wins)
8. Implement that ONE story (keep the change small and focused)
9. Run feedback loops (Python + uv):
   - Find the project's fastest typecheck and tests
   - Use `uv run ...` to run them
   - If the project has no typecheck/tests configured, add them (prefer `ruff` + `mypy`
     or `pyright` + `pytest`)
     and ensure they run fast and deterministically
   - Do NOT mark the story as done unless typecheck AND tests pass. If they fail, fix and rerun;
     only proceed when both are green.
10. If you discover durable, reusable codebase facts, append a brief, evidence-based note to
   `$codebase_map_path` under **Iteration Notes** or update **Quick Facts**
   (skip if nothing new).
11. Update `AGENTS.md` files with reusable learnings
   (only if you discovered something worth preserving):
   - Only update `AGENTS.md` in directories you edited
   - Add patterns/gotchas/conventions, not story-specific notes
12. **Adversarial self-check.** Before declaring done, append the EXACT
    heading `## Self-Critique` (verbatim, two hash marks - the harness
    verifies this string) followed by AT LEAST 3 bullet lines (`- `).
    Each bullet must be substantive: not `TBD`, not `TODO`, not `N/A`.
    Format each bullet as: `- If X happens, this code will do Y, which
    is wrong because Z.` Categories to consider: invalid/empty/None
    input, concurrent access, partial-failure mid-way through a
    multi-step operation, hostile input, schema drift, missing
    auth/authz check, swallowed errors, performance under load,
    time/locale dependence. If you genuinely cannot find three, look at
    every new function and ask what could break it. Placeholders will
    fail the mechanical check.
13. Commit with message: `feat: [ID] - [Title]`
14. Update `$prd_path`: set that story's `passes` to `true`
    (only after tests/typecheck pass AND the self-critique is written)
15. Append learnings to `$progress_path`

## PRD ambiguity

If the PRD is too vague to implement responsibly, do NOT guess. Append an
`## INTERPRETATION` block to `$progress_path` stating what
assumptions you are making and why. The reviewer will see this and can
push back; silent guesses become silent bugs.

## Progress Format

Append this to the END of `$progress_path`:

## [YYYY-MM-DD] - [Story ID]
- What was implemented
- Files changed
- Verification run (exact commands)
- **Learnings:**
  - Patterns discovered
  - Gotchas encountered
- **Self-Critique:**
  - Failure mode 1: ...
  - Failure mode 2: ...
  - Failure mode 3: ...
- **Interpretations** (only if PRD was ambiguous): ...
---

## Codebase Patterns

Add reusable patterns to the TOP section in `$progress_path`
under `## Codebase Patterns`.

## Stop Condition

If ALL stories pass, reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_PROGRESS = """# Ralph Progress Log

## Codebase Patterns
- (add reusable patterns here)

## Iteration Notes
- (append entries below using the format in prompt.md)

---
"""

DEFAULT_CODEBASE_MAP = """# Codebase Map (Brownfield Notes)

This file is meant to be built over time using the Ralph **codebase understanding** loop.

## How to use this map

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Read-only mode**: in understanding mode, the agent should ONLY edit this file.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Next Topics (checklist)

Edit this list to match your repo. During the understanding loop, mark items as done.

- [ ] How to run locally (setup, env vars, start commands)
- [ ] Build / test / lint / CI gates (what runs in CI and how)
- [ ] Repo topology & module boundaries (where code lives, layering rules)
- [ ] Entrypoints (server, worker, cron, CLI)
- [ ] Configuration, env vars, secrets, feature flags
- [ ] Authn/Authz (where permissions are enforced)
- [ ] Data model & persistence (migrations, ORM patterns, transactions)
- [ ] Core domain flow #1 (trace end-to-end)
- [ ] Core domain flow #2 (trace end-to-end)
- [ ] External integrations (third-party APIs, webhooks, queues)
- [ ] Observability (logging, metrics, tracing, error reporting)
- [ ] Deployment / release process

## Quick Facts (keep updated)

- **Language / framework**:
- **How to run**:
- **How to test**:
- **How to typecheck/lint**:
- **Primary entrypoints**:
- **Data store**:

## Known "Do Not Touch" Areas (optional)

- (add directories/files that are fragile or off-limits)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

DEFAULT_FEATURE_UNDERSTAND = """# Feature Understand Notes

This file captures feature-specific understanding tied to one PRD.

## How to use this file

- **Evidence-first**: prefer citations to specific files/entrypoints over broad claims.
- **Feature scope**: keep notes anchored to the PRD for this feature.
- **Small increments**: one topic per iteration keeps notes high-signal.

## Quick Feature Facts (keep updated)

- **PRD**:
- **Branch**:
- **Stories in scope**:
- **Primary entrypoints**:
- **Data touched**:
- **Tests / commands**:

## Story Coverage (checklist)

- [ ] (add story IDs from the PRD)

## Known Risks / Hotspots (optional)

- (add areas likely to break or require extra care)

---

## Iteration Notes

(New notes append below; keep older notes for history.)
"""

DEFAULT_UNDERSTAND_PROMPT = """# Ralph Codebase Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **codebase understanding** loop. Your job is to explore the existing codebase
and write an evidence-based "map" for humans.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is:**
- `scripts/ralph/codebase_map.md`

If you think code changes are needed, write that as a note in the map under
**Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read `scripts/ralph/codebase_map.md`.
2. Choose ONE topic to investigate this iteration:
   - If `codebase_map.md` has a **Next Topics** checklist, pick the first unchecked item.
   - Otherwise follow this default order:
     1) How to run locally
     2) Build / test / lint / CI gates
     3) Repo topology & module boundaries
     4) Entrypoints (server/worker/cron/CLI)
     5) Configuration, env vars, secrets, feature flags
     6) Authn/Authz
     7) Data model & persistence (migrations, ORM patterns)
     8) Core domain flows (trace one end-to-end)
     9) External integrations
     10) Observability (logging/metrics/tracing)
     11) Deployment / release process
3. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - package/lock files
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
4. Update **ONLY** `scripts/ralph/codebase_map.md`:
   - Append a new **Iteration Notes** section for this topic (template below)
   - If you used a Next Topics checklist, mark the topic as done (`[x]`)
   - Keep notes concise, factual, and verifiable

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of `scripts/ralph/codebase_map.md`:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked topics** in the Next Topics checklist
(or you have covered the default list above), reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""

DEFAULT_FEATURE_UNDERSTAND_PROMPT = """# Ralph Feature Understanding Instructions (Read-Only)

## Goal (one iteration)

You are running a **feature understanding** loop for a specific PRD.
Your job is to build a focused, evidence-based map of the code that this feature touches.

**Hard rule:** do NOT modify application code, tests, configs, dependencies, or CI.

**The only file you may edit is the feature understand file, for example:**
- `scripts/ralph/feature/<feature_name>/understand.md`

If you think code changes are needed, write that as a note in the feature understand file
under **Open questions / Follow-ups**. Do not implement changes in this mode.

## What to do

1. Read the feature PRD file you were given.
2. Derive a short list of keywords from the PRD intent, not just exact wording.
3. Read `scripts/ralph/codebase_map.md` and query only the sections relevant to this feature.
   - Always check **Quick Facts** and any relevant **Iteration Notes**.
   - Do not load the entire file.
4. Investigate by reading docs, configs, and code. Prefer fast, high-signal entrypoints:
   - README / docs
   - build/test scripts
   - app entrypoints (server/main)
   - routes/controllers
   - data layer (models, migrations)
5. Update **ONLY** the feature understand file:
   - Update **Quick Feature Facts** if you learned something durable
   - Append a new **Iteration Notes** section for this topic (template below)
   - If there is a **Story Coverage** checklist, mark items you verified

## Evidence rules (important)

- Every "fact" should include **evidence**:
  - File paths
  - What to look for (function/class name)
  - Preferably line ranges (if your tooling can provide them)
- If you are uncertain, label it clearly as a hypothesis and add an **Open question**.

## Iteration Notes format

Append this to the END of the feature understand file:

## [YYYY-MM-DD] - [Topic]

- **Summary**: 1-3 bullets on what you learned
- **Evidence**:
  - `path/to/file.ext` - what to look for (and line range if available)
- **Conventions / invariants**:
  - "Do X, don't do Y" rules implied by the codebase
- **Risks / hotspots**:
  - Areas likely to break or require extra care
- **Open questions / follow-ups**:
  - What's unclear, what needs human confirmation

---

## Stop condition

If there are **no remaining unchecked stories** in the **Story Coverage** checklist,
reply with exactly:

<promise>COMPLETE</promise>

Otherwise end normally.
"""


# Scaffolded ralph.toml (R2.1): the project's discoverable config
# surface. Every key is commented out and shows its built-in default, so
# scaffolding changes no effective value; uncommenting a line is the
# explicit opt-in. Content mirrors ralph.toml.example trimmed to keys the
# loaders actually read, plus the [timeout] section wired in R0.1.
DEFAULT_RALPH_TOML = """\
# Ralph configuration (scaffolded by `ralph init`).
# Every key is commented out and shows its built-in default: uncomment a
# line to override it. Precedence: CLI flag > environment variable > this
# file > built-in default. See docs/env-vars.md for the env-var mapping.

[agent]
# type = ""                        # "claude" | "codex" | "custom" (empty = auto-detect)
# command = ""                     # shell command (only used when type = "custom")
# model = ""                       # e.g. "sonnet" for claude, "o3" for codex (empty = agent default)
# reasoning_effort = ""            # low|medium|high|max

[run]
# max_iterations = 10
# sleep_seconds = 2
# interactive = false

[paths]
# prompt = "scripts/ralph/prompt.md"
# prd = "scripts/ralph/prd.json"
# progress = "scripts/ralph/progress.txt"
# codebase_map = "scripts/ralph/codebase_map.md"
# allowed = []                     # e.g. ["scripts/ralph/", "src/"]

[git]
# branch = ""                      # override branch (empty = use PRD branchName)
# auto_checkout = true

[ui]
# ascii = false

# Factory orchestrator settings (Phase 0-3 pipeline coordination).
[factory]
# max_parallel = 4                 # concurrent component workers
# max_retries = 3                  # per-component retry budget across all phases
# retry_delay = 5.0                # seconds between retry attempts
# use_worktrees = true             # branch each component into .ralph/worktrees/<id>
# single_pr = false                # one PR for the whole factory vs per-component
# create_prs = true                # call `gh` to push + merge per component
# review_mode = "hard"             # hard | advisory | skip
# merge_timeout = 300.0            # seconds to wait for PR merge confirmation
# max_adversarial_calls = 0        # 0 = unbounded; caps review+security+distill LLM calls per run
# pause_before_pr_merge = false    # opt-in HITL checkpoint before each PR push+merge

# Phase 1 mechanical verification.
[verify]
# test_command = "uv run pytest"   # empty/omitted = project-type default
# typecheck_command = "uv run mypy ."
# lint_command = "uv run ruff check ."
# check_diff_scope = true
# check_bad_patterns = true
# dead_code_cleanup = false
# dead_code_command = ""           # custom dead-code detector (default: vulture)
# mutation_testing = false
# mutation_threshold = 50.0
# mutation_timeout = 600.0
# subprocess_timeout = 300.0
# require_self_critique = false    # fail Phase 1 if the ## Self-Critique block is missing/sparse
# self_critique_min_bullets = 3
# progress_file_path = "scripts/ralph/progress.txt"

# Phase 2.5 security review (independent adversarial pass focused on vulns).
[security]
# mode = "skip"                    # skip | advisory | hard (skip = default, opt in explicitly)
# fail_threshold = "high"          # critical | high | medium | low (hard mode only)
# timeout_seconds = 600.0
# agent_cmd = ""                   # leave blank to inherit from [agent]
# agent_type = ""
# model = ""

# Phase 3 cross-component contract testing.
[contract]
# mode = "tier"                    # tier | final | skip
# test_command = "uv run pytest"
# timeout = 600.0

# Phase 0 feedforward (computational structural scan; no LLM).
[feedforward]
# enabled = true
# module_map = true
# public_interfaces = true
# dependency_graph = true
# conventions = true
# max_context_tokens = 4000

# Per-component semantic knowledge layer: durable facts about WHAT WAS
# BUILT (interfaces, invariants, contracts, gotchas), written after the
# review passes and read by downstream components automatically.
[knowledge]
# enabled = true
# max_core_tokens = 2000           # current component's facts (full text)
# max_dependency_tokens = 1000     # dependency facts (full text)
# max_sibling_tokens = 500         # other components' facts (first sentence only)
# distill_timeout_seconds = 300
# distill_model = ""               # empty = falls back to [agent].model
# max_facts_per_distill = 7
# dependency_scope = "direct"      # direct | transitive

# Continuous-learning journal.
[evolution]
# enabled = true
# journal_path = ".ralph/evolution.jsonl"
# experiments_path = ".ralph/experiments.tsv"
# min_pattern_frequency = 2
# lookback_runs = 10

# Timeouts in seconds; 0 or less disables that limit.
[timeout]
# git_operation = 30.0
# agent_iteration = 1800.0         # per agent iteration
# component_total = 7200.0         # wall clock per component
# verification_check = 300.0
# review_agent = 600.0
# contract_test = 600.0
# subprocess_default = 60.0
# scheduler_backstop_margin = 60.0
"""


def run_init(directory: Path, ui: UI) -> int:
    """Initialize Ralph harness in a project directory.

    Args:
        directory: Target project directory
        ui: UI for output

    Returns:
        Exit code (0=success, 1=validation failure, 2=directory not found)
    """
    ui.title("Ralph Init")

    # Validate directory
    ui.section("Target")
    if not directory.exists():
        ui.err(f"Directory not found: {directory}")
        return 2

    root = directory.resolve()
    ui.kv("Directory", str(root))

    # Check for git repo
    is_repo = git.is_git_repo(root)
    if is_repo:
        ui.ok("Git repository detected")
    else:
        ui.warn("Not a git repository")

    ui.section("Scaffold")
    ralph_dir = root / "scripts" / "ralph"
    if not ralph_dir.exists():
        ralph_dir.mkdir(parents=True, exist_ok=True)
        ui.ok("Created scripts/ralph/")
    else:
        ui.ok("scripts/ralph/ exists")

    ui.section("Create defaults")
    _create_if_missing(root / "ralph.toml", DEFAULT_RALPH_TOML, ui)
    _create_if_missing(ralph_dir / "prompt.md", DEFAULT_PROMPT, ui)
    _create_if_missing(ralph_dir / "prd.json", json.dumps(DEFAULT_PRD, indent=2) + "\n", ui)
    _create_if_missing(ralph_dir / "progress.txt", DEFAULT_PROGRESS, ui)
    _create_if_missing(ralph_dir / "codebase_map.md", DEFAULT_CODEBASE_MAP, ui)
    _create_if_missing(ralph_dir / "understand_prompt.md", DEFAULT_UNDERSTAND_PROMPT, ui)
    _create_if_missing(
        ralph_dir / "feature_understand_prompt.md",
        DEFAULT_FEATURE_UNDERSTAND_PROMPT,
        ui,
    )

    # Bootstrap CLAUDE.md and AGENTS.md
    bootstrap_claude_md(root, ui)

    # Validate PRD
    ui.section("Validate PRD")
    prd_file = ralph_dir / "prd.json"

    try:
        with open(prd_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        ui.err(f"Invalid JSON in prd.json: {e}")
        return 1

    errors = PRD.validate_schema(data)
    if errors:
        ui.err("PRD schema validation failed:")
        for error in errors:
            ui.info(f"  - {error}")
        return 1

    ui.ok("PRD schema valid")

    # PRD summary
    ui.section("PRD summary")
    prd = PRD.load(prd_file)
    ui.kv("Branch", prd.branch_name)
    ui.kv("Stories", str(len(prd.user_stories)))

    passing = sum(1 for s in prd.user_stories if s.passes)
    failing = len(prd.user_stories) - passing
    if prd.user_stories:
        ui.kv("Passing", str(passing))
        ui.kv("Failing", str(failing))

    # Next steps
    ui.section("Next steps")
    ui.info("1. Edit scripts/ralph/prompt.md")
    ui.info("2. Add user stories to scripts/ralph/prd.json")
    ui.info("3. Run: ralph run [iterations]")
    ui.info("")
    ui.info("For codebase understanding mode:")
    ui.info("  ralph understand [iterations]")
    ui.info("")
    ui.info("For feature understanding mode:")
    ui.info("  ralph feature [iterations] --prd scripts/ralph/feature/<feature_name>/prd.json")

    return 0


def _create_if_missing(path: Path, content: str, ui: UI) -> None:
    """Create file if it doesn't exist."""
    if path.exists():
        ui.info(f"  {path.name} already exists")
    else:
        path.write_text(content)
        ui.ok(f"  Created {path.name}")


# ---------------------------------------------------------------------------
# CLAUDE.md and AGENTS.md bootstrap
# ---------------------------------------------------------------------------


def _detect_project_context(root: Path) -> dict[str, str]:
    """Detect project language, framework, and tooling from config files.

    Inspects the project root for pyproject.toml, Cargo.toml, package.json,
    go.mod, etc. and returns a dict of detected values.
    """
    ctx: dict[str, str] = {
        "name": root.name,
        "language": "unknown",
        "framework": "",
        "test_cmd": "",
        "lint_cmd": "",
        "typecheck_cmd": "",
        "build_cmd": "",
        "format_cmd": "",
    }

    # Python
    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    if pyproject.exists() or setup_py.exists():
        ctx["language"] = "Python"
        has_uv = (root / "uv.lock").exists() or (root / ".venv").exists()
        runner = "uv run " if has_uv else ""
        ctx["test_cmd"] = f"{runner}pytest tests/ -v --tb=short"
        ctx["typecheck_cmd"] = f"{runner}mypy src/ --strict"
        ctx["lint_cmd"] = f"{runner}ruff check src/"
        ctx["format_cmd"] = f"{runner}ruff format src/"
        if pyproject.exists():
            try:
                text = pyproject.read_text()
                if "name" in text:
                    import re

                    m = re.search(r'name\s*=\s*"([^"]+)"', text)
                    if m:
                        ctx["name"] = m.group(1)
                if "fastapi" in text:
                    ctx["framework"] = "FastAPI"
                elif "django" in text:
                    ctx["framework"] = "Django"
                elif "flask" in text:
                    ctx["framework"] = "Flask"
            except OSError:
                pass
        return ctx

    # Rust
    cargo_toml = root / "Cargo.toml"
    if cargo_toml.exists():
        ctx["language"] = "Rust"
        ctx["test_cmd"] = "cargo test"
        ctx["lint_cmd"] = "cargo clippy -- -D warnings"
        ctx["typecheck_cmd"] = "cargo check"
        ctx["build_cmd"] = "cargo build"
        ctx["format_cmd"] = "cargo fmt"
        try:
            text = cargo_toml.read_text()
            import re

            m = re.search(r'name\s*=\s*"([^"]+)"', text)
            if m:
                ctx["name"] = m.group(1)
            if "actix" in text or "axum" in text:
                ctx["framework"] = "Axum/Actix"
            elif "rocket" in text:
                ctx["framework"] = "Rocket"
        except OSError:
            pass
        return ctx

    # TypeScript / JavaScript
    pkg_json = root / "package.json"
    if pkg_json.exists():
        ctx["language"] = "TypeScript"
        try:
            import json as _json

            pkg = _json.loads(pkg_json.read_text())
            ctx["name"] = pkg.get("name", root.name)
            scripts = pkg.get("scripts", {})
            ctx["test_cmd"] = (
                f"npm run {scripts.get('test', 'test')}"
                if "test" in scripts else "npx jest"
            )
            ctx["lint_cmd"] = (
                f"npm run {scripts.get('lint', 'lint')}"
                if "lint" in scripts else "npx eslint ."
            )
            ctx["typecheck_cmd"] = "npx tsc --noEmit"
            ctx["build_cmd"] = "npm run build" if "build" in scripts else ""
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                ctx["framework"] = "Next.js"
            elif "react" in deps:
                ctx["framework"] = "React"
            elif "express" in deps:
                ctx["framework"] = "Express"
            elif "vue" in deps:
                ctx["framework"] = "Vue"
            if "typescript" not in deps and not (root / "tsconfig.json").exists():
                ctx["language"] = "JavaScript"
        except (OSError, ValueError):
            pass
        return ctx

    # Go
    go_mod = root / "go.mod"
    if go_mod.exists():
        ctx["language"] = "Go"
        ctx["test_cmd"] = "go test ./..."
        ctx["lint_cmd"] = "golangci-lint run"
        ctx["typecheck_cmd"] = "go vet ./..."
        ctx["build_cmd"] = "go build ./..."
        ctx["format_cmd"] = "gofmt -w ."
        try:
            text = go_mod.read_text()
            first_line = text.strip().splitlines()[0] if text.strip() else ""
            if first_line.startswith("module "):
                ctx["name"] = first_line.split()[-1].split("/")[-1]
        except OSError:
            pass
        return ctx

    # Java / Kotlin
    if (
        (root / "pom.xml").exists()
        or (root / "build.gradle").exists()
        or (root / "build.gradle.kts").exists()
    ):
        ctx["language"] = "Java"
        if (root / "build.gradle.kts").exists():
            ctx["language"] = "Kotlin"
        ctx["test_cmd"] = "./gradlew test" if (root / "gradlew").exists() else "mvn test"
        ctx["build_cmd"] = "./gradlew build" if (root / "gradlew").exists() else "mvn package"
        return ctx

    return ctx


_LANGUAGE_STANDARDS: dict[str, str] = {
    "Python": """
- Use type hints on ALL function signatures
- Use `from __future__ import annotations` in every file
- Use `T | None` not `Optional[T]`, `A | B` not `Union[A, B]`
- Prefer `@dataclass` for data models, `frozen=True` when immutable
- Use `Protocol` for interfaces (structural subtyping over inheritance)
- Google-style docstrings with Args/Returns/Raises sections
- snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants
- Absolute imports only, grouped: stdlib, third-party, local
- No star imports, no circular imports
- No bare `except:` clauses - always specify the exception type
- No mutable default arguments (use `field(default_factory=...)`)
""",
    "Rust": """
- Use `Result<T, E>` for fallible operations, not panics
- Prefer `&str` over `String` in function parameters
- Use `derive` macros: Debug, Clone, PartialEq where appropriate
- Handle all match arms exhaustively - no catch-all `_` unless justified
- Prefer iterators and combinators over manual loops
- Use `clippy::pedantic` lint level
- Document public APIs with `///` doc comments
- Use `thiserror` for library errors, `anyhow` for application errors
- Minimize `unwrap()` - use `?` or explicit error handling
- Prefer `impl Trait` over `dyn Trait` when the concrete type is known
""",
    "TypeScript": """
- Enable strict mode in tsconfig.json
- Use explicit return types on all exported functions
- Prefer `interface` over `type` for object shapes
- Use `readonly` for properties that should not be mutated
- Prefer `unknown` over `any` - narrow with type guards
- Use discriminated unions for variant types
- Handle all Promise rejections - no unhandled promises
- Use `const` by default, `let` only when mutation is needed, never `var`
- Prefer named exports over default exports
- Use template literals over string concatenation
""",
    "Go": """
- Handle every error - never use `_` for error returns
- Use table-driven tests
- Keep interfaces small (1-3 methods)
- Accept interfaces, return structs
- Use `context.Context` as the first parameter for cancellable operations
- Prefer composition over embedding
- Use `errors.Is` and `errors.As` for error checking, not string matching
- Document all exported identifiers
- Use `go vet` and `golangci-lint` in CI
- Prefer channels for synchronization, mutexes for state protection
""",
    "Java": """
- Use final for variables that should not be reassigned
- Prefer composition over inheritance
- Use Optional<T> instead of null for return types
- Document public APIs with Javadoc
- Use try-with-resources for AutoCloseable resources
- Prefer immutable collections where possible
- Use meaningful exception types, not generic RuntimeException
""",
    "Kotlin": """
- Prefer val over var (immutability by default)
- Use data classes for plain data holders
- Use sealed classes for restricted hierarchies
- Prefer expression bodies for simple functions
- Use coroutines for async operations, not callbacks
- Leverage null safety - avoid `!!` operator
- Use `when` expressions exhaustively
""",
}

_LANGUAGE_ANTIPATTERNS: dict[str, str] = {
    "Python": """
- Do NOT use `typing.Optional` or `typing.Union` - use `|` syntax
- Do NOT use `Any` without a TODO comment explaining why
- Do NOT use mutable default arguments (`def f(x=[])`)
- Do NOT use bare `except:` or `except Exception:` without re-raising
- Do NOT use `import *`
- Do NOT use `type: ignore` without a specific mypy error code
- Do NOT use `global` or `nonlocal` unless absolutely necessary
- Do NOT suppress linter warnings without justification
""",
    "Rust": """
- Do NOT use `unwrap()` or `expect()` in library code
- Do NOT use `unsafe` without a SAFETY comment explaining the invariant
- Do NOT use `clone()` to avoid borrow checker issues - redesign instead
- Do NOT use `Box<dyn Any>` as an escape hatch from the type system
- Do NOT ignore compiler warnings - treat them as errors
- Do NOT use `String` in struct fields when `&str` with a lifetime would work
""",
    "TypeScript": """
- Do NOT use `any` - use `unknown` and narrow with type guards
- Do NOT use `!` non-null assertion operator without justification
- Do NOT use `var` - use `const` or `let`
- Do NOT use `==` - always use `===`
- Do NOT ignore TypeScript errors with `@ts-ignore` without a specific reason
- Do NOT use `Function` or `Object` types - use specific signatures
""",
    "Go": """
- Do NOT use `panic` for error handling in library code
- Do NOT ignore errors with `_`
- Do NOT use `init()` functions unless absolutely necessary
- Do NOT use global mutable state
- Do NOT use `interface{}` / `any` as an escape hatch from the type system
""",
}


def _generate_claude_md(ctx: dict[str, str]) -> str:
    """Generate CLAUDE.md content from detected project context."""
    lang = ctx["language"]
    framework_line = f" ({ctx['framework']})" if ctx["framework"] else ""

    sections = [f"# CLAUDE.md - {ctx['name']}", ""]

    # Project overview
    sections.append("## Project Overview")
    sections.append(f"- **Language**: {lang}{framework_line}")
    sections.append(f"- **Project**: {ctx['name']}")
    sections.append("")

    # Verification commands
    sections.append("## Verification Commands")
    if ctx["test_cmd"]:
        sections.append(f"- **Test**: `{ctx['test_cmd']}`")
    if ctx["typecheck_cmd"]:
        sections.append(f"- **Typecheck**: `{ctx['typecheck_cmd']}`")
    if ctx["lint_cmd"]:
        sections.append(f"- **Lint**: `{ctx['lint_cmd']}`")
    if ctx["format_cmd"]:
        sections.append(f"- **Format**: `{ctx['format_cmd']}`")
    if ctx["build_cmd"]:
        sections.append(f"- **Build**: `{ctx['build_cmd']}`")
    sections.append("")

    # Coding standards
    standards = _LANGUAGE_STANDARDS.get(lang, "")
    if standards:
        sections.append("## Coding Standards")
        sections.append(standards.strip())
        sections.append("")

    # Implementation principles (language-agnostic, elite-level)
    sections.append("""## Implementation Principles

### First Principles Thinking
- Reason from first principles about WHY the code should work, not just HOW
- Consider nth-order effects: what happens downstream when this function's contract changes?
- Ask "what invariant does this maintain?" for every data structure and state transition
- Before implementing, understand the problem domain - do not cargo-cult patterns from other contexts

### No Shortcuts
- Do not implement stub functions that return hardcoded values
- Do not add TODO comments as a substitute for implementation
- Do not use placeholder/dummy values in production code paths
- Do not catch exceptions just to silence them
- Do not skip validation because "it should never happen"
- Every code path must be intentional and justified

### No Handwaving
- Every function must have a concrete, complete implementation
- Error handling must cover ALL failure modes, not just the happy path
- Edge cases (empty inputs, None values, boundary conditions, concurrent access) must be handled explicitly
- Do not assume "this will never happen" - if the type system allows it, handle it
- Performance implications must be considered, not deferred

### Correctness Over Cleverness
- Prefer readable, straightforward implementations over clever one-liners
- Add assertions for preconditions that the type system cannot enforce
- Use immutable data structures by default
- Never silently swallow errors or return default values for unexpected inputs
- Make illegal states unrepresentable through the type system

### Testing Discipline
- Every public function needs at least one test
- Test the contract (inputs/outputs), not the implementation details
- Include edge cases: empty inputs, single elements, maximum values, None/null, unicode, negative numbers
- Error paths are tested as thoroughly as success paths
- Do not write tests that always pass (tautological assertions like `assert True`)
- Tests must be deterministic - no flaky tests, no time-dependent assertions

### Completeness
- Implement ALL specified behavior, not a subset
- Handle ALL variants of enums and match/switch expressions
- Implement ALL methods of an interface/protocol/trait, not just the common ones
- Do not leave partial implementations - either fully implement or explicitly raise/panic with a reason
- Documentation matches behavior - if docs say it does X, it must do X""")
    sections.append("")

    # Anti-patterns
    antipatterns = _LANGUAGE_ANTIPATTERNS.get(lang, "")
    if antipatterns:
        sections.append("## What NOT To Do")
        sections.append(antipatterns.strip())
        sections.append("")

    # Agent learnings section (agents append patterns, gotchas, conventions here)
    sections.append("""## Agent Learnings

> This section is maintained by AI agents working on this codebase.
> Agents: append patterns, gotchas, and conventions you discover below.
> This is the single source of truth - AGENTS.md is a symlink to this file.

### Codebase Patterns
<!-- Agents: add reusable patterns you discover here -->

### Gotchas
<!-- Agents: add surprises and non-obvious behaviors here -->

### Conventions
<!-- Agents: add established conventions here -->""")
    sections.append("")

    return "\n".join(sections) + "\n"


def bootstrap_claude_md(root: Path, ui: UI) -> None:
    """Generate CLAUDE.md and symlink AGENTS.md to it.

    Detects project language, framework, and tooling from config files,
    then generates agent-facing documentation with coding standards,
    implementation principles, and verification commands.

    AGENTS.md is a symlink to CLAUDE.md so both names point to the same
    file. When the prompt tells agents to "update AGENTS.md", they are
    writing to CLAUDE.md.
    """
    import os

    ui.section("Agent context files")

    ctx = _detect_project_context(root)
    ui.kv("Detected language", ctx["language"])
    if ctx["framework"]:
        ui.kv("Detected framework", ctx["framework"])

    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        ui.info("  CLAUDE.md already exists")
    else:
        claude_md.write_text(_generate_claude_md(ctx))
        ui.ok("  Created CLAUDE.md")

    agents_md = root / "AGENTS.md"
    if agents_md.is_symlink() and os.readlink(str(agents_md)) == "CLAUDE.md":
        ui.info("  AGENTS.md already symlinked to CLAUDE.md")
    elif agents_md.exists():
        ui.info("  AGENTS.md already exists (not a symlink)")
    else:
        # Create relative symlink: AGENTS.md -> CLAUDE.md
        agents_md.symlink_to("CLAUDE.md")
        ui.ok("  Created AGENTS.md -> CLAUDE.md (symlink)")
