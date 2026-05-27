# Adversarial Factory Hardening Roadmap

Durable tracker for the 42-item hardening cycle that follows PRs #35 and #36.
Strategy doc: `~/.claude/plans/zazzy-orbiting-sketch.md`.

Status legend: `[ ]` pending - `[~]` in progress - `[x]` done - `[-]` skipped.

User decisions (locked):

1. Correlated-failure across adversarial roles accepted as known limitation. Multi-model rotation (E1) is OUT of scope.
2. HITL checkpoint (E6) shipped as opt-in, off by default.
3. Planted-bug fixtures (Phase D) use a small/quick real set: ~11 fixtures total.
4. Real-world validation target (F1): I pick - file-upload service spec.

Execution order: A -> D -> F -> B -> C -> E -> G. H is non-code, captured as policies as they're adopted.

---

## Phase A - Critical correctness fixes

PR: [#37](https://github.com/0xfauzi/ralph-loop/pull/37) merged 2026-05-27

- [x] A1 - Sanitize knowledge facts against prompt injection (`_is_injection_attempt` in `knowledge.py`, MAX_CLAIM_LENGTH=500, MAX_EVIDENCE_ITEMS=10, MAX_TAG_ITEMS=8 + 9 tests)
- [x] A2 - Single-PR mode skips knowledge distillation with a warning (`factory.py::_handle_result` + integration test)
- [x] A3 - Sub-second run_id collision: `secrets.token_hex(3)` nonce in both `factory.py::run_factory` and `knowledge.py::current_run_id` + collision test
- [x] A4 - Concurrent worktree clobber: per-host `fcntl.flock` on `.ralph/worktrees/<id>.lock`, POSIX only
- [x] A5 - Stream size cap: `collect_agent_output` helper with 5MB ceiling, wired into knowledge / security / review / decompose + 2 tests
- [x] A6 - Self-Critique heading regex tightened (H2/H3 or `- **...**` only), fuzz corpus of accepted + rejected lines
- [x] A7 - Codex contract test: structural tests + skipped live tests for `--output-last-message`

Status: 523 tests passing (21 new). Ready to commit and push.

Done when: PR merged.

---

## Phase D - Planted-bug fixtures + calibration runner

PR: _pending push_

- [x] D1 - 5 security fixtures (SQL injection, command injection, hardcoded secret, predictable token via random.random, broken JWT verify)
- [x] D2 - 3 reviewer-concern fixtures (dead code, tautological test, scope creep)
- [x] D3 - 3 vague-spec fixtures (no error handling, unspecified auth, ambiguous perf)
- [x] D4 - Calibration runner `tests/test_calibration.py` opt-in via `RALPH_RUN_CALIBRATION=1`; structural sanity always runs; per-role detection-rate JSON report at `_results/baseline-<date>.json`
- [x] D5 - `SECURITY_CATEGORY_MAP` in security.py maps every category to its OWASP Top 10 bucket + CWE, with helpers `category_owasp` / `category_cwe`
- [x] D6 - `knowledge.measure_fact_utilization` instruments factory.py post-distill: counts referenced facts via case-insensitive 30-char substring match against shared_diff + progress.txt
- [x] D7 - `exhaustively_searched` docstring updated to mark it as an unverifiable self-report; calibration suite is the trustworthy verification path
- [x] D8 - `EvolutionJournal.get_concern_hit_rate` aggregates concerns across recent runs by category

Status: 538 tests passing (+15 D6/D8/structural). 11 calibration tests skipped pending `RALPH_RUN_CALIBRATION=1`. Infrastructure ready; baseline measurement to be captured by the user.

Done when: PR merged.

---

## Phase F - Real-world validation

PR: _pending push_

- [x] F1 - `examples/file-upload-spec.md` — 4 functional + 5 non-functional requirements, 8 planted concerns spanning path traversal / content-type trust / 413 streaming / cursor probing / soft-delete cleanup / filename header / TOCTOU / alg=none
- [~] F2 - Decompose phase only. Full implementation pass (Phase 1-3 across 6 components) skipped to bound LLM cost; documented in `docs/phase-f-run-log.md`. User can invoke when ready.
- [x] F3 - Captured at `docs/phase-f-run-log.md` — architect found **7 of 8 planted spec issues (87.5%)** plus 4 extra defensible findings, zero hallucinated. Components decomposed: config, jwt-auth, metadata-db, upload-endpoint, download-endpoint, delete-endpoint.
- [x] F4 - Ultra-review command list shipped at `docs/f4-ultra-review-commands.md` listing PRs #35-#43 plus this deferred-follow-up PR. User invokes the commands; per H1 the assistant cannot.
- [x] F5 - Calibration baseline captured 2026-05-27 against Haiku. Security 5/5, reviewer 3/3, architect 2/3 (one missed kind classification — see `docs/f5-calibration-baseline.md`). Surfaced a real bug in the calibration runner (used `finding.evidence` instead of `finding.location`); fix included.

Status: All Phase F items now shipped or documented. F1+F3 done with Phase F validation. F2 partial (decompose only). F4 + F5 closed as deferred follow-ups.

Done when: tracker contains documented evidence the new factory catches things the prior version missed.

---

## Phase B - Complete TOML loader

PR: _pending push_

- [x] B1 - `FactoryConfig.load()` reads `[factory]` (max_parallel, max_retries, retry_delay, use_worktrees, single_pr, create_prs, review_mode)
- [x] B2 - `VerifyConfig.load()` reads `[verify]` including the new require_self_critique fields
- [x] B3 - `ContractConfig.load()` reads `[contract]`; `__post_init__` validates mode
- [x] B4 - `FeedforwardConfig.load()` reads `[feedforward]` with env overrides
- [x] B5 - `EvolutionConfig.load()` reads `[evolution]` and resolves relative journal/experiment paths against root_dir
- [x] B6 - `SecurityConfig.load()` reads `[security]`; existing __post_init__ validates mode + threshold from both toml and env paths
- [x] B7 - Shared `load_toml_section(toml_path, section)` helper in `config.py` reused by every loader; raises ValueError uniformly on malformed TOML
- [x] B8 - Enum validation: ContractConfig and SecurityConfig validate in __post_init__ (the two configs with enum-typed fields). 6 validation tests (3 toml + 3 env) prove typos raise.

Status: 19 new tests across the 6 loaders + cross-cutting malformed-toml parametrized test. Full suite 557 passing.

Done when: PR merged.

---

## Phase C - Test coverage gaps

PR: _pending push_

- [x] C1 - Parallel ProcessPoolExecutor entry covered (`TestC1ParallelExecution` in test_phase_c_coverage.py)
- [x] C2 - Phase 2 reviewer-retry path: review fails then passes; component completes after retry
- [x] C3 - Phase 2.5 security-retry path
- [x] C4 - Phase 3 contract tier-breaker resets the breaker component for re-run
- [x] C5 - single_pr mode integration: distill_facts skipped, run completes
- [x] C6 - Concurrent factory invocation in two threads; Windows-skipped (POSIX flock only)
- [x] C7 - Already covered by `test_factory.py::test_crash_recovery_resets_running` / `_resets_verifying`; tracker just confirms coverage
- [x] C8 - Pickling round-trip parametrized across all 8 config dataclasses (Ralph/Factory/Verify/Contract/Feedforward/Evolution/Knowledge/Security)
- [x] C9 - Agent factory matrix: CustomAgent / Codex / Claude / auto-detect
- [x] C10 - Windows skip markers applied where POSIX features are used

Status: 18 new tests. Full suite 575 passing, 11 skipped (calibration suite).

Done when: PR merged.

---

## Phase E - Architectural refinements (minus E1)

PR (partial): _pending push_

- [-] E1 - Multi-model rotation - SKIPPED per user decision; documented as known limitation
- [x] E2 - Strip Self-Critique block from reviewer-visible diff via `git.strip_self_critique_from_diff`; review.py invokes it before truncation
- [x] E3 - Typed `Finding` dataclass at `ralph_py/findings.py`. `Component.findings: list[Finding]` is the new source of truth, `Component.review_findings` (string) is a derived view kept for backward compat. `ReviewResult.as_findings()` converts criteria failures + concerns; `SecurityResult.as_findings()` adds OWASP/CWE taxonomy. Factory wires both at the existing phase 2 / 2.5 attachment points. Manifest.json roundtrip handled; legacy manifests without `findings` load as `[]`. 15 new tests; full suite 613 passing.
- [x] E4 - `FactoryConfig.max_adversarial_calls` shared counter; review/security/distill all consult it; 0 = unbounded (default)
- [x] E5 - Confidence rename: `verified` -> `review_passed`, new `test_verified` tier added. Legacy `verified` aliased on read for backward compat.
- [x] E6 - `FactoryConfig.pause_before_pr_merge` HITL checkpoint. Prompts user before push+merge when UI is interactive; warns and proceeds when non-interactive.
- [~] E7 - Feedforward/knowledge overlap doc - DEFERRED to Phase G (documentation). The dedupe analysis itself is small; addressed there.
- [x] E8 - `KnowledgeConfig.dependency_scope: str = "direct"` (default) restricts the Dependencies full-text tier to `Component.dependencies` only. Transitive deps still appear in the sibling first-sentence summary tier (downgraded, not hidden). The old behavior is opt-in via `dependency_scope = "transitive"` or `RALPH_KNOWLEDGE_DEPENDENCY_SCOPE=transitive`. 5 new tests in `test_knowledge.py`.
- [x] E9 - ReviewResult.infrastructure_error added (parallel to SecurityResult.infrastructure_error); parse failures set it; downstream can distinguish "clean review" from "review never ran"

Status: 7 of 8 items shipped (E2/E3/E4/E5/E6/E8/E9). E1 permanently skipped per user; E7 folded into Phase G. Deferred-follow-up PRs (E3, E8) merged 2026-05-27. 25+ new tests across the E-series.

Done when: PR merged. Deferred items tracked here for the next iteration.

---

## Phase G - Documentation

PR: _pending push_

- [x] G1 - README.md phase diagram extended to show all 8 roles (architect, engineer, P1 mechanical, P2 reviewer, P2.5 security, HITL, knowledge distiller, P3 contract)
- [x] G2 - CLAUDE.md created at repo root with role taxonomy and H1-H4 process rules
- [x] G3 - docs/env-vars.md - single canonical env-var reference across every config
- [x] G4 - ralph.toml.example extended with [factory] [verify] [security] [contract] [feedforward] [evolution] sections
- [x] G5 - ~/.claude/plans/zazzy-orbiting-sketch.md marked SUPERSEDED; points to this tracker
- [x] G6 - docs/adversarial-design.md - role taxonomy, pipeline, invariants, known limitations, E7 feedforward-vs-knowledge clarification
- [x] G7 - docs/runbook.md - operator recovery procedures for every named failure mode

Done when: PR merged.

---

## Phase H - Process (non-code; adopted as policies)

- [x] H1 - Adopted 2026-05-27. Assistant does NOT self-review its own code. PRs #37-#43 in this hardening cycle all merged without `/code-review`; user is the gating reviewer via `/code-review ultra` or direct diff inspection. Codified in CLAUDE.md "What NOT to do".
- [x] H2 - Adopted 2026-05-27. Calibration suite (`tests/test_calibration.py`) is the verification path for prompt changes. CI hook still TBD (env var gating is the manual-trigger mechanism today). Codified in CLAUDE.md.
- [x] H3 - Adopted 2026-05-27. Every adversarial prompt (`DECOMPOSE_PROMPT`, `REVIEWER_PROMPT`, `SECURITY_PROMPT`, `DISTILL_PROMPT`) now declares a `*_PROMPT_VERSION` semver constant. `tests/test_prompt_versions.py` snapshots the SHA-256 of each prompt; drift fails the suite and forces a version bump + hash update + recalibration. The two-file diff (prompt + hash) is the audit trail.
- [x] H4 - Adopted 2026-05-27. Claim discipline codified in CLAUDE.md "What NOT to do" and docs/adversarial-design.md "Process" section: be explicit about checked vs assumed; smoke tests are presence checks, not behavior checks.
- [ ] H5 - User-driven: user runs `/code-review ultra` retroactively on PRs #35, #36 and the seven hardening PRs #37-#43. Not something the assistant can do (H1).

Status: 3 policies fully adopted, 1 partial (H3 deferred), 1 pending user action (H5).

---

## Phase results

_Filled in as each phase completes._

### Calibration baselines

First baseline captured 2026-05-27 against Haiku:

- Security: 5/5 (100%)
- Reviewer: 3/3 (100%)
- Architect: 2/3 (67%) - haiku conflated `undefined_failure_mode` with `missing_detail` on one spec; full breakdown in `docs/f5-calibration-baseline.md`

Raw: `tests/adversarial_fixtures/_results/baseline-20260527-161822.json`
