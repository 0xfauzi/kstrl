# F5 Calibration Baseline — 2026-05-27

First end-to-end calibration baseline against real LLM calls. Captures the
detection rate of each adversarial role on the planted-bug fixtures shipped in
Phase D.

## How to reproduce

```bash
RALPH_RUN_CALIBRATION=1 RALPH_CALIBRATION_MODEL=haiku uv run pytest tests/test_calibration.py -v
```

Cost on 2026-05-27 run: ~$0.10-0.50 in Haiku calls, 374s wall-clock for 11
fixtures plus 9 structural sanity checks.

## Results

Raw JSON: `tests/adversarial_fixtures/_results/baseline-20260527-161822.json`

| Role | Caught | Total | Rate |
|---|---|---|---|
| Security reviewer | 5 | 5 | **100%** |
| Code reviewer | 3 | 3 | **100%** |
| Architect (PRD red-team) | 2 | 3 | **67%** |

## Per-fixture breakdown

**Security (Phase 2.5)** — 5/5, all critical-severity, all locations match:
- `sec-01-sql-injection` → `critical injection at src/users.py:11-13`
- `sec-02-command-injection` → `critical injection at src/backup.py:10-14`
- `sec-03-hardcoded-secret` → `critical hardcoded_secret at src/config.py:6`
- `sec-04-predictable-token` → `critical predictable_randomness at src/auth.py:15`
- `sec-05-broken-jwt-verify` → `critical auth_bypass at src/jwt_verify.py:9-13`

**Reviewer (Phase 2)** — 3/3, all `fail` severity:
- `concern-01-dead-code` → `fail dead_code at src/sandbox/parser.py:15-31`
- `concern-02-tautological-test` → `fail test_quality at tests/test_calculator.py:6-8`
- `concern-03-scope-creep` → `fail scope_creep at src/sandbox/logger.py (entire file)`

**Architect (PRD red-team / Phase 0)** — 2/3:
- `spec-01-no-error-handling` — **missed** (see analysis below)
- `spec-02-unspecified-auth` — 14 issues found, includes `undefined_failure_mode`
- `spec-03-ambiguous-perf` — 15 issues found, includes `undefined_failure_mode`

## Analysis of the one miss

`spec-01-no-error-handling` was a "GET /users/{user_id}" spec with no behavior
specified for invalid/missing user IDs and no auth story. The fixture's
`must_include_kind` requires both `undefined_failure_mode` AND `missing_detail`
to be in the architect's output.

Haiku found **8 blocker-severity issues** on this spec — all of them legitimate
issue surfacing. But Haiku classified the "non-existent user_id" issue as
`missing_detail` instead of `undefined_failure_mode`, so the strict-subset
matcher failed.

This is a real calibration signal, not noise:

- **Haiku tends to over-use `missing_detail`** as a catch-all kind. The
  decompose prompt's taxonomy distinction between `missing_detail`
  ("information needed for implementation is absent") and
  `undefined_failure_mode` ("error/edge case behavior not specified") is
  apparently not robustly internalized by Haiku.
- The architect is **not failing to catch the issue** — it surfaced the
  underlying concern. It's failing to *classify* the issue under the prompt's
  specific kind. From a "did the spec get flagged?" perspective, the architect
  is 3/3.
- The fixture's strictness is a deliberate design choice: we want the
  taxonomy to be reliable, not just the issue-surfacing.

The 67% number is honest. Two ways to interpret it:
1. **Calibration-strict**: Haiku misses 1 of 3 — improvement target is
   tightening the kind taxonomy in the decompose prompt.
2. **Issue-surfacing**: Haiku catches 3 of 3 — the prompt produces actionable
   output, just with imprecise kinds.

H2 of the hardening roadmap says: if a prompt change moves this number,
calibration is the verification. Both interpretations should be tracked across
prompt edits.

## v1.1.0 architect re-run (DECOMPOSE_PROMPT version bump, 2026-05-27 19:13)

Re-ran the architect role against the 3 spec fixtures after bumping `DECOMPOSE_PROMPT` from `1.0.0` to `1.1.0` (added `allowedPaths` requirements). Per H2 a prompt change without a calibration delta is treated as untested.

Result: **2/3 = 67%** (same rate as v1.0.0). Different fixture missed:

- `spec-01-no-error-handling` -- caught this run (was missed in v1.0.0 baseline)
- `spec-02-unspecified-auth` -- missed this run (was caught in v1.0.0)
- `spec-03-ambiguous-perf` -- caught both runs

The missed-fixture rotation is within LLM run-to-run variance at single-run sample size. The bias documented for v1.0.0 (Haiku over-uses `missing_detail` and `ambiguity`, under-uses `undefined_failure_mode` / `unstated_assumption`) appears unchanged. The prompt change did not regress the spec-issue detection axis -- which is the only axis the current fixtures grade. Whether the architect now reliably emits `allowedPaths` for non-halting specs is a separate measurement that the existing calibration fixtures do not cover (they are all designed to halt the architect on planted blockers).

Raw: `tests/adversarial_fixtures/_results/baseline-20260527-191337.json`.

## v1.2.0 architect re-run with new allowedPaths fixture (2026-05-27 19:51)

Re-ran the architect after the DECOMPOSE_PROMPT bump to `1.2.0` (tightened wording on `allowedPaths` rule #12) and after adding a non-halting fixture `spec-04-clear-layout` that grades whether the architect emits sensible scopes per the rule.

Result:

| Suite | Caught | Total | Rate |
|---|---|---|---|
| Architect (halting, spec-issue detection) | 2 | 3 | 67% |
| Architect (non-halting, allowedPaths quality) | 1 | 1 | 100% |

The halting-fixture rate held at 67% across v1.0.0/v1.1.0/v1.2.0. The non-halting fixture is new in v1.2.0 and Haiku passed on first run, producing `['src/slugify/', 'tests/test_slugify.py', 'scripts/ralph/feature/slugify-.../']` -- excludes harness internals, includes test root, names the feature subtree. This is the first calibrated evidence that the rule actually constrains the architect, not just the prompt-instruction layer.

Raw: `tests/adversarial_fixtures/_results/baseline-20260527-195157.json`.

## Acknowledged limitation: single-run baselines are noisy signal

Every baseline in this document is one fixture-suite run against Haiku. With 3 halting fixtures and LLM-inherent variance, comparing rates across versions (`v1.0.0`: missed spec-01; `v1.1.0`: missed spec-02; `v1.2.0`: missed spec-02) is noisy at best -- the same fixture rotates in and out of the miss column on different runs. Aggregating 5+ runs would give confidence intervals.

The fixture-suite is also small. Three halting spec fixtures grade three distinct vagueness modes; one non-halting fixture grades the allowedPaths rule. Expanding the suite (more vagueness fixtures, more layouts to test allowedPaths emission, multi-component fixtures) is fixture-library work that would tighten the H2 verification path. Future calibration improvement.

## Trustworthy use of these numbers

- **Single-run baseline**. LLMs vary; aggregate across multiple runs before
  trusting any single rate as stable. The architect miss on spec-01 may or may
  not be reproducible.
- **Haiku-specific**. Running with `RALPH_CALIBRATION_MODEL=sonnet` (or
  `opus`) will probably catch more issues and classify them more precisely,
  but is also more expensive.
- **Fixture set is small** (5 + 3 + 3 = 11). Each role's rate has wide
  confidence intervals at this sample size. A 5/5 rate doesn't prove 100%
  recall, just that 5 specific bugs are caught.
- **No false-positive measurement**. The fixtures contain real planted bugs;
  there's no negative-control set of clean diffs where we'd expect *zero*
  findings. Adding a negative-control set is future work.

## Recommended next steps

1. Re-run with `RALPH_CALIBRATION_MODEL=sonnet` to see how the rates shift.
2. Expand the fixture library: more security categories (SSRF, deserialization,
   XSS), more reviewer concerns (copy_paste, error_handling), more vague specs.
3. Add a small negative-control set (clean diffs/specs) and assert the roles
   produce *no* findings. That measures false-positive rate.
4. Promote the calibration suite to a CI hook on `prompt.md` /
   `DECOMPOSE_PROMPT` / `REVIEWER_PROMPT` / `SECURITY_PROMPT` edits (H2).
