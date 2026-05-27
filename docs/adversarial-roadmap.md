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

PR: _pending push_

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

PR: _pending_

- [ ] D1 - 5 security fixtures (SQL/command injection, hardcoded secret, predictable token, broken JWT verify)
- [ ] D2 - 3 reviewer-concern fixtures (dead code, tautological test, scope creep)
- [ ] D3 - 3 vague-spec fixtures (no error handling, unspecified auth, ambiguous perf)
- [ ] D4 - Calibration runner (`tests/test_calibration.py`) using Haiku-class fast model; per-role detection rate report
- [ ] D5 - OWASP/CWE taxonomy map for security categories; drop made-up ones
- [ ] D6 - Fact-utilization metric: instrument factory to log when downstream agents reference injected facts
- [ ] D7 - Remove or replace `exhaustively_searched` with verifiable evidence-cite requirement
- [ ] D8 - Concern hit-rate report from evolution.jsonl

Done when: each role has a published detection rate against fixtures, regenerable via `uv run pytest tests/test_calibration.py`.

---

## Phase F - Real-world validation

PR: _pending_

- [ ] F1 - Write `examples/file-upload-spec.md` with natural concerns (path traversal, file-type validation, size limits, races)
- [ ] F2 - Run the full factory against the spec, all phases enabled
- [ ] F3 - Capture and document the factory output
- [ ] F4 - User invokes `/code-review ultra` on resulting PRs (cannot be me; tracker note)
- [ ] F5 - Compare new vs prior factory on Phase D fixtures; quantify detection delta

Done when: tracker contains documented evidence the new factory catches things the prior version missed.

---

## Phase B - Complete TOML loader

PR: _pending_

- [ ] B1 - `FactoryConfig.load()` reading `[factory]`
- [ ] B2 - `VerifyConfig.load()` reading `[verify]`
- [ ] B3 - `ContractConfig.load()` reading `[contract]`
- [ ] B4 - `FeedforwardConfig.load()` reading `[feedforward]`
- [ ] B5 - `EvolutionConfig.load()` reading `[evolution]`
- [ ] B6 - `SecurityConfig.load()` reading `[security]`
- [ ] B7 - Extract shared `_TomlSectionLoader` mixin in `config.py`
- [ ] B8 - Enum-string validation across every `from_env`/`from_toml` (typo raises, not silently defaults)

Done when: every section of `ralph.toml.example` has an observable runtime effect, asserted by one integration test per section.

---

## Phase C - Test coverage gaps

PR: _pending_

- [ ] C1 - Multi-component parallel factory (`max_parallel=2`) via real ProcessPoolExecutor
- [ ] C2 - Phase 2 retry path test (reviewer rejects -> agent retries -> reviewer accepts)
- [ ] C3 - Phase 2.5 retry path test
- [ ] C4 - Phase 3 contract testing integration
- [ ] C5 - Single-PR mode integration test (after A2)
- [ ] C6 - Concurrent factory invocation test (after A4)
- [ ] C7 - Crash recovery (`RUNNING`/`VERIFYING` reset path)
- [ ] C8 - Pickling regression for all config dataclasses
- [ ] C9 - CLI matrix: claude / codex (multi model) / CustomAgent stub
- [ ] C10 - Windows handling: either CI on Windows or "Linux/macOS only" markers + skip decorators

Done when: each named code path has at least one test that exercises it end-to-end.

---

## Phase E - Architectural refinements (minus E1)

PR: _pending_

- [-] E1 - Multi-model rotation - SKIPPED per user decision; documented as known limitation
- [ ] E2 - Strip Self-Critique from reviewer-visible diff (or invert ordering)
- [ ] E3 - Structured `comp.findings: list[Finding]` replacing stringly-typed `comp.review_findings`
- [ ] E4 - Per-run LLM budget cap in `FactoryConfig`; enforced via shared counter in `agents`
- [ ] E5 - Rename `confidence: verified` -> `review_passed`; add `test_verified` tier
- [ ] E6 - Configurable HITL checkpoint (`FactoryConfig.pause_before_pr_merge`, opt-in)
- [ ] E7 - Feedforward/knowledge overlap doc + dedupe
- [ ] E8 - Fact scope by import surface
- [ ] E9 - Standardize `infrastructure_error` / `Result[T, E]` across all roles

Done when: each refinement lands with tests; rationale captured in `docs/adversarial-design.md`.

---

## Phase G - Documentation

PR: _pending_

- [ ] G1 - Phase diagram in `README.md`
- [ ] G2 - Role taxonomy in `CLAUDE.md`
- [ ] G3 - Single env-var reference: `docs/env-vars.md`
- [ ] G4 - `ralph.toml.example` with every section (depends on B)
- [ ] G5 - Replace stale plan file (point to this roadmap instead)
- [ ] G6 - `docs/adversarial-design.md` (why each role, known limitations)
- [ ] G7 - `docs/runbook.md` for operator phase-failure recovery

Done when: every feature shipped in #35, #36, and this hardening cycle has user-facing docs.

---

## Phase H - Process (non-code; adopt as policies)

- [ ] H1 - Policy: I do NOT self-review my own code. User invokes `/code-review ultra` or reads diffs directly.
- [ ] H2 - Calibration runs on every prompt change (CI hook once D lands)
- [ ] H3 - Prompt-versioning policy (PR + human approval or operator-tunable?)
- [ ] H4 - "Verified" claim discipline: state what I actually checked vs assumed
- [ ] H5 - Independent review of merged PRs #35, #36 (user runs `/code-review ultra` retroactively)

Done when: tracker captures each policy with the date adopted; CI integration where applicable.

---

## Phase results

_Filled in as each phase completes._

### Calibration baselines

_`tests/adversarial_fixtures/_results/` will accumulate per-date JSON reports here once Phase D is built._
