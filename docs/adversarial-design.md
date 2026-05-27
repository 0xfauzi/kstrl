# Adversarial Factory Design

This document explains the roles, phases, and invariants that make up the Ralph factory's adversarial design, and the known limitations a user must keep in mind.

## Why adversarial

The factory orchestrates LLM agents to implement software. An LLM left to its own devices is helpful by default - it will tell you the diff is good, the spec is clear, and the code is safe. Adversarial design assumes the opposite: that at every step something is wrong, and that the only way to find it is to commit a specific role to looking for it. Each role's prompt is framed to be skeptical, evidence-required, and gated by a check the LLM cannot lie its way around.

Calibration is the truth signal. The `tests/test_calibration.py` suite (Phase D of the hardening roadmap) feeds planted bugs to each role and measures detection rate. The whole adversarial design is only as good as that number says it is.

## Roles

| Role | Module | Prompt | Phase | What it catches |
|---|---|---|---|---|
| **Architect / PRD red-team** | `ralph_py/decompose.py` | `DECOMPOSE_PROMPT` | Spec | Ambiguities, missing failure modes, unstated assumptions, undefined auth, ambiguous quantifiers. Halts the pipeline via `SpecBlockerError` when any blocker-severity issue is found. |
| **Engineer** | `ralph_py/init_cmd.py` (`DEFAULT_PROMPT`) + per-project `scripts/ralph/prompt.md` | (project-specific) | Iteration | Implements one story per iteration. Required to emit a `## Self-Critique` block with >=3 substantive failure-mode bullets before declaring done (mechanically enforced by `verify.check_self_critique` when `VerifyConfig.require_self_critique` is True). |
| **Mechanical verifier** | `ralph_py/verify.py` | (no LLM) | Phase 1 | PRD stories pass-marked, tests/typecheck/lint green, diff-scope and bad-pattern checks, optional dead-code / mutation / self-critique. |
| **Code reviewer** | `ralph_py/review.py` | `REVIEWER_PROMPT` | Phase 2 | PRD criterion verdicts plus a separate `concerns` array (scope_creep, security_concern, test_quality, unrelated_change, dead_code, error_handling, copy_paste). Self-Critique block is stripped from the diff before review so the reviewer is not biased by the engineer's own failure-mode list. |
| **Security reviewer** | `ralph_py/security.py` | `SECURITY_PROMPT` | Phase 2.5 | Threat-model framing: injection, auth_bypass, hardcoded_secret, unsafe_deserialization, broken_crypto, predictable_randomness, missing_input_validation, race_condition, SSRF, XSS, open_redirect, information_disclosure, denial_of_service. Each category mapped to OWASP Top 10 + CWE via `SECURITY_CATEGORY_MAP`. |
| **Contract tester** | `ralph_py/contract.py` | (no LLM) | Phase 3 | Cross-component integration tests on merged tier branches. Failing tier identifies a breaker component, sent back through Phase 1+ for retry. |
| **Knowledge distiller** | `ralph_py/knowledge.py` | `DISTILL_PROMPT` | Post-merge | Captures durable facts about the artifact for downstream components. Voyager-style write gate: only fires when Phase 2 review passed. |
| **Human checkpoint (E6)** | `ralph_py/factory.py` | (interactive) | Pre-merge | Optional. When `FactoryConfig.pause_before_pr_merge=True` and UI is interactive, prompts a human to approve or reject before PR creation. Off by default. |

## Findings model (E3)

Every finding produced by Phase 2 (`ReviewResult`) or Phase 2.5 (`SecurityResult`) is converted into a typed `Finding` (`ralph_py/findings.py`) before landing on `Component.findings: list[Finding]`. **Consumers**: `pr.py` renders findings into the PR body via `render_findings_markdown` (the legacy `review_findings` string is a fallback for legacy manifests); `evolution.py::record_run` serializes `findings` + a `findings_summary` aggregator into the journal.

The fields are:

| Field | Type | Notes |
|---|---|---|
| `phase` | str | `"review"` or `"security"` |
| `category` | str | Reviewer concern category, security category, or `"prd_criterion"` for failed acceptance criteria |
| `severity` | str | Native to the role: `"fail" / "advisory"` (review), `"critical" / "high" / "medium" / "low"` (security) |
| `location` | str | `file:line`, file path, or `"(entire file)"` |
| `explanation` | str | Free text |
| `suggestion` | str | Optional |
| `owasp`, `cwe` | str | Populated for security findings via `SECURITY_CATEGORY_MAP` |
| `tags` | tuple[str,...] | Free-form; reserved for downstream consumers |

`Component.findings` is the source of truth. The string at `Component.review_findings` is a derived view kept as a backward-compat fallback for legacy manifests where the typed list is absent.

### Infrastructure error semantics (E3-infra)

When a role's result has `infrastructure_error=True` (timeout, parse failure, agent crash), `as_findings()` emits a single synthetic `Finding(phase=<role>, category="infrastructure_error", severity="critical")` with `is_infrastructure_error=True`. This guarantees:

- `len(findings) == 0` always means "the role ran AND found nothing" — a verifiably clean review.
- `[f for f in findings if not f.is_infrastructure_error]` filters to the verified subset.
- A consumer that checks only `len(findings) > 0` to gate something will not accidentally pass an unverified component through.

### Tag conventions (E3-tags)

Each Finding emitted by the factory path carries:
- `phase:<role>` (review or security)
- `category:<X>` (matching the `category` field)
- For security: `owasp:<bucket>` and `cwe:<id>` when `SECURITY_CATEGORY_MAP` covers the category
- For infrastructure errors: `infrastructure`

Tags let downstream consumers filter by taxonomy without re-parsing the field-level data.

## Pipeline

```
spec.md
  -> [Architect] decompose + red-team
        -> manifest.json + per-component PRDs
        -> SpecBlockerError if any blocker (halt; human resolves spec)
  -> for each component (DAG order, optionally parallel):
       -> [Phase 0] feedforward (computational structural scan)
       -> [Engineer] iterate until COMPLETE
       -> [Phase 1] mechanical verify (incl. optional self-critique check)
       -> [Phase 2] code reviewer (criteria + concerns)
       -> [Phase 2.5] security reviewer
       -> [HITL checkpoint] if enabled
       -> create + merge PR
       -> [Knowledge distiller] post-merge write
  -> [Phase 3] contract testing across merged tiers
  -> evolution journal recorded
```

## Invariants

1. **Halt over heroics.** Architect's `SpecBlockerError` stops the pipeline rather than proceeding with a vague spec. Mechanical verification failures retry up to `FactoryConfig.max_retries` then mark the component failed and cascade-skip dependents.
2. **Hard mode means hard fail.** Phase 2 / Phase 2.5 in `hard` mode block on findings at or above the configured threshold. Infrastructure failures (agent crash, parse error) in hard mode count as failures, not silent passes (Phase A1 + E9).
3. **Latest-run-dir wins for facts.** Knowledge files at `.ralph/knowledge/<component_id>/<run_id>/<fact_id>.md`. A breaker retry naturally orphans the old run dir.
4. **No prompt injection through knowledge.** Fact claims that match role markers (`<system>`, `<|im_*|>`), `ignore previous instructions` patterns, or `## Instructions` headings are rejected at coercion time (Phase A1).
5. **No infinite cost.** `FactoryConfig.max_adversarial_calls` is a hard cap across review + security + distill (Phase E4). Stream-size cap of 5MB per agent invocation prevents pathological output (Phase A5).
6. **Audit trail.** Evolution journal records every component result. Concerns surface to `EvolutionJournal.get_concern_hit_rate()` for aggregate dashboards. Knowledge fact utilization is measured per component via `knowledge.measure_fact_utilization()`.

## Calibration

Calibration is the trustworthy verification path for "do the adversarial roles actually catch bugs." Without it, every "exhaustively_searched: true" claim is unverifiable.

To run:
```bash
RALPH_RUN_CALIBRATION=1 RALPH_CALIBRATION_MODEL=haiku uv run pytest tests/test_calibration.py -v
```

Each run writes `tests/adversarial_fixtures/_results/baseline-<UTC>.json` with per-role detection rates. Fixtures cover security (5), reviewer concerns (3), and vague specs (3).

The fixtures themselves live in `tests/adversarial_fixtures/{security,concerns,specs}/` with paired `.meta.json` files describing the planted bug and the must-detect category.

## Feedforward vs knowledge (E7)

Two memory surfaces exist for the implementing agent. They look similar but serve different jobs:

- **Feedforward** (`ralph_py/feedforward.py`) is *computed* fresh each iteration. Walks the worktree, builds a module map with LOC counts, lists public interfaces from `__init__.py` / `__all__`, infers a dependency graph from imports, and extracts conventions from `pyproject.toml` / `package.json` / etc. No LLM, no persistence. Used to ground the implementing agent in the current code shape.
- **Knowledge** (`ralph_py/knowledge.py`) is *distilled* by an LLM after a component completes and persists across runs. Stored at `.ralph/knowledge/<component>/<run>/<fact>.md`. Three-tier retrieval (core / dependency / sibling) injects relevant facts into the prompt of downstream components.

The overlap: both can describe what a component exports. The distinction:
- Feedforward describes what *exists* at this instant (computationally extracted).
- Knowledge describes what was *learned* about an artifact's contract or invariants (LLM-distilled, durable).

If a feedforward entry says `auth.middleware.verify_token(token: str) -> User`, that's the current signature. If a knowledge fact says "the middleware rejects expired tokens at the handler layer, before the route guard runs," that's the *behavior* the LLM extracted from passing tests + the diff. They complement, but neither replaces the other.

### Dependency scope (E8)

The knowledge layer's "Dependencies" tier defaults to `direct` scope: only facts from `Component.dependencies` (the import surface declared in the manifest) appear in the full-text tier. Transitive dependencies still surface in the sibling summary tier (first-sentence only).

Rationale: the typical reason a component needs full-text facts about a transitive dependency is that the manifest is missing a direct edge - i.e. the architect under-specified imports. Forcing the user to add the edge is better than silently injecting every transitive ancestor's facts into every downstream prompt. For projects that genuinely need the old behavior, `KnowledgeConfig.dependency_scope = "transitive"` (or `RALPH_KNOWLEDGE_DEPENDENCY_SCOPE=transitive`) restores it.

### Telemetry for the direct-vs-transitive gap (E8-telemetry)

Switching to `direct` scope can silently drop facts that downstream components were relying on. To make that visible, `build_knowledge_context` writes a per-component event to `<knowledge_root>/_e8_dependency_scope.jsonl` every time it excludes one or more transitive deps. The event records `excluded_dep_count` and `withheld_fact_count`. Read via `read_dependency_scope_telemetry(knowledge_root) -> list[dict]`.

Healthy state: empty file. Persistent non-zero values per build are the signal that direct scope is dropping information real workflows need, and the architect should be asked to make the missing edges explicit (or `dependency_scope=transitive` re-enabled).

## Known limitations

1. **Correlated failure.** The architect, engineer, reviewer, security reviewer, and distiller can all be the same model. They will fail on the same inputs in the same way. Multi-model rotation (roadmap E1) was deliberately deferred; until it lands, treat "all roles agree" as one data point, not several.
2. **`exhaustively_searched` is self-reported.** Both reviewer and security results expose the flag, but it cannot be verified at runtime. The trustworthy signal is calibration rate, not the flag.
3. **Fact-utilization is a lower bound.** `measure_fact_utilization` uses a 30-character case-insensitive substring match. LLMs paraphrase, so a false negative just means we under-count.
4. **Calibration baseline is non-deterministic.** LLMs vary; a single calibration run is one data point. Aggregate across multiple runs before trusting a detection rate as a stable measurement.
5. **Windows is not supported for concurrent worktrees.** `fcntl.flock` is POSIX-only (Phase A4); on Windows the lock is silently skipped and concurrent factory invocations against the same worktree directory can clobber each other.
6. **The fact-injection prompt is trusted code.** A future model that ignores the engineer prompt's "treat as ground truth" framing could be misled by injected facts. The Phase A1 sanitizer is a defense-in-depth pattern, not a guarantee.

## Process: how this design stays trustworthy

H1 of the hardening roadmap: the assistant does not run `/code-review` on its own code. The user, or `/code-review ultra`, is the gating reviewer for changes that touch this design.

H2: when an adversarial prompt changes, calibration is re-run. A prompt edit without a calibration delta is treated as untested.

H3: every adversarial prompt has a `*_PROMPT_VERSION` semver constant next to its body and a `(hash, version)` snapshot in `tests/test_prompt_versions.py::_EXPECTED_SNAPSHOTS`. The joint snapshot catches three drift modes:

1. **Prompt edit without snapshot bump**: hash differs from recorded hash, test fails.
2. **Version constant change without snapshot bump**: live version differs from snapshot version, test fails.
3. **New `*_PROMPT` added without enrollment**: `test_no_unenrolled_prompt_constants` AST-walks `ralph_py/` and fails on any unprotected prompt.

The engineer prompt template (`init_cmd.DEFAULT_PROMPT`, used to scaffold per-project `scripts/ralph/prompt.md`) is also enrolled, not just the four LLM-driven role prompts.

The audit trail is the PR diff with prompt body + version constant + snapshot tuple all moving together. That is what makes the H2 calibration step a real gate rather than a polite suggestion. H3 cannot prevent a determined developer from leaving the version pinned while updating both hash and snapshot to the *previous* version number; that bypass requires explicit deception in the snapshot file and is the irreducible limit of code-side enforcement.

H4: when reporting "tested" or "verified", be explicit about what was checked vs. what was assumed. Smoke tests are presence checks; calibration is behavior verification.
