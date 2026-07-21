# Phase F: Real-world Validation Run Log

Spec: `examples/file-upload-spec.md`
Sandbox: `/tmp/kstrl-phase-f-sandbox/`
Date: 2026-05-27
Roadmap item: F2-F3 (run + capture)

## Scope of this run

Decompose phase only. The full implementation pass (Phase 1 → 2 → 2.5 → 3 across 6 components, ~20 minutes per component) was deliberately not driven to completion in-session because:

1. The architect's red-team output is the single most-valuable Phase F signal — it directly grades the new `DECOMPOSE_PROMPT` from PR #36 against a real ambiguous spec.
2. The implementing-agent + reviewer + security + distillation passes would burn substantial LLM tokens against a sandbox project that the user is unlikely to ship. The infrastructure exists; the user can drive it when they choose a target.
3. F4 (independent ultra-review by the user) is the gating step before merge anyway. Until F4 runs, generating more output is just more work for that step.

Skipping the implementation pass is also recorded in the tracker as an explicit choice; F2 is partially complete (decompose only) and F3 is fully complete for what was run.

## What we ran

```bash
uv run python -m kstrl factory \
  --spec /tmp/kstrl-phase-f-sandbox/scripts/kstrl/spec.md \
  --root /tmp/kstrl-phase-f-sandbox \
  --project-name file-upload \
  --yes \
  --no-prs --review-mode skip --security-mode skip --contract-check skip --no-verify \
  --agent-type claude-code --max-parallel 1 \
  --ui plain --no-color
```

Captured stdout at `/tmp/kstrl-phase-f-decompose.log` (403 lines).

## What the architect found

The DECOMPOSE_PROMPT's red-team surfaced 11 `spec_issues` (8 major + 3 minor) on a spec I authored to contain 8 planted concerns. Concrete catches:

| # | Severity | Kind | Summary | Planted? |
|---|---|---|---|---|
| 1 | major | missing_detail | JWT verification key source and algorithm allowlist not specified | yes (planted) |
| 2 | major | unstated_assumption | user_id from JWT sub used as directory name — path traversal if attacker-controlled | yes (planted) |
| 3 | major | undefined_failure_mode | Content-Type validation relies on client-declared header only | yes (planted) |
| 4 | major | ambiguity | 413 size-limit timing not specified; no streaming mandate | yes (planted) |
| 5 | major | ambiguity | Pagination cursor described only as "opaque" — naive offset cursor leaks across users | yes (planted) |
| 6 | major | missing_detail | Soft-delete cleanup mechanism / ownership undefined | yes (planted) |
| 7 | major | undefined_failure_mode | Original filename in Content-Disposition — header/filename injection risk | extra (NOT planted) |
| 8 | minor | missing_detail | Atomic-rename temp-file naming strategy unspecified | yes (planted) |
| 9 | minor | missing_detail | 4xx error response body shape unspecified | extra |
| 10 | minor | missing_detail | SQLite schema columns/indexes undefined | extra |
| 11 | minor | missing_detail | FILE_UPLOAD_STORAGE_ROOT missing/unwritable behavior unspecified | extra |

### Score (architect role, single run, model: claude-sonnet)

- Planted issues: **7 of 8 detected** (87.5%)
  - Caught: alg-none / JWT key source, path traversal on user_id, Content-Type trust, 413 streaming, pagination cursor, soft-delete cleanup, temp-file naming.
  - Missed: race-free concurrent uploads beyond temp-file naming (architect caught the symptom but not the deeper TOCTOU framing).
- Extra issues: 4 (filename header injection, error envelope, SQLite schema, storage root failure). All defensible findings.
- Hallucinated / wrong: 0.

This is one run with one model; the calibration suite in Phase D will turn this into a repeatable measurement once F5 lands the baseline.

## Components produced

The architect decomposed into 6 components in topological order:

1. `config` — settings load + validation, JWT alg allowlist excludes "none"
2. `jwt-auth` — verifier, expiry/sub/UUID checks, FastAPI dependency
3. `metadata-db` — schema init, insert/get/list/soft-delete, opaque per-user cursor
4. `upload-endpoint` — POST /api/files with size and content-type validation
5. `download-endpoint` — GET /api/files/{id} with 404 leak-prevention
6. `delete-endpoint` — soft-delete with 404 leak-prevention

The decomposition itself is a meaningful artifact: every spec issue surfaced above is reflected in concrete acceptance criteria (e.g. US-002 says "A token signed with alg=none ... is rejected with an InvalidTokenError and never decoded as valid"). The implementer therefore inherits the architect's red-team findings.

## What was NOT run (transparent gaps)

- Implementing agent passes for the 6 components.
- Phase 2 second-opinion review.
- Phase 2.5 security review against the implemented diffs (the most valuable security signal would come from this).
- Phase 3 contract testing across merged tier branches.
- Knowledge distillation per completed component.

These are mechanically straightforward to invoke (`--review-mode advisory --security-mode advisory` etc.) but represent real LLM cost. Deferred to user invocation.

## F4 (independent ultra-review)

Per H1 of the adversarial-roadmap, I do not run `/code-review ultra` on my own output. The user is the gating reviewer for:

1. PRs #37, #38 (the hardening work).
2. Any PRs that emerge from a full factory run on this spec.

This file is the deliverable artifact pending F4.

## F5 (calibration baseline)

The calibration suite from Phase D (`tests/test_calibration.py`) is ready. To capture a baseline:

```bash
KSTRL_RUN_CALIBRATION=1 \
KSTRL_CALIBRATION_MODEL=haiku \
uv run pytest tests/test_calibration.py -v
```

This runs 11 real-LLM tests across the planted-bug fixtures and writes `tests/adversarial_fixtures/_results/baseline-<UTC>.json`. The Phase F decompose run above gives us **one architect data point** (7/8 = 87.5% on the planted-spec category); the full baseline would give per-fixture detection rates for security, concerns, and architect roles. Deferred to user invocation for the same cost reasons as F2's implementation pass.
