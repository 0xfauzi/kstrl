# Ralph Operator Runbook

Recovery procedures for the failure modes that actually happen during factory runs.

## Phase 1: mechanical verification failed

**Symptom**: `Phase 1 FAILED for <comp_id>: <check_names>`

**Diagnose**:

- `prd_stories`: the agent never set `passes: true` on its assigned story. Either the iteration ran out, or the agent didn't understand the PRD.
- `test_suite`, `typecheck`, `linter`: the project's commands failed. Check the worktree at `.ralph/worktrees/<comp_id>/` and rerun the command manually.
- `diff_scope`: the agent wrote files outside `ALLOWED_PATHS`. Tighten the allowlist or relax it as appropriate.
- `bad_patterns`: a secret-like pattern landed in the diff.
- `dead_code` / `mutation`: the optional advanced checks failed.
- `self_critique`: the engineer prompt's self-critique block is missing, too short, or filled with placeholder content.

**Resolve**: the agent retries automatically up to `FactoryConfig.max_retries` (default 3). After that the component is marked FAILED and cascade-skips dependents. Manual options:

1. Edit the PRD to clarify the story; re-run.
2. Increase `--max-retries`.
3. Run the agent loop manually against the worktree to debug interactively.

## Phase 2: review failed (hard mode)

**Symptom**: `Phase 2 FAILED for <comp_id>: N failures`

**Diagnose**:

- Inspect `comp.review_findings` (also written to the PR body when the PR gets created).
- If the failures are PRD-criterion failures, the diff genuinely does not implement what was asked.
- If the failures are concerns (`scope_creep`, `security_concern`, `test_quality`, `unrelated_change`, `dead_code`, `error_handling`, `copy_paste`), the reviewer surfaced cross-cutting issues.

**Resolve**: the retry path injects the review findings back into the agent's context so the implementer has a concrete checklist. If the reviewer is wrong, switch the run to `--review-mode advisory` and the failures become warnings.

If `ReviewResult.infrastructure_error=True`, the reviewer agent itself failed (timeout, API outage, parse error). Same retry path, but check API health.

## Phase 2.5: security review failed (hard mode)

**Symptom**: `Phase 2.5 FAILED for <comp_id>: N critical, M high`

**Diagnose**: same logic as Phase 2, but the findings are typed against the security taxonomy. Each finding has `category`, `severity`, `location`, `explanation`, `suggestion`.

**Resolve**:

- For genuine security issues, the retry context goes back to the agent.
- For false positives, switch to `--security-mode advisory` (findings logged, not blocking) or `--security-fail-threshold critical` (only critical findings block).
- If `infrastructure_error=True`, the security reviewer didn't actually run. In hard mode this fails the component; in advisory mode it passes with a warning.

## Phase 3: contract test breaker

**Symptom**: `Contract breaker '<comp_id>' sent back for retry`

**Diagnose**: the merged tier branch's tests failed; Phase 3 attributes the failure to a "breaker" component (the most recent one merged into that tier). The breaker gets reset to PENDING and re-runs.

**Resolve**: the system handles this automatically up to `max_retries`. If it keeps breaking, the integration is genuinely broken — inspect the merged tier branch, fix the spec or the components' contracts, re-run.

## Knowledge layer reports `no_valid_facts`

**Symptom**: `Knowledge: knowledge.no_valid_facts (raw: ...)`

**Diagnose**: the distiller LLM returned output, the JSON parsed, but `_coerce_facts` rejected every fact. Common causes:

- Fact ids don't match `/^fact-\d{3}$/` (e.g. `fact-1` instead of `fact-001`)
- Unknown scope value (the agent invented categories beyond handler/adapter/schema/contract/invariant/gotcha)
- Empty evidence array
- Empty claim text
- Prompt-injection pattern matched in claim text (Phase A1 rejection)

**Resolve**:

- Inspect `.ralph/knowledge/<comp_id>/<run_id>/_distill_raw.txt` (saved automatically on failure paths) to see the agent's actual output.
- If the agent consistently produces malformed output, the distill prompt may need to be tightened.
- If the failure is `no_facts` (not `no_valid_facts`), the JSON didn't parse at all; usually means the agent emitted prose around the JSON.

## Concurrent factory runs clobbering each other

**Symptom**: One run's worktree disappears or its branch gets force-pushed by the other.

**Diagnose**: on POSIX, Phase A4's `fcntl.flock` on `.ralph/worktrees/<comp_id>.lock` should prevent this. On Windows there is no flock and the runs race.

**Resolve**: avoid running concurrent factory invocations against the same `root_dir` on Windows. On POSIX, the lock serializes worktree setup but doesn't prevent two runs from doing different work on the same component — use distinct `root_dir`s for distinct factory invocations.

## Adversarial budget exhausted mid-run

**Symptom**: `Phase 2 SKIPPED for <comp_id>: adversarial LLM budget exhausted`

**Diagnose**: `FactoryConfig.max_adversarial_calls` is set and the count of review + security + distillation calls has hit the cap.

**Resolve**: increase the cap, or accept that later components run without adversarial phases. The mechanical pipeline (Phase 1) still gates them.

## Spec was rejected by the architect

**Symptom**: factory exits with code 2; stderr lists `[blocker/<kind>] <summary>` lines.

**Diagnose**: the architect's red-team pass found blocker-severity issues. The pipeline halts rather than implementing against a vague spec.

**Resolve**: read the surfaced issues, edit the spec to address them, re-run. There is no override flag; that's deliberate — the alternative was producing brittle code from ambiguous instructions.

## Calibration suite reports a regression

**Symptom**: `tests/test_calibration.py` test fails after a prompt edit; detection rate dropped.

**Diagnose**: the prompt change made the role miss a planted bug it previously caught.

**Resolve**: either revert the prompt change or update the fixture's `must_detect` if the change deliberately narrowed scope. Do not just unskip the test — a calibration regression is the signal you wrote the system to produce.

## Where to find things

- Tracker for the hardening roadmap: `docs/adversarial-roadmap.md`
- Adversarial design overview: `docs/adversarial-design.md`
- Env-var reference: `docs/env-vars.md`
- Per-run captures: `.ralph/evolution.jsonl`, `.ralph/experiments.tsv`
- Distillation debug dumps: `.ralph/knowledge/<comp>/<run>/_distill_raw.txt` (on failure)
- Phase F sample real-world run log: `docs/phase-f-run-log.md`
