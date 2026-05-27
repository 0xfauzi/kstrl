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
- [ ] F4 - User invokes `/code-review ultra` on hardening PRs (#37, #38) and any future implementation PRs. Documented in run log; cannot be me per H1.
- [ ] F5 - Calibration baseline (`RALPH_RUN_CALIBRATION=1 uv run pytest tests/test_calibration.py`) — deferred to user invocation. F2 architect data point recorded: 7/8 = 87.5% on the file-upload spec.

Status: F1+F3 done with documented Phase F validation. F2 partial (decompose only). F4+F5 are user-driven.

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
- [~] E3 - Structured `comp.findings: list[Finding]` - DEFERRED to follow-up PR. Today review/security findings funnel into the stringly-typed `comp.review_findings`. A real typed `Finding` requires changing PR creation paths and breaking serialization compat. Scope is one focused PR; tracked here for visibility.
- [x] E4 - `FactoryConfig.max_adversarial_calls` shared counter; review/security/distill all consult it; 0 = unbounded (default)
- [x] E5 - Confidence rename: `verified` -> `review_passed`, new `test_verified` tier added. Legacy `verified` aliased on read for backward compat.
- [x] E6 - `FactoryConfig.pause_before_pr_merge` HITL checkpoint. Prompts user before push+merge when UI is interactive; warns and proceeds when non-interactive.
- [~] E7 - Feedforward/knowledge overlap doc - DEFERRED to Phase G (documentation). The dedupe analysis itself is small; addressed there.
- [~] E8 - Fact scope by import surface - DEFERRED to follow-up PR. Today every transitive dependency's facts get injected; filtering by import surface needs Component-level import metadata that the manifest doesn't carry yet.
- [x] E9 - ReviewResult.infrastructure_error added (parallel to SecurityResult.infrastructure_error); parse failures set it; downstream can distinguish "clean review" from "review never ran"

Status: 5 of 8 items shipped (E2/E4/E5/E6/E9). E3 + E8 deferred with rationale in tracker; E1 permanently skipped per user; E7 folded into Phase G. 10 new tests; full suite 585 passing.

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
- [~] H3 - Prompt-versioning policy: today prompts are module-level constants edited under PR review. A versioning policy beyond that (e.g. per-prompt version field, semantic-versioned prompt files) is deferred — would be a separate cross-cutting change.
- [x] H4 - Adopted 2026-05-27. Claim discipline codified in CLAUDE.md "What NOT to do" and docs/adversarial-design.md "Process" section: be explicit about checked vs assumed; smoke tests are presence checks, not behavior checks.
- [ ] H5 - User-driven: user runs `/code-review ultra` retroactively on PRs #35, #36 and the seven hardening PRs #37-#43. Not something the assistant can do (H1).

Status: 3 policies fully adopted, 1 partial (H3 deferred), 1 pending user action (H5).

---

## Phase results

_Filled in as each phase completes._

### Calibration baselines

_`tests/adversarial_fixtures/_results/` will accumulate per-date JSON reports here once Phase D is built._
