# CLAUDE.md - Ralph

## Project Overview

- **Language**: Python (FastAPI / pytest / uv toolchain)
- **Project**: Ralph - an adversarial coding-agent harness
- **Layout**: `kstrl/` is the canonical factory implementation. `src/ralph/` is the legacy single-component loop (out of scope for the adversarial roadmap).

## Verification commands

- **Test**: `uv run pytest tests/ -v`
- **Calibration (opt-in, real LLMs)**: `RALPH_RUN_CALIBRATION=1 uv run pytest tests/test_calibration.py -v`
- **Typecheck**: `uv run mypy kstrl/ --strict`
- **Lint**: `uv run ruff check kstrl/ tests/`

Note on mypy scope: `pyproject.toml` declares `[tool.mypy] files = ["kstrl"]` so `uv run mypy` (no args) also checks `kstrl/`. The legacy `src/ralph/` package is intentionally not in mypy's scope -- it is the out-of-scope single-component loop, and the factory's smart-default typecheck command honors this configuration. If you actively maintain `src/ralph/`, run `uv run mypy src/ralph/ --strict` manually; CI does not gate it.

## Adversarial role taxonomy

Ralph's factory uses eight distinct roles. Three are LLM-driven adversarial passes; the rest are mechanical or computational. Full taxonomy with file:line references is in [docs/adversarial-design.md](docs/adversarial-design.md).

| Role | Prompt | What it catches |
|---|---|---|
| Architect / PRD red-team | `decompose.DECOMPOSE_PROMPT` | Spec ambiguity, missing failure modes, unstated assumptions |
| Engineer | per-project `scripts/ralph/prompt.md` | Implements one story per iteration; emits required `## Self-Critique` block |
| Mechanical verifier | `verify.run_mechanical_verification` (no LLM) | tests / typecheck / lint / diff-scope / bad-patterns / self-critique-shape |
| Code reviewer | `review.REVIEWER_PROMPT` | PRD criteria + concerns (scope_creep, security_concern, test_quality, etc.) |
| Security reviewer | `security.SECURITY_PROMPT` | OWASP-mapped vuln categories |
| Contract tester | `contract.run_contract_testing` (no LLM) | Cross-component integration tests on merged tier branches |
| Knowledge distiller | `knowledge.DISTILL_PROMPT` | Durable facts about the artifact, written pre-PR (after the review gates pass, before the PR merges main in) |
| Human checkpoint (E6) | interactive UI | Optional opt-in approval before PR merge |

## When working on this codebase

- **Do not run `/code-review` on your own code.** Per H1 of `docs/adversarial-roadmap.md`, AI self-review of AI-generated code is prohibited. The user or `/code-review ultra` is the gating reviewer.
- **Calibration is the truth signal.** When changing an adversarial prompt (`DECOMPOSE_PROMPT`, `REVIEWER_PROMPT`, `SECURITY_PROMPT`, `DISTILL_PROMPT`, the engineer prompt), re-run the calibration suite and compare detection rates against the saved baseline. A prompt edit without a calibration check is treated as untested (H2).
- **Prompt edits require a version bump AND a hash update.** Every adversarial prompt (including the harness-shipped engineer prompt `DEFAULT_PROMPT`) declares a `*_PROMPT_VERSION` semver constant next to the body. `tests/test_prompt_versions.py` snapshots each prompt as a `(hash, version)` tuple in `_EXPECTED_SNAPSHOTS`; both must move together. The test also AST-walks `kstrl/` for any module-level `*_PROMPT` constant and fails if a new one is not enrolled. The audit trail is the PR diff with prompt body + version constant + snapshot tuple all moving (H3).
- **Be explicit about what was tested vs assumed.** "Smoke passed" without listing what was checked is presence-testing, not behavior-testing (H4).
- **All adversarial-roadmap policies are tracked in `docs/adversarial-roadmap.md`**. Read it before changing the role architecture.

## Coding standards

- Type hints on all function signatures
- `from __future__ import annotations` at the top of every file
- `T | None` over `Optional[T]`; `A | B` over `Union[A, B]`
- `@dataclass` for data containers, `frozen=True` when immutable
- `Protocol` for interfaces (structural subtyping over inheritance)
- snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants
- Absolute imports grouped: stdlib, third-party, local
- No bare `except:` - always specify the exception type
- No mutable default arguments

## Implementation principles

### Adversarial mindset for any role-related code

The whole factory rests on the idea that adversarial framing causes the LLM to find bugs it would otherwise miss. When editing prompts or role code, ask: "does this make the role more skeptical, or more eager to please?" Prefer the former.

### Calibration over claims

Any change to an adversarial role should include either a calibration delta or a test against the planted-bug fixtures in `tests/adversarial_fixtures/`. Self-reported flags like `exhaustively_searched` are hints, not signals - they cannot be trusted alone.

### Halt over heroics

The architect halts on blocker-severity spec issues; hard-mode reviewers halt on findings at or above the threshold. The pipeline should fail loudly when something is wrong, not silently degrade.

### Audit trail

Every adversarial decision writes a record: review/security findings go to PR bodies, knowledge facts go to disk, evolution journal records component outcomes. Don't add silent code paths - if it's worth deciding, it's worth recording.

## What NOT to do

- Do NOT run `/code-review` on your own code (H1).
- Do NOT ship a prompt change without re-running calibration (H2).
- Do NOT update the hash in `tests/test_prompt_versions.py` without also bumping the matching `*_PROMPT_VERSION` constant (H3). The two changes always travel together.
- Do NOT use `pickle` to load untrusted data; the existing `tests/test_phase_c_coverage.py` C8 pickling test only round-trips configs we constructed in-test.
- Do NOT add unverifiable self-report claims to results without flagging them as hints (E9 added `infrastructure_error` precisely to distinguish verified from claimed; E3-infra lifts the same signal into the `Finding` stream so `len(findings)==0` is a safe "ran cleanly" check).
- Do NOT bypass the budget cap (`max_adversarial_calls`) without explicit user opt-in.

## Agent Learnings

> Maintained by agents working on this codebase.
> Append patterns, gotchas, and conventions discovered below.

### Codebase Patterns

- Atomic file writes use `tempfile.mkstemp` + `os.replace` (`manifest.py:189`, mirrored in `knowledge.py::write_facts`).
- Cross-module JSON extraction from agent output reuses `decompose._extract_json` + `decompose._select_agent_output`.
- Diff truncation uses the shared `git.truncate_diff_for_prompt` helper.

### Gotchas

- `os.replace` is not atomic on Windows; the codebase is POSIX-first.
- `fcntl.flock` (Phase A4 concurrent worktree lock) is POSIX-only; tests skip on Windows.
- Confidence value `"verified"` is legacy and aliased to `"review_passed"` on read; new code should use the new tier names.

### Conventions

- Phase numbers are sticky: Phase 0 feedforward, Phase 1 verify, Phase 2 review, Phase 2.5 security, Phase 3 contract. New phases get fractional numbers to preserve ordering semantics.
- Every config dataclass should have `from_env()` AND `load(root_dir)`; the load method reads `[<section>]` from `ralph.toml` and overlays env on top.
