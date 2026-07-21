# Remediation Execution Plan: Copy/Paste Session Prompts

Companion to `docs/remediation-roadmap.md` (the WHAT). This file is the HOW:
one prompt per Claude Code session, grouped into waves you can run in
parallel. Paste a prompt into a fresh session, review the PR it produces,
merge, move on. Each session flips its own checkboxes in the roadmap tracker
inside its PR, so the tracker is always the single source of truth for
progress.

Review report (context for humans; sessions do not need it):
https://claude.ai/code/artifact/0996ac84-8acf-4000-a526-72d4b0994832

---

## How to run this

**Sequential (simplest):** open a Claude Code session at the repo root, paste
the next prompt, review the PR, merge, repeat. Follow wave order; within a
wave, order does not matter.

**Parallel (waves):** sessions within the same wave touch disjoint files (or
disjoint regions with trivial rebases). Run each parallel session in its own
worktree:

```bash
cd ~/Documents/code/kstrl
git fetch origin
git worktree add ../kstrl-<id> -b <branch-from-prompt> origin/main
cd ../kstrl-<id>
uv sync          # each worktree needs its own venv
claude           # then paste the session prompt
```

After the PR merges:

```bash
cd ~/Documents/code/kstrl
git worktree remove ../kstrl-<id>
```

**Merge protocol:** you are the gating reviewer for every PR (H1): use
`/code-review ultra <PR#>` or direct inspection. Merge a wave's PRs in the
order listed. Do not start the next wave until the current wave is merged
(prompts assume the previous wave's code exists).

**If a session goes sideways:** stop it, delete the branch, re-paste the
prompt in a fresh session. Prompts are self-contained; no session depends on
another session's chat context.

---

## Shared rules (every prompt references this section)

1. Read `CLAUDE.md`, then your items in `docs/remediation-roadmap.md`. They
   contain the finding details and file:line references. The line numbers were
   captured at main @ 8115636: treat them as pointers, verify against current
   code before editing.
2. Touch only the files your prompt names, plus tests and the two tracker
   docs. If you conclude another file must change, stop and report why
   instead of expanding scope.
3. Do NOT modify any `*_PROMPT` body, `*_PROMPT_VERSION` constant, or
   `tests/test_prompt_versions.py` snapshot. Only Session 8C (and 5A for
   `DEFAULT_PROMPT` only) may, following H2/H3. The snapshot test will catch
   violations.
4. Do NOT run `/code-review` (H1: the user is the gating reviewer).
5. Coding standards per CLAUDE.md: `from __future__ import annotations`,
   full type hints, `T | None`, frozen dataclasses where immutable, no bare
   except, no mutable defaults.
6. Before opening the PR, all three must be green and you must paste their
   final lines into the PR body:
   `uv run pytest tests/ -q` / `uv run mypy kstrl/ --strict` /
   `uv run ruff check kstrl/ tests/`
7. Rebase onto `origin/main` before pushing. Open the PR with `gh pr create`;
   do not merge it.
8. PR body must contain: roadmap item IDs, a "Tested" list (behaviors proven
   by named tests) and an "Assumed" list (anything claimed but not tested):
   H4 discipline.
9. In the same PR: flip your item checkboxes in
   `docs/remediation-roadmap.md` from `[ ]` to `[x]` (use `[~]` plus a note
   if partial).

---

## Wave map

| Wave | Sessions (merge order) | Parallel OK | Needs merged |
|---|---|---|---|
| 1 | 1A timeouts, 1B input hygiene, 1C knowledge, 1D suite isolation | all 4 | - |
| 2 | 2A PR outcomes, 2B contract safety, 2C worktree provisioning | all 3 (disjoint factory.py regions; rebase) | wave 1 |
| 3 | 3A instance safety, 3B reviewer gates, 3C scope hardening, 3D architect persistence | all 4 | wave 2 |
| 4 | 4A truncation policy, 4B config control plane, 4C spine tests I | all 3 | wave 3 |
| 5 | 5A run honesty, 5B HITL + env scrub, 5C test fixes + coverage | all 3 | wave 4 |
| 6 | 6A preflight + config show, 6B cost meter, 6C spine tests II | all 3 | wave 5 |
| 7 | 7A docs regeneration, 7B status + notify, 7C resume ergonomics | all 3 | wave 6 |
| 8 | 8A calibration tooling, 8D self-critique, then 8B fixtures, then 8C prompt batch | 8A+8D parallel; 8B after 8A; 8C last | wave 7 |
| 9 | 9A evolution repair | solo | wave 8 |
| 10 | 10A rotation*, 10B fixtures wiring, 10C refactor (solo), 10D Linear*, 10E platform | 10A+10B parallel; 10C strictly solo; 10D after 10C | wave 9 |

`*` = blocked on a user decision (see roadmap "User decisions required").

---

# Wave 1

## Session 1A: Timeouts end to end
Branch: `fix/r0-1-timeouts`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R0.1 in docs/remediation-roadmap.md. You are implementing R0.1: agent timeouts are currently configured everywhere and enforced nowhere.

Defects (verify line numbers against current code):
- loop.py:154 calls agent.run(prompt, cwd) with no timeout; config.py:62-63 (agent_iteration_timeout, component_timeout) and the entire timeout.py module are parsed but never consumed; cli.py binds --agent-timeout/--component-timeout for `ks factory` and never uses them.
- agents/codex.py:44-98 ignores its timeout parameter entirely and proc.wait() is unbounded; agents/claude_code.py:90-98 only checks the clock when a stdout line arrives, so a silently hung CLI blocks forever; agents/custom.py has the same class of problem.

Scope: kstrl/agents/*.py, kstrl/loop.py, kstrl/timeout.py, kstrl/config.py, kstrl/factory.py (scheduler backstop only), kstrl/cli.py (wire the two dead flags), tests.

Requirements:
1. All agent subprocesses launch with start_new_session=True. Read stdout on a reader thread with a deadline so a hang with NO output is detected. On breach: killpg(SIGTERM), grace period, then SIGKILL. All three adapters honor the timeout parameter. Bound codex proc.wait().
2. loop.py passes agent_iteration_timeout into agent.run and enforces component_timeout as a wall-clock check across iterations (abort the loop cleanly when exceeded, report which limit fired).
3. factory.py scheduler: backstop deadline of component_timeout plus margin per future; on breach mark the component FAILED with error "component timeout", warn a worker may be leaked, continue the run.
4. Consume TimeoutConfig as the single source for these values (loaded from toml/env); delete any now-dead duplicate fields rather than leaving two sources.
5. Timeout-failure retries must not reuse a possibly-dirty worktree state: remove stale .git/index.lock if present and note the recreate-from-base behavior in the retry error string.

Tests (real subprocesses, no LLM): a sleep-forever fake agent script is terminated within the deadline and the component is FAILED; a fake agent that spawns a grandchild (sh -c 'sleep 1000 & wait') has the grandchild killed too; a fake agent that hangs silently AFTER emitting one line is still killed.

Finish per Shared rules: green pytest/mypy/ruff, rebase, PR with Tested/Assumed lists referencing R0.1, flip the R0.1 checkbox in docs/remediation-roadmap.md in the same PR.
```

## Session 1B: Input hygiene for LLM-emitted identifiers
Branch: `fix/r0-6-id-hygiene`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R0.6 in docs/remediation-roadmap.md. You are implementing R0.6: LLM-emitted component ids and branch names flow unvalidated into filesystem paths and git argv.

Defects: decompose.py:384-390 and manifest.py:283-298 validate ids only as non-empty strings. An id like "../../repo" escapes .kstrl/worktrees/ and reaches `git worktree remove --force` at the escaped path (factory.py builds worktree_path = root/.kstrl/worktrees/<id>). Branch names reach `git push -u origin <branch>` and `git worktree add ... <branch>` with no leading-dash rejection and no `--` separators, so a crafted value becomes an argv option.

Scope: kstrl/manifest.py, kstrl/decompose.py, kstrl/git.py, kstrl/pr.py, tests.

Requirements:
1. Component ids must match ^[a-z0-9][a-z0-9._-]{0,63}$ (no slashes, no dots-only segments). Enforce at manifest parse AND decompose validation, with actionable error messages (the decompose retry loop should be able to feed the error back).
2. Branch names: reject leading '-', '..', whitespace, and ':'; allow the kstrl/factory/ prefix pattern.
3. Add `--` separators in git invocations where a ref or path follows options and could be attacker-shaped (survey git.py and pr.py call sites; change only where it is safe for the git subcommand in question).
4. Do not silently sanitize: reject with an error. Silent rewriting hides architect drift.

Tests: parametrized rejection tests (traversal ids, option-injection branch names, unicode confusables at least for the dash), plus acceptance tests for current legitimate ids/branches from examples and tests to prove no regression.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R0.6, flip the R0.6 checkbox in docs/remediation-roadmap.md.
```

## Session 1C: Knowledge retention + read-side defense
Branch: `fix/r1-6-knowledge`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R1.6 in docs/remediation-roadmap.md. You are implementing R1.6. IMPORTANT: do not edit DISTILL_PROMPT or any prompt body; every fix here is code-side.

Defects (knowledge.py):
- Retrieval reads only the lexicographically latest run dir (:282-294, :297-320). The distill prompt forbids re-emitting existing facts, so each successful re-distill HIDES all prior facts; and _dump_debug (:1013-1035) creates a fact-less newer run dir on parse failure, so a failed distill erases the component's entire visible knowledge.
- _coerce_facts (:907-911) runs the injection filter on claim and tags but NOT evidence; evidence has no length/count cap and _format_section renders it verbatim into downstream "treat as ground truth" prompts.
- _parse_fact_md (:231-274) validates nothing on read, so an edited-on-disk fact bypasses all filters.
- test_verified confidence is accepted with zero cross-checking.
- Same-second run dirs order by random nonce, so "latest" can be older.

Scope: kstrl/knowledge.py, tests/test_knowledge.py.

Requirements:
1. Retrieval becomes union-across-run-dirs with per-fact-id latest-wins (supersede by fact id, not by directory). Keep token budgeting semantics; re-check tier caps still hold with union reads.
2. Debug dumps move to <knowledge_root>/<comp>/_debug/<run_id>/ and are never globbed as facts.
3. Sanitize + cap evidence items at coercion (same _is_injection_attempt gate and explicit MAX lengths/counts as claims); ALSO re-validate claim/evidence/tags on read in _parse_fact_md, rejecting (with a warning, not a crash) any fact that fails.
4. test_verified: downgrade to "asserted" unless at least one evidence item cites a path that exists in the worktree; keep the hint framing in code comments.
5. Run-id: include microsecond precision so same-second ordering is deterministic.

Tests: the exact decay scenario (distill run 1 writes 7 facts, run 2 fails parse, read_facts still returns the 7); supersede-by-fact-id (run 2 re-emits fact A only; read returns run-2 A plus run-1 B); evidence-field injection payload rejected at write AND at read; test_verified downgrade behavior; microsecond ordering.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R1.6, flip the R1.6 checkbox in docs/remediation-roadmap.md.
```

## Session 1D: Test-suite isolation + repo hygiene
Branch: `fix/r4-1-isolation`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R4.1 and R3.4 in docs/remediation-roadmap.md. You are implementing suite isolation and repo hygiene.

Defects: the test suite writes to the repo's REAL .kstrl/evolution.jsonl and .kstrl/experiments.tsv: 837 of 910 journal entries are test pollution, corrupting the data the learning loop consumes. conftest's clean_env clears only legacy vars, not FACTORY_*/KSTRL_* families. The repo also carries a ~99MB stale worktree at .claude/worktrees/tender-leakey and .gitignore does not cover .kstrl/.

Scope: tests/conftest.py, .gitignore, .kstrl/ (archival move only), tests as needed. Do NOT touch kstrl/ source in this session.

Requirements:
1. Autouse conftest fixture that redirects every evolution/experiments/knowledge write path to tmp_path for all tests (env vars and/or monkeypatched defaults; inspect how EvolutionConfig and KnowledgeConfig resolve paths and cover both).
2. A guard fixture that snapshots the repo's .kstrl/ state before the session and FAILS the run loudly if any test mutated it. This is the enforcement, not the redirect.
3. Extend clean_env to the FACTORY_*, KSTRL_TIMEOUT_*, KSTRL_CONTRACT_*, KSTRL_SECURITY_*, KSTRL_VERIFY_*, KSTRL_EVOLUTION_*, KSTRL_KNOWLEDGE_* families so ambient dev-machine env cannot alter from_env tests.
4. Archive (do not delete) the polluted journals: move .kstrl/evolution.jsonl and .kstrl/experiments.tsv to .kstrl/archive/ with a README.md line explaining why.
5. Add .kstrl/ to .gitignore.
6. Stale worktree: run `git log claude/tender-leakey --not main --oneline`. If it shows NO unique commits, remove the worktree (git worktree remove --force .claude/worktrees/tender-leakey) and delete the branch, and say so in the PR. If it shows unique commits, do NOT remove anything: list them in the PR body for the user to decide.

Tests: a deliberate journal-writing test against defaults proves the redirect works; the guard fixture is exercised by a self-test.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R4.1 + R3.4 (mark R3.4 [~] since gitignore/archive land here but kstrl.toml tracking questions may remain), flip checkboxes accordingly.
```

---

# Wave 2 (after wave 1 merges)

## Session 2A: PR outcomes gate completion + fetch-based base propagation
Branch: `fix/r0-2-pr-outcomes`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R0.2 in docs/remediation-roadmap.md. You are implementing R0.2.

Defects:
- factory.py:887-915: the result of push_create_and_merge_pr is only used to collect a URL; push failure, PR-creation failure, merge failure, and wait_for_merge timeout ALL fall through to comp.status = COMPLETED, so dependents build without the dependency's merged code (CRIT-2).
- pr.py:149-152 runs `git pull origin <base>` with cwd=root_dir on WHATEVER branch the user has checked out: it mutates the operator's checkout and leaves local base stale (H-1).
- Every pr.py subprocess except the merge-wait loop lacks a timeout.

Scope: kstrl/pr.py, kstrl/factory.py (PR block + worktree base refs), kstrl/manifest.py (status enum), kstrl/git.py (diff base refs), tests.

Requirements:
1. pr.py returns a typed PrOutcome dataclass (pushed, pr_url, merged, merge_pending, error) instead of lossy tuples. Explicit timeouts on every gh/git call in the module.
2. factory: COMPLETED only when merged is True (or create_prs=False). New manifest status MERGE_PENDING for wait-timeout: dependents are NOT scheduled past it; crash-recovery treats MERGE_PENDING as re-pollable, not failed.
3. Kill the pull: replace with `git fetch origin <base>`; cut worktrees from origin/<base> and compute verification diffs against origin/<base> when a remote exists, falling back to the local base ref when there is no origin (local-only repos and the test suite must keep working). Centralize the "resolve base ref" logic in one helper.
4. The operator's checkout must never be touched by any code path in this session's scope.

Tests (real git repos, stub gh binary placed on PATH by the test): push-fail, create-fail, merge-fail, wait-timeout each produce the right status and dependents do not schedule; the fetch path updates origin/<base> without touching the checked-out branch; no-remote fallback works.

Finish per Shared rules: green checks, rebase (wave-2 sessions touch different factory.py regions; resolve any trivial conflicts), PR with Tested/Assumed referencing R0.2, flip the R0.2 checkbox.
```

## Session 2B: Contract phase safety
Branch: `fix/r0-3-contract`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R0.3 in docs/remediation-roadmap.md. You are implementing R0.3.

Defects:
- contract.py merges tier branches into a temp branch created IN THE USER'S REPO (root_dir). On merge conflict the cleanup path (git checkout base + branch -D, contract.py:110-118) fails against a conflicted index, both return codes are ignored, the repo is left mid-conflict, and every subsequent tier fails. There is no `git merge --abort` anywhere (CRIT-6).
- factory.py:1055-1082: when contract fails, the breaker is reset to PENDING AFTER the scheduling loop has exited, so the promised retry never runs; the failure is not recorded in factory_result.failed, so the run exits 0 with broken integrated code.
- Bisection blames the first component unconditionally when PRs were already squash-merged (merges no-op), and is order-dependent for interaction failures.

Scope: kstrl/contract.py, kstrl/factory.py (contract block + scheduling loop re-entry), tests.

Requirements:
1. All tier merging happens in a detached temp WORKTREE (git worktree add --detach), never the user's checkout. Recovery path: git merge --abort, then remove the temp worktree; assert cleanup succeeded and fail loudly if not.
2. Contract failure sets a nonzero exit code and lands in the run summary; record a contract_result event in the evolution journal for pass and fail.
3. Breaker retry actually re-enters scheduling: restructure so a reset breaker re-runs while contract retries remain (an outer loop around scheduling + contract, or equivalent). Cascade rules unchanged.
4. Bisection honesty: when components were already merged to base (create_prs mode), skip blame attribution and report "tier failed" with the failing test output. Keep merge-order bisection only for deferred-merge mode, in topological order, and document the two-component-interaction limitation in the module docstring.

Tests (real git): a conflicted tier leaves the user's checkout byte-identical and recovers cleanly; a failing tier yields nonzero exit; a breaker re-runs and a subsequently passing contract completes the run; squash-merged mode reports without blame.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R0.3, flip the R0.3 checkbox.
```

## Session 2C: Worktree provisioning + retry context + guard ordering
Branch: `fix/r0-4-provisioning`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R0.4 in docs/remediation-roadmap.md. You are implementing R0.4: the pair of defects that caused the recorded end-to-end validation failure (docs/phase-f-e2e-validation-v12.log: read it, it is short and is your acceptance scenario).

Defects:
- factory.py _run_component copies only the PRD into the worktree. scripts/kstrl/prompt.md is gitignored, so fresh worktrees never contain it and the engineer silently falls back to the harness DEFAULT_PROMPT (log line 38).
- The repo-root detection heuristic (factory.py:274-280) is inverted: it tests for .kstrl/worktrees inside the worktree, which only exists if committed; CLAUDE.md/AGENTS.md propagation is a guaranteed no-op in real worktrees.
- Root-relative paths (prd/prompt) resolve against the worker's inherited CWD (factory.py:254, 265-268), so --root from another directory silently no-ops the copies.
- verify.check_diff_scope failure messages name neither the base branch nor the allowed paths; in the logged run the agent guessed `main` as base, ran `git checkout main -- kstrl/decompose.py ...` reverting base-branch content, and failed again.
- loop.py:168-186: the completion-marker early return happens BEFORE guards.enforce_allowed_paths, so an agent that edits out-of-scope files and emits COMPLETE in the same iteration bypasses enforcement.

Scope: kstrl/factory.py (_run_component and _submit_args plumbing), kstrl/verify.py (check_diff_scope message), kstrl/context.py (pass-through only if needed), kstrl/loop.py (guard ordering), tests.

Requirements:
1. _run_component receives root_dir explicitly and resolves prd/prompt/CLAUDE.md/AGENTS.md sources against it; copies prompt.md and CLAUDE.md/AGENTS.md into the worktree alongside the PRD; delete the broken heuristic.
2. check_diff_scope failure details include the base branch name and the full allowed-paths list, formatted so the retry prompt carries them verbatim.
3. Guards run before the completion early-return in loop.py.
4. Integration test: a real worktree run (fake agent) has the customized prompt file present; a diff-scope failure's retry context contains the base branch string and allowed paths; an out-of-scope edit plus same-iteration COMPLETE is reverted.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R0.4, flip the R0.4 checkbox.
```

---

# Wave 3 (after wave 2 merges)

## Session 3A: Instance and state safety
Branch: `fix/r0-5-instance-safety`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R0.5 in docs/remediation-roadmap.md. You are implementing R0.5.

Defects:
- _setup_worktree unconditionally force-removes an existing worktree at the component path under a per-component flock that only serializes the git commands: a second factory invocation destroys the first's IN-FLIGHT worktree (guaranteed collision for `ks run`, whose component id is constant) (H-7). Stale branches from aborted runs are silently reused with their old commits via the fallback `git worktree add <path> <branch>`.
- factory.py:424 hardcodes the manifest save path: `ks run` clobbers an in-progress factory's resumable manifest, and `--manifest /custom.json` state saves to the wrong file (H-15).
- single_pr mode with parallel worktrees hard-fails every same-tier component after the first (branch already checked out elsewhere); nothing forces max_parallel=1 (H-8).

Scope: kstrl/factory.py, kstrl/cli.py (manifest path + single_pr guard), kstrl/manifest.py (if path handling lives there), tests.

Requirements:
1. Run-level exclusion: flock on .kstrl/factory.lock held for the whole run; a second invocation on the same root refuses to start with a clear message (add a --force-lock override flag). POSIX-only like the existing A4 lock; degrade with a warning on Windows.
2. Worktrees keyed .kstrl/worktrees/<run_id>/<component_id>; cleanup and crash-recovery updated for the new layout; stale branches from previous runs are deleted if fully merged, otherwise the run refuses with an explicit error naming the branch. Never silently reuse.
3. Manifest save path == manifest load path. `ks run` uses its own scripts/kstrl/run-manifest.json.
4. single_pr=true forces max_parallel=1 with a printed notice.

Tests: two-process contention test (second invocation refused while first holds the lock); custom-manifest round-trip saves to the custom path; single_pr parallel request downgrades; stale-branch refusal.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R0.5, flip the R0.5 checkbox.
```

## Session 3B: Reviewer gate integrity
Branch: `fix/r1-1-review-gates`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R1.1, R1.2, R1.3 in docs/remediation-roadmap.md. You are closing the reviewer-gate holes. Do NOT edit any prompt body.

Defects:
- review.py:406-485: {"stories":[],"concerns":[]} parses to passed=True in hard mode; criterion coverage against the PRD is never checked (CRIT-5).
- review.py:437-441: verdicts stored verbatim and compared case-sensitively; "FAIL"/"Blocked" become non-blocking advisories.
- review.py:518-527: the AgentOutputTooLarge path never sets infrastructure_error (security.py:499-505 does), so a review that never happened is indistinguishable from a clean one in advisory mode; findings.py's "len(findings)==0 means ran cleanly" claim is false on this path (H-13).
- Budget-exhausted and mode-skipped phases leave no trace in the findings stream (factory.py:599-604, 664-670, 774-778).
- A generic reviewer-agent exception propagates out of run_review and aborts the entire run via unwrapped _handle_result call sites; security wraps its call in except Exception (factory.py:684-718) but review does not (factory.py:615-624).
- review.py:409-421: _extract_json can return non-dict JSON (null, list) and crash the parser with AttributeError.
- factory.py:732-739: a security infrastructure error in advisory mode produces a PR body with NO security section: "did not run" is invisible.
- review.py:484 / security.py:436: raw_output truncated to 2000 chars, losing the forensic tail of malformed outputs.

Scope: kstrl/review.py, kstrl/security.py (parity bits only), kstrl/findings.py, kstrl/factory.py (phase call sites + PR body), kstrl/pr.py (security-did-not-run section), tests.

Requirements:
1. Criterion coverage: every PRD story id must receive a verdict or the result is infrastructure_error=True. Match by story id, not criterion text.
2. Verdict whitelist {pass, fail} case-insensitive; anything else is a parse failure (infrastructure), never an advisory.
3. AgentOutputTooLarge sets infrastructure_error in review, mirroring security.
4. Skipped and budget-exhausted phases emit a synthetic Finding (category "phase_skipped", is_infrastructure_error stays False but a distinct flag or tag marks non-execution) and a journal event, so the findings stream distinguishes ran-clean from never-ran.
5. Wrap the review agent call like security's (catch Exception, degrade to per-component infra failure, never abort the run).
6. Guard non-dict _extract_json results.
7. PR body renders an explicit "security review did not run (infrastructure error)" section when applicable (render_findings_markdown already has the callout; wire it).
8. Full raw-output debug dumps for review and security parse failures (mirror knowledge.py's _distill_raw.txt pattern) instead of the 2000-char truncation.

Tests: empty review fails hard mode; "FAIL"/"Blocked"/"PASS " verdicts behave correctly; oversized output is infra; skip/budget paths produce the synthetic finding + journal event; reviewer crash fails one component, not the run; non-dict JSON is infra; PR body shows the did-not-run section.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R1.1-R1.3, flip those checkboxes.
```

## Session 3C: Scope-guard hardening
Branch: `fix/r1-5-scope`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R1.5 in docs/remediation-roadmap.md. You are hardening the diff-scope guard. Do NOT edit any prompt body.

Defects:
- git.get_diff_names (git.py:216) uses `git diff --name-only base...HEAD`; git rename detection means moving protected content into an allowed path shows ONLY the destination, so a rename-move defeats scope (empirically reproduced in the review: `git mv protected/gate.py allowed/gate.py` yields only allowed/gate.py) (H-5).
- decompose._validate_decompose_output (decompose.py:414-434) validates allowedPaths entries only as non-empty strings. The DECOMPOSE_PROMPT promises the harness rejects entries like ".kstrl/" or repo-root manifests: it does not, so the constrained model can reopen its own guardrail (H-4). Read the EXCLUDE list stated inside DECOMPOSE_PROMPT (do not modify the prompt) and enforce exactly that list in the validator.
- factory.py:554-558: PRD load/parse failure is swallowed into allowed_paths=None, which silently DISABLES the scope check (check_diff_scope returns passed=True on falsy allowed_paths).

Scope: kstrl/git.py, kstrl/verify.py, kstrl/decompose.py (validator only), kstrl/factory.py (the swallow site), tests.

Requirements:
1. get_diff_names uses --name-status -M (and -C if cheap); rename/copy SOURCES count as changed paths for scope purposes. Update all callers.
2. allowedPaths content validation: reject absolute paths, "..", entries matching the DECOMPOSE_PROMPT EXCLUDE list, and overly-broad entries ("/", "."). Rejection flows through the existing decompose retry-with-error loop.
3. PRD load failure at the factory scope site fails CLOSED: produce a failing diff_scope CheckResult with an infrastructure-flavored message instead of None-disabling. Keep the "no allowed_paths configured at all" legitimate-disable case working (distinguish "not configured" from "failed to load").
4. Keep the base-branch + allowed-paths failure-message content added in wave 2 intact.

Tests: rename-move is caught (real git repo test); each EXCLUDE-list entry is rejected by the validator with a retryable message; PRD-load failure fails the check; unconfigured scope still passes with the existing message.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R1.5, flip the R1.5 checkbox.
```

## Session 3D: Architect persistence + decompose validation ordering
Branch: `fix/r1-7-architect`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R1.7 and R1.8 in docs/remediation-roadmap.md. Do NOT edit any prompt body.

Defects:
- decompose.py:514-537: red-team spec_issues (blockers, majors, minors) exist only as UI lines; nothing persists them. Three weeks later there is no record the harness saw a problem coming (violates the audit-trail invariant).
- decompose.py:539-577: PRD schema validation happens AFTER the retry loop and AFTER files are written; a malformed story crashes decompose outside the retry loop, the LLM never gets to fix it, and partial prd.json files remain on disk.
- Empty userStories, empty acceptanceCriteria, and passes:true all pass validation (vacuous components auto-pass review downstream).

Scope: kstrl/decompose.py, kstrl/cli.py (print the artifact path on halt), tests.

Requirements:
1. Persist ALL spec_issues to scripts/kstrl/spec-issues.json (structured: severity, kind, summary, location) plus a journal event; on SpecBlockerError the CLI prints the file path so the user iterates against a durable artifact.
2. Move PRD schema validation inside the decompose retry loop so validation errors feed back to the LLM like other validation failures; write files only after everything validates; clean up partials on terminal failure.
3. Reject empty userStories, empty acceptanceCriteria arrays, and passes:true at decompose validation with retryable messages.

Tests: spec-issues.json written with correct content on both halt and success; a malformed story triggers a retry (feed a fake agent that fails once then succeeds); no partial files after terminal failure; the three vacuous-PRD cases are rejected.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R1.7 + R1.8, flip those checkboxes.
```

---

# Wave 4 (after wave 3 merges)

## Session 4A: Truncated-diff policy + security parity
Branch: `fix/r1-4-truncation`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R1.4 in docs/remediation-roadmap.md. Do NOT edit any prompt body (the reviewer-facing truncation DIRECTIVE is Session 8C's job; yours is the mechanical policy).

Defects:
- git.py:261-271 head-truncates review diffs at 50KB. A rogue engineer can pad the first 50KB with benign churn and land the malicious hunk after the cut; both reviewers see a truncation notice but hard mode has no policy: the unreviewed tail merges (H-16).
- The security reviewer still sees the engineer's Self-Critique block: only Phase 2 strips it (review.py:391 vs security.py:377), so the E2 anti-anchoring rationale is unapplied where it matters most.

Scope: kstrl/git.py, kstrl/review.py, kstrl/security.py, kstrl/factory.py (chunk orchestration + budget), tests.

Requirements:
1. Hard mode: a diff exceeding the cap is never partially reviewed silently. Chunk the diff on file boundaries into <=cap segments and run one review pass per chunk (each pass counts against max_adversarial_calls); merge results (any chunk failure fails; findings concatenate). If the budget cannot cover the chunks, the phase fails as infrastructure rather than passing partially.
2. Advisory mode: single-pass review is acceptable but the result is annotated as partial (finding or suffix) so a PASS over a truncated diff is visibly partial.
3. Apply strip_self_critique_from_diff before the security review too (share the stripped diff once in factory rather than stripping twice).
4. Same policy for the knowledge distiller input is OUT of scope; note it in the PR if you see it matters.

Tests: an oversized synthetic diff in hard mode produces N chunked calls (fake agent counts invocations) and merges verdicts correctly; budget-insufficient chunking is infra-fail; advisory is marked partial; security prompt input no longer contains the Self-Critique block (string-level test on the built prompt).

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R1.4, flip the R1.4 checkbox.
```

## Session 4B: Config control plane
Branch: `fix/r2-1-control-plane`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R2.1 and R2.2 in docs/remediation-roadmap.md. You own cli.py for this wave; no other session touches it.

Defects:
- The CLI never calls VerifyConfig.load, SecurityConfig.load, ContractConfig.load, FactoryConfig.load, FeedforwardConfig.load, or EvolutionConfig.load (all exist, all tested, zero product call sites). `ks factory` constructs phase configs directly from click flags (cli.py:1415-1456), so click defaults always win and six of nine kstrl.toml sections are silently ignored (CRIT-7).
- max_adversarial_calls and pause_before_pr_merge are unreachable: FactoryConfig.load reads only 7 keys (factory.py:91-105), no env var, no flag.
- FeedforwardConfig/EvolutionConfig/KnowledgeConfig lack from_env.
- ks init does not scaffold kstrl.toml, so a fresh project has no discoverable config surface.

Scope: kstrl/cli.py, kstrl/config.py, kstrl/factory.py (FactoryConfig.load keys), kstrl/verify.py, kstrl/security.py, kstrl/contract.py, kstrl/feedforward.py, kstrl/evolution.py, kstrl/knowledge.py (loaders/from_env only), kstrl/init_cmd.py (scaffold), tests.

Requirements:
1. Resolution order everywhere: explicit CLI flag > env > kstrl.toml > dataclass default. Give click options default=None sentinels so "not passed" is distinguishable from "passed the default value"; apply flag overrides onto the loaded config.
2. Both `ks factory` and `ks run` build every phase config via .load(root). Add from_env where missing so the documented env vars all function.
3. FactoryConfig.load reads max_adversarial_calls and pause_before_pr_merge; add KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS / KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE env vars and --max-adversarial-calls / --pause-before-pr-merge flags.
4. ks init scaffolds a fully commented kstrl.toml (content mirrors kstrl.toml.example, trimmed to real keys).
5. Behavior-change caution: existing setups where kstrl.toml sections were silently ignored will now take effect. Add a NOTE line to the factory startup output when a toml section changed an effective value away from the CLI default, and document the change in the PR body prominently.

Tests: for each of the nine sections, a toml round-trip test proving the value reaches the resolved config through the real CLI construction path; precedence tests (flag beats env beats toml) for at least factory/verify/security; the two safety knobs reachable via all three surfaces.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R2.1 + R2.2, flip those checkboxes.
```

## Session 4C: Spine tests I (worktrees + PR paths)
Branch: `test/r4-2-spine-1`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R4.2 in docs/remediation-roadmap.md. You are adding the first half of the real-git spine tier. Tests only: do not modify kstrl/ source (if a test exposes a product bug, write the failing test, mark it xfail with a comment naming the bug, and report it in the PR body).

Context: the review found the five most load-bearing behaviors have mock-only coverage. This session covers: worktree lifecycle and PR failure paths.

Scope: tests/ (new files, conftest marker registration), pyproject.toml (marker declaration only).

Requirements:
1. Register a `spine` pytest marker; all new tests carry it so `-m "not spine"` keeps the fast tier fast.
2. Worktree lifecycle against real git repos: _setup_worktree creates from the requested base; cleanup removes; recreate-after-crash works; the run-level and per-component locks actually exclude (two-PROCESS test using multiprocessing or subprocess, not threads: flock is per-process).
3. PR failure paths with a stub gh binary the test writes onto PATH (a small shell/python script whose behavior is driven by an env var): push-fail, pr-create-fail, merge-fail, wait-timeout. Assert component status and dependent scheduling per the wave-2 semantics (MERGE_PENDING etc.).
4. Keep each test under ~5s; total spine-1 under ~60s. Measure and report actual timings in the PR body (measure, do not estimate).

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R4.2 (flip to [~] with a note that spine II remains), and list any product bugs the tests exposed.
```

---

# Wave 5 (after wave 4 merges)

## Session 5A: `ks run` honesty + feedforward wiring + prd_path contract
Branch: `fix/r2-3-run-honesty`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R2.3 in docs/remediation-roadmap.md.

PROMPT EXCEPTION: this session MAY edit init_cmd.DEFAULT_PROMPT (the engineer prompt template) because the prd_path fix requires it. H3 applies: bump DEFAULT_PROMPT_VERSION, update the (hash, version) snapshot in tests/test_prompt_versions.py in the same commit, and say so in the PR body. Touch no other prompt.

Defects:
- factory.py:328-344 hardcodes max_iterations=30 and interactive=False and never receives allowed_paths; _submit_args (factory.py:954-975) does not forward them. `ks run 5` runs 30 iterations per attempt; -i and --allowed-paths are no-ops (CRIT-8).
- --no-verify does not skip verification: factory.py:532 does `factory_config.verify_config or VerifyConfig()`, substituting defaults; on a non-Python repo this burns full retries against checks that cannot pass.
- Feedforward never runs under `ks factory`: cli builds FactoryConfig without feedforward_config, so ff_config_dict stays None (H-10).
- The prd-path contract is broken: a comment (factory.py:259-261) claims the prompt template uses $prd_path substituted by loop.py, but NO shipped template contains $prd_path; decomposed component PRDs live at scripts/kstrl/feature/<id>/prd.json while the default prompt points at scripts/kstrl/prd.json, so the agent reads the wrong file while check_prd_stories reads the right one (H-11).

Scope: kstrl/factory.py, kstrl/cli.py, kstrl/loop.py, kstrl/init_cmd.py (DEFAULT_PROMPT + scaffold), tests/test_prompt_versions.py (snapshot only, per the exception), tests.

Requirements:
1. Forward max_iterations, interactive, allowed_paths through _submit_args into _run_component; delete the hardcoded 30.
2. --no-verify uses an explicit skip sentinel that run_factory honors (Phase 1 genuinely skipped, stated in output).
3. Wire feedforward config through the factory command path (it should honor the wave-4 control plane: toml/env/flags).
4. DEFAULT_PROMPT gains an explicit $prd_path (and $progress_path if applicable) placeholder; loop.py substitutes the per-component paths; add a test asserting the rendered prompt names the same PRD file that check_prd_stories reads for a decomposed component.
5. H3 compliance for the DEFAULT_PROMPT edit as described above.

Tests: fake-agent run with N=3 executes at most 3 iterations; --no-verify runs zero checks; factory run builds feedforward context (assert marker string in the built prompt); the prd-path consistency test.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R2.3, flip the R2.3 checkbox.
```

## Session 5B: HITL semantics + subprocess env hygiene
Branch: `fix/r2-6-hitl-env`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R2.6 in docs/remediation-roadmap.md.

Defects:
- factory.py:866-880: the E6 human checkpoint's "Reject and abort component" routes through _retry_or_fail, which re-runs the ENTIRE component (agent + reviews) and re-prompts the human each retry; to truly abort you must reject max_retries+1 times, paying a full LLM cycle each time.
- Every verification/contract/fixtures subprocess inherits the harness's full environment, including ANTHROPIC_API_KEY etc.: agent-authored tests can read the harness's secrets.
- Verification subprocess timeouts kill only the direct child: a test that backgrounds a server leaks it across iterations (no start_new_session/killpg in verify.py subprocess calls).

Scope: kstrl/factory.py (checkpoint block), kstrl/verify.py, kstrl/contract.py, kstrl/fixtures.py (env plumbing only), tests.

Requirements:
1. Checkpoint choices become: Approve / Reject (component -> FAILED immediately, no retry, dependents cascade-skip) / Retry (explicit, uses a retry). Non-interactive behavior unchanged (warn + proceed).
2. A shared scrubbed-env helper: allowlist approach (PATH, HOME, LANG, LC_*, TMPDIR, TERM, VIRTUAL_ENV, UV_*, PYTHON*, CI, and the uv cache vars: verify empirically that `uv run pytest` works under the scrubbed env, and add what is truly required rather than guessing). Applied to every verify/contract/fixtures subprocess. Assert ANTHROPIC_API_KEY/OPENAI_API_KEY never pass through.
3. All verification subprocesses use start_new_session=True and process-group kill on timeout.

Tests: reject marks FAILED with zero further agent calls (counting fake agent); a test subprocess printing os.environ shows no *_API_KEY under the scrub while uv-run still functions; a timeout kills a backgrounded grandchild.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R2.6, flip the R2.6 checkbox.
```

## Session 5C: Misleading tests + coverage ratchet
Branch: `test/r4-3-test-fixes`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R4.3 and R4.4 in docs/remediation-roadmap.md. Tests and CI only; do not modify kstrl/ source.

Defects (all verified in the review):
- test_phase_c_coverage.py TestC1ParallelExecution disables worktrees and forces max_parallel=1 (tests nothing parallel); TestC6ConcurrentFactory uses separate roots + mocked _run_component (would pass with the flock deleted); TestC4ContractBreaker asserts only that contract ran, not that the breaker reset.
- test_factory.py test_verification_failure_triggers_retry asserts only final failure, not that a retry happened.
- test_prompt_versions.py docstring cites test_no_silent_version_pin, which does not exist; and the AST-walker "regression guard" tests re-implement the walk inline instead of calling _module_level_prompt_constants (tautological).
- test_codex_agent.py runs a LIVE network LLM call in the default suite when codex is on PATH (with a broad except: skip), and its caching test asserts a==b without counting invocations.
- No coverage measurement exists anywhere.

Scope: tests/, .github/workflows/ci.yml, pyproject.toml (dev deps + config).

Requirements:
1. C1 becomes a true 2-worker worktree test (real git, fake agent); C6 uses the SAME root and proves serialization (assert the second invocation blocks or is refused per wave-3 semantics); C4 asserts the breaker was reset AND re-ran; the verification-retry test counts attempts.
2. AST-walker tests call the real _module_level_prompt_constants against synthetic modules.
3. Implement test_no_silent_version_pin: hash moved while version pinned must fail.
4. Codex live-contract tests gated behind KSTRL_RUN_LIVE_CONTRACT=1; default suite is network-free (grep the suite for other network calls and gate any you find). Fix the caching test to count subprocess invocations.
5. pytest-cov in dev deps; CI uploads a coverage summary and enforces a ratchet: coverage must not drop below the recorded baseline. Record the MEASURED baseline number in the PR (run it, do not estimate) and store it where CI reads it.
6. Register the spine marker split in CI: fast job runs -m "not spine", a second job runs -m spine.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R4.3 + R4.4, flip those checkboxes.
```

---

# Wave 6 (after wave 5 merges)

## Session 6A: Preflight honesty + config show + status stub
Branch: `fix/r2-4-preflight`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R2.4 in docs/remediation-roadmap.md.

Defects:
- cli.py:281-284, 556-559, 858-861: run/understand/feature preflight checks ONLY CodexAgent.is_available() and exits "codex not found in PATH" even when Claude Code is installed and configured (decompose/factory at cli.py:1048, 1344 already do it right).
- `ks run` does not preflight prd.json: the agent burns full iterations against a prompt referencing a nonexistent PRD before Phase 1 reports "Failed to load PRD".
- README promises `ks config show` and `ks status`; neither exists.

Scope: kstrl/cli.py, tests.

Requirements:
1. Preflight accepts whichever agent the resolved config selects (claude/codex/custom) and errors naming the agent it actually looked for, mirroring the factory implementation.
2. run preflights prd.json existence + schema (reuse PRD.validate_schema) BEFORE any agent invocation, with the same friendly per-field errors init uses.
3. `ks config show`: print every resolved config section with the SOURCE of each value (flag/env/toml/default): this is the observability for the wave-4 control plane.
4. `ks status`: minimal version reading the manifest: per component id: status, retries, branch, timestamps if present. (The full ProgressLog-backed version is Session 7B; structure the command so 7B extends it.)

Tests: preflight matrix (claude-only machine + type=claude passes; codex-only + type=codex passes; mismatch errors correctly); missing/invalid prd.json blocks before the agent runs (counting fake agent); config show output names sources correctly for a toml-set, env-set, and flag-set value; status renders a synthetic manifest.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R2.4, flip the R2.4 checkbox.
```

## Session 6B: Cost meter
Branch: `feat/r3-1-cost-meter`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R3.1 in docs/remediation-roadmap.md.

Context: no token or dollar visibility exists anywhere; the call-count budget covers only review/security/distill and excludes the engineer loop (the dominant spend). Your job is per-phase, per-component usage accounting.

MEASURE FIRST (the project rule is measure, do not assume): before writing the meter, empirically determine what usage data each agent CLI emits. Run the claude CLI in the stream-json mode the ClaudeCodeAgent uses and inspect the result event for usage fields; check what codex emits with the flags CodexAgent uses. Write your findings (actual field names, sample values) into the PR body. If codex exposes nothing, fall back to call counts + wall time for codex and say so.

Scope: kstrl/agents/*.py (usage extraction), kstrl/loop.py, kstrl/factory.py (aggregation), kstrl/observability.py, kstrl/evolution.py (experiments columns), tests.

Requirements:
1. Agents surface a usage record per invocation (tokens in/out where available; else calls + duration). The Agent protocol change must stay backward-compatible for CustomAgent.
2. Factory aggregates per component per phase (engineer iterations, review, security, distill) and prints a rollup table in the run summary; the journal entry and experiments.tsv gain the totals.
3. Optional max_total_tokens (toml/env/flag via the control plane): when exceeded, halt LOUDLY: fail the current component with a synthetic budget finding (reuse the wave-3 phase_skipped pattern) rather than degrading silently.
4. The meter must never gate correctness: parse defensively; a usage-parse failure logs a warning and records unknown, never crashes a run.

Tests: fake agent emitting realistic usage events -> correct rollup math; missing-usage fallback; budget halt fires and is recorded; malformed usage never raises.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R3.1 (Tested must include the CLI-emission findings), flip the R3.1 checkbox.
```

## Session 6C: Spine tests II (contract, retry propagation, crash recovery)
Branch: `test/r4-2-spine-2`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R4.2 in docs/remediation-roadmap.md. Second half of the real-git spine tier. Tests only: if a test exposes a product bug, write it xfail with a comment naming the bug and report it in the PR body.

Scope: tests/ (new spine-marked files).

Requirements:
1. Contract execution against real git: passing tier; conflicted tier (assert the user checkout is byte-identical afterward and the run recovers per wave-2 semantics); breaker re-run to completion.
2. Retry-context propagation end to end: a fake agent that writes its received prompt to a file; attempt 1 fails diff-scope; assert attempt 2's prompt contains the failure details INCLUDING the base branch and allowed paths (wave-2 behavior).
3. Crash recovery: kill a run mid-verify (subprocess-driven factory run, SIGKILL); restart; assert RUNNING/VERIFYING reset, stale worktrees from the run_id layout are handled, and the manifest is consistent.
4. Engineer-loop plumbing smoke: one unmocked _run_component execution with a fake agent binary end to end (worktree in, result out, PRD copy present, prompt copy present).
5. Keep spine II under ~90s total; report measured timings in the PR body.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R4.2, flip R4.2 to [x] (spine complete).
```

---

# Wave 7 (after wave 6 merges)

## Session 7A: Docs regeneration + packaging truth
Branch: `fix/r2-5-docs`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R2.5 in docs/remediation-roadmap.md.

Defects (docs vs reality):
- README quick start step 1 (`uv tool install kstrl-cli`) installs an UNRELATED PyPI package; step 3 (`kstrl prd create`) does not exist; a third of the CLI table is fiction (TUI, prd group, config show/status existed only as of wave 6, --legacy). "362 tests" is stale. README documents [sensors]/[fixtures] toml sections that do not exist in code and omits [knowledge]/[factory]/[verify]/[security]/[contract].
- pyproject.toml still depends on textual (dead since the TUI purge).
- examples/uv-python predates the engineer contract: no Self-Critique block in its prompt.md, prd_prompt.txt forbids allowedPaths.
- adversarial-design.md says the distiller runs "post-merge"; it runs pre-PR. runbook.md tells the operator to inspect a failed component's worktree, but cleanup deletes worktrees (align with the --keep-worktrees-on-failure flag landing in Session 7C: coordinate by documenting the flag as the recovery path).

Scope: README.md, docs/*.md, kstrl.toml.example, examples/uv-python/, pyproject.toml (dependency removal), scripts/ (new generator), .github/workflows/ci.yml (doc-drift check), tests as needed.

Requirements:
1. Write scripts/gen_docs.py: generates the README CLI reference from click introspection and the config reference from the dataclasses + loaders. Insert between markers in README so the rest is hand-written. CI check: regeneration produces no diff.
2. Install story: unless the user has decided on publishing (check the roadmap "User decisions" section: if undecided), document clone-install (`uv tool install -e .` from a clone) as the ONLY path and remove the PyPI command.
3. Remove textual from dependencies; verify nothing imports it.
4. Refresh examples/uv-python to the current engineer contract (Self-Critique block, allowedPaths-aware prd_prompt, current progress format).
5. Fix the drift list: distiller timing wording, runbook worktree guidance, test count (state the measured number), [sensors]/[fixtures] removed, missing sections added from kstrl.toml.example.

Tests: the CI doc-drift check itself; a test that scripts/gen_docs.py output matches the committed README sections.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R2.5, flip the R2.5 checkbox (and R3.4 fully if the kstrl.toml tracking note is resolved here).
```

## Session 7B: Status + notifications
Branch: `feat/r3-2-status-notify`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R3.2 in docs/remediation-roadmap.md.

Context: the ProgressLog (observability.py) is a JSONL event bus (component_started/completed/failed/retrying, verification_result, review_result, contract_result) but is opt-in and unconsumed. There is no way to know a walk-away run's state without reading raw JSON, and no notification when it finishes or breaks.

Scope: kstrl/observability.py, kstrl/cli.py (status command extension), kstrl/factory.py (default-on wiring + hook firing), kstrl/config.py or factory config ([notify] section), tests.

Requirements:
1. ProgressLog defaults ON (path under .kstrl/), configurable off.
2. Extend `ks status` (from Session 6A) to join manifest + ProgressLog: per component: phase, attempt, last-event age, cost totals (from Session 6B), evidence paths. A --watch flag polling on an interval is optional; add only if simple.
3. [notify] config: on_complete and on_first_failure shell commands (documented examples: terminal bell, curl webhook). Fired exactly once each per run; also fire on MERGE_PENDING. Hook failures log a warning, never affect the run.
4. Events gain the run_id so multiple runs' logs are distinguishable.

Tests: status renders correctly from a synthetic manifest + log; hooks fire exactly once per condition (counting stub command); hook failure is non-fatal; default-on writes the log.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R3.2, flip the R3.2 checkbox.
```

## Session 7C: Resume + partial-failure ergonomics
Branch: `feat/r3-3-resume`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R3.3 in docs/remediation-roadmap.md.

Defects/gaps:
- run_id is generated but never persisted in the manifest; completed_at is never set anywhere; resuming cannot correlate with the prior run: exactly what Linear integration will need later.
- A FAILED component can only be reset by hand-editing manifest.json; failed components' worktrees are deleted at cleanup so post-mortem evidence is gone.
- comp.findings accumulates across retry attempts uncleared (factory.py:628, 731), inflating journal counts and attributing superseded findings to shipped code.

Scope: kstrl/manifest.py, kstrl/factory.py, kstrl/cli.py, kstrl/findings.py (attempt tag), tests.

Requirements:
1. Persist run_id in the manifest; set completed_at on terminal states; store per-component last-attempt evidence pointers (worktree path if kept, journal offsets).
2. `ks retry <component-id>`: resets that FAILED component and its cascade-SKIPPED dependents to PENDING and re-enters the factory with the same manifest (respecting the wave-3 run lock).
3. --keep-worktrees-on-failure flag (and toml key via the control plane); update the cleanup path; the failure summary lists per failed component: phase, check, and where the evidence lives.
4. Clear comp.findings at the start of each attempt; tag every Finding with attempt:<n> so the journal distinguishes superseded findings from final ones.

Tests: retry round-trip on a synthetic failed manifest; keep-worktrees leaves the failed worktree and the summary points at it; findings from attempt 1 do not appear in the final component findings but DO remain in the journal tagged attempt:1; completed_at set.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R3.3, flip the R3.3 checkbox.
```

---

# Wave 8 (after wave 7 merges): calibration + the one prompt batch

## Session 8A: Calibration tooling + model-bump trigger
Branch: `feat/r5-1-calibration-tooling`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R5.1 and R5.5 in docs/remediation-roadmap.md. Do NOT edit any prompt body.

Defects: calibration has no comparison tooling (H2 says "compare against baseline" but nothing diffs baseline JSONs, no threshold is codified); each fixture is a hard assert so the suite has been red-at-baseline in every recorded run (the architect misses are matcher artifacts: must_include_kind demands exact taxonomy labels the model paraphrases); single-trial single-model runs cannot support a "detection rate"; baselines do not record the model.

Scope: kstrl/ (new calibration module), tests/test_calibration.py, tests/test_calibration_matchers.py, tests/adversarial_fixtures/_results/ format, docs (usage section), tests.

Requirements:
1. `python -m kstrl.calibration compare <old.json> <new.json>`: per-role, per-category deltas with codified pass/fail thresholds (thresholds in one constants block, documented).
2. N-run mode: the runner supports KSTRL_CALIBRATION_RUNS=N (default 3 for baselines), reporting per-fixture consistency (fraction of runs detected) alongside detection.
3. Convert per-fixture hard asserts into threshold gates over the run set, so the suite is green at the current baseline and red on REGRESSION. The report JSON keeps per-fixture detail.
4. Fix matcher brittleness: kind-matching accepts a documented synonym map (e.g. unstated_assumption ~ missing_detail) OR grades kind separately from detection so a paraphrased kind is a partial hit, not a miss. Update matcher unit tests.
5. Baselines record the model id; add an always-run structural test that WARNS (not fails) when the configured calibration model differs from the newest baseline's model, citing H2-extended (re-calibrate on model change).
6. You cannot run the real-LLM suite yourself; end the PR body with the exact commands the user should run to capture the new-format baseline, and mark R5.1 [~] until a baseline in the new format exists.

Tests: compare tool against synthetic old/new JSONs (regression detected, improvement passes); consistency math; synonym matcher units; model-drift warning.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R5.1 + R5.5.
```

## Session 8D: Self-critique check correctness (parallel with 8A)
Branch: `fix/r5-4-self-critique`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R5.4 in docs/remediation-roadmap.md. Do NOT edit any prompt body.

Defects (verify.py:224-254, both empirically reproduced in the review):
- check_self_critique walks from the end of progress.txt to the LAST Self-Critique heading, never associating the block with the CURRENT iteration's entry: an iteration that omits the block passes if any earlier iteration had one. The docstring claims the opposite of what the code does.
- The bullet-boundary break is dead code: `stripped.rstrip(":*").lower().endswith(("**","**:"))` can never be true because rstrip("*:") strips the trailing asterisks first: bullets from later sections (e.g. Interpretations in DEFAULT_PROMPT's format) inflate the count toward min_bullets.

Scope: kstrl/verify.py, tests/test_verify.py.

Requirements:
1. Associate the Self-Critique block with the current (latest) iteration entry: identify the latest iteration boundary in progress.txt first, then require the heading within it. Document the expected progress format assumption in the docstring honestly.
2. Fix the boundary break so a following bold-label section terminates the bullet count; extend the fuzz corpus with both regression cases (missing-block-in-latest-iteration; inflated-count-from-next-section).
3. Keep this a shape check; note in the docstring (H4) that substance is not verified.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R5.4, flip the R5.4 checkbox.
```

## Session 8B: Fixture expansion (after 8A merges)
Branch: `feat/r5-2-fixtures`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R5.2 in docs/remediation-roadmap.md. Do NOT edit any prompt body.

Context: the existing planted bugs are textbook-trivial (f-string SQL, shell=True, sk_live constant, random tokens, signature-discarding JWT): Haiku catches them all, so the suite can detect catastrophe but not regression. There are no negative fixtures, so false-positive rate: the metric that keeps hard-mode halts credible: is unmeasured. Fixtures also use a synthetic no-PRD context and an all-PASS verification stub (test_calibration.py around line 55), so measured detection may not transfer.

Scope: tests/adversarial_fixtures/ (new fixtures + meta), tests/test_calibration.py (context realism), tests/test_calibration_matchers.py, tests.

Requirements:
1. Hard positives, at least 4: multi-hop authorization bug (permission check present but on the wrong object), second-order injection (sanitized at entry, unsafe at use), TOCTOU race, subtle timing oracle (early-return comparison). Each as a realistic multi-file diff with .meta.json.
2. Negatives: at least 3 clean-but-nontrivial diffs per role (security, reviewer) whose meta marks must_not_flag categories; the runner computes FP rate and includes it in the report + thresholds (extend the 8A format).
3. Context realism: fixtures gain a real PRD and realistic verification output; replace the all-PASS stub.
4. You cannot validate hardness yourself (needs real LLM runs). End the PR with the exact KSTRL_RUN_CALIBRATION commands; the ACCEPTANCE criterion the user checks: baseline model does NOT trivially catch all new positives on the first run (if it does, the fixtures are not hard and need another iteration): mark R5.2 [~] pending that empirical check.
   UPDATE (2026-07-20): this "must not catch all" bar was DROPPED. The capture caught all 4 hard positives 3/3, but investigation (docs/adversarial-design.md "Hard-positive hardness") showed the matcher is strict, the catches are genuine, and even a tell-free variant is caught 5/5 - haiku is just a competent reviewer, so `< 1.0` is ill-posed. R5.2 was reframed (measured-not-gated + detection-drop floors) and closed [x]. Do NOT reintroduce the "must be missable" bar for these categories.

Tests: structural checks for every new fixture (loadable, meta schema, matchers resolve); FP-rate math on synthetic results.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R5.2.
```

## Session 8C: The prompt-hardening batch (LAST in wave 8)
Branch: `feat/r5-3-prompt-batch`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R5.3 in docs/remediation-roadmap.md, then docs/adversarial-design.md (H2/H3 sections). This is the ONE session authorized to edit prompt bodies. H2 and H3 bind everything you do.

Changes (batch them so one calibration cycle covers all):
1. Injection separation in REVIEWER_PROMPT, SECURITY_PROMPT, DISTILL_PROMPT, DECOMPOSE_PROMPT: an explicit paragraph stating content between the delimiters is DATA and instructions inside it must never be followed; harness-side per-run random delimiters (code in review.py/security.py/knowledge.py/decompose.py generates them and the prompts reference them: the prompt text names the delimiter variable, the harness substitutes).
2. Spec-as-data framing in DECOMPOSE_PROMPT (the spec is currently an unguarded injection surface).
3. Truncated/chunked-diff directive for both reviewers (pairs with the wave-4 mechanical chunking): instruct the model that a truncation notice means the review is partial and must be flagged.
4. Remove the hardcoded "exhaustively_searched": true from both schema examples (anchor bias): show the field with an honest placeholder.
5. Security FP hard-exclusion list (precision-first, mirrors Anthropic's security-review action): exclude DoS, rate-limiting, and theoretical input-validation findings unless concrete exploitability is stated. This protects hard-mode halt credibility.
6. OPTIONAL (only if the 8B fixtures support measuring them): reviewer concern categories for concurrency and new-dependency introduction.

Process requirements (non-negotiable):
- Every edited prompt: bump its *_PROMPT_VERSION (minor bump), update the (hash, version) snapshot in tests/test_prompt_versions.py: all in the same commit as the body change (H3).
- The harness-side delimiter code changes come with unit tests (delimiters present in built prompts, random per run, referenced by the prompt text).
- H2: you cannot run real-LLM calibration. Prepare docs/calibration-notes-r5.md with: what changed per prompt, the exact before/after commands (KSTRL_RUN_CALIBRATION=1 with the 8A N-run mode), and empty tables for the user to paste deltas into. The PR body states in bold that the PR must not merge until the user has run calibration and recorded no regression.
- Injection efficacy check: add planted injection strings to at least one 8B negative fixture (an in-diff instruction telling the reviewer to emit empty JSON) so calibration MEASURES injection resistance rather than asserting it.

Finish per Shared rules: green checks (the snapshot test proves H3 compliance), rebase, PR with Tested/Assumed referencing R5.3, mark R5.3 [~] pending the user's calibration run.
```

---

# Wave 9 (after wave 8 merges and calibration deltas are recorded)

## Session 9A: Evolution loop repair
Branch: `fix/r6-evolution`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then items R6.1-R6.4 in docs/remediation-roadmap.md.

Defects:
- Component failures record flattened strings ("Review failed", "Mechanical verification failed"), so evolution.py's _normalize_error collapses every failure into one degenerate signature and cross-run "patterns" are tautologies; the linter-code fast path (S608) can never fire (evolution.py:126-181, 554-569).
- get_concern_hit_rate (evolution.py:651-690) scans entry["error"] for concern categories that never appear there: structurally zero forever, while the structured findings it needs are already in the same journal entries.
- factory.py:1131 constructs EvolutionConfig() directly: [evolution] toml and env ignored, journal_path CWD-relative, writes wrapped in `except OSError: pass`.
- Proposal IDs restart at PROP-001 every call and save_proposals clobbers prior files (evolution.py:475-480, 599-645).
- `ks evolve --apply` prints "not yet automated" while README and the command's own output promise application; auto_propose is never consulted; recorded durations are 0.0 and retry_rate semantics are undocumented.

Scope: kstrl/evolution.py, kstrl/factory.py (failure recording + config load), kstrl/cli.py (evolve), docs (metric definitions), tests.

Requirements:
1. Failure recording carries check_name plus the parser's structured signature (e.g. linter:E501, typecheck:arg-type, diff_scope:rename, review:<category>); pattern extraction consumes these.
2. get_concern_hit_rate reads findings_summary; proposals derive from finding taxonomies + real signatures; proposal IDs monotonic across runs, never clobbering prior files.
3. factory uses EvolutionConfig.load(root_dir) (the wave-4 control plane); journal paths resolve against root; the silent except becomes a logged warning.
4. evolve --apply implements the minimal real path: convention-type proposals append to the project CLAUDE.md Agent Learnings section after explicit confirmation; everything else prints manual instructions honestly. Honor auto_propose.
5. Fix duration recording; document retry_rate and every experiments.tsv column in docs/ (state definitions, do not invent aspirational metrics).
6. Fresh-journal note: wave 1 archived the polluted journals; add a journal-format version field so future migrations are detectable.

Tests: an integration test with a synthetic-but-realistic journal (real signature strings) yields a proposal traceable to a signature and a nonzero concern hit rate; proposal-ID monotonicity across two runs; config load path; --apply appends exactly once with confirmation.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R6.1-R6.4, flip those checkboxes.
```

---

# Wave 10 (after wave 9): strategic

## Session 10A: Cross-model review rotation (BLOCKED on user decision 2)
Branch: `feat/r7-1-rotation`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R7.1 in docs/remediation-roadmap.md. Precondition: the user has named the second model family in the roadmap "User decisions" section; if it still reads undecided, STOP and report.

Context: 2025-2026 research established self-preference bias and verifier homogenisation mechanistically: a same-family reviewer systematically misses the bug classes its family produces. The config already supports review_agent_cmd/review_model; nothing defaults to heterogeneity, warns about homogeneity, or records who reviewed whom.

Scope: kstrl/factory.py, kstrl/config.py / factory config, kstrl/findings.py, kstrl/review.py, kstrl/security.py, kstrl/pr.py, docs/adversarial-design.md (limitation #1 update), tests.

Requirements:
1. When the decided second family's CLI is available, review and security default to it (engineer keeps the primary); explicit config always wins. When unavailable, print a homogeneity warning naming the risk.
2. Every Finding records the reviewing model identity (tag model:<id>); the PR body's findings section names the reviewer model; the journal carries it.
3. Reviews remain independent single passes: do NOT add any cross-reviewer deliberation.
4. Measurement plan: extend the calibration runner to accept a reviewer-model override so the user can capture same-family vs cross-family baselines with the 8A tooling; put the exact commands in the PR body. Mark R7.1 [~] until the user records both baselines.
5. Update docs/adversarial-design.md known-limitation #1 to describe the new default and what remains correlated (architect/engineer/distiller).

Tests: default-selection matrix (both CLIs present / one absent / explicit override); model tag present on findings end to end; homogeneity warning fires.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R7.1.
```

## Session 10B: Wire fixtures, sandboxed (parallel with 10A)
Branch: `feat/r7-2-fixtures-wired`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R7.2 in docs/remediation-roadmap.md.

Context: fixtures.py (the independent behavioral oracle) is currently wired to NOTHING: run_mechanical_verification never calls check_fixtures, and PRD.validate_schema rejects the documented fixtures key. Two latent hazards block naive wiring: run_function_fixture imports and calls agent-written code IN the harness process (sys.path.insert + importlib) with the harness env (API keys); run_cli_fixture executes a PRD-supplied command with shell=True. The PRD is LLM-emitted: treat fixture definitions as untrusted.

Scope: kstrl/fixtures.py, kstrl/verify.py, kstrl/prd.py, kstrl/factory.py (config plumbing), kstrl/config.py (FixturesConfig loader via the control plane), docs, tests.

Requirements:
1. Function fixtures execute in a SUBPROCESS (sys.executable with an argv-passed spec, JSON over stdout), using the wave-5 scrubbed-env helper, cwd=worktree, timeout, start_new_session. The harness process never imports agent code.
2. CLI fixtures: shlex.split + shell=False; document that shell features are unsupported.
3. PRD.validate_schema accepts the fixtures key (schema per README's documented shape; validate fixture entries strictly: unknown keys rejected).
4. run_mechanical_verification calls check_fixtures when [fixtures].enabled (default false per user decision 4); snapshot regression wired behind the same flag; failures produce parsed, actionable retry context like other checks.
5. README fixtures section updated to match what actually ships.

Tests: real subprocess function-fixture round trip (pass and fail); env scrub verified inside the fixture subprocess (no *_API_KEY); a module-level side effect in agent code cannot touch the harness process (e.g. fixture module writes a sentinel: assert isolation); cli fixture with shell metacharacters does not shell-interpret; PRD round trip with fixtures key; Phase 1 integration with fixtures enabled.

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R7.2, flip the R7.2 checkbox.
```

## Session 10C: run_factory refactor (STRICTLY SOLO: nothing else in flight)
Branch: `refactor/r7-3-pipeline`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R7.3 in docs/remediation-roadmap.md. Confirm no other remediation branch is open before starting; this refactor touches everything.

Context: run_factory is a ~770-line function; scheduling, worktrees, six phases, budget, checkpoint, PR lifecycle, distillation, and journaling live in nested closures over a dozen mutable variables (including a knowledge_config late-binding accident). The sequential and parallel loops duplicate the launch protocol. The state machine is untestable in isolation: which is exactly where the review's critical bugs lived.

Approach: STRANGLER, two PRs from this one session if needed.
1. PR 1: extract a ComponentPipeline class: explicit typed phase results (verify/review/security/checkpoint/pr/distill), single place for state transitions (VERIFYING -> retry/fail/COMPLETED/MERGE_PENDING), budget consultation injected. run_factory delegates per-component handling to it. Behavior-preserving: the whole suite including spine must stay green unchanged.
2. PR 2: unify the sequential/parallel scheduling loops into one scheduler consuming the pipeline; remove the duplicated launch protocol and the late-binding hazard.

Also in scope: decide distiller placement (pre-PR vs post-merge) explicitly: keep current behavior unless you find a correctness reason, but make the placement a named, documented step and align docs/adversarial-design.md wording (it currently says post-merge; the code is pre-PR).

Requirements: no behavior change (spine + full suite green before and after each PR); new unit tests for the state machine in isolation (every transition, including retry exhaustion, cascade-skip, MERGE_PENDING, checkpoint reject, budget exhaustion); delete dead code the extraction strands.

Finish per Shared rules: green checks, rebase, PR(s) with Tested/Assumed referencing R7.3, flip the R7.3 checkbox after the second PR.
```

## Session 10D: Linear integration (after 10C; BLOCKED on user decision 3)
Branch: `feat/r7-4-linear`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R7.4 in docs/remediation-roadmap.md. Preconditions: 10C merged; the user has approved the Linear app-actor OAuth identity (roadmap "User decisions" 3) and provided a workspace/team id plus an auth token env var name. If not, STOP and report.

Design constraints (from the 2026 platform research recorded in the roadmap):
- GraphQL API with app-actor auth for all mutations (NOT the MCP server: this is a deterministic pipeline); respect the shared rate-limit pool with modest client-side throttling.
- PR linking comes free via Linear's GitHub integration: branch names carry the issue id, PR bodies carry "Fixes <ISSUE-ID>": status transitions then need zero API calls.
- The Agents API (@-mention delegation) is Developer Preview: OUT of scope; leave a seam.

Scope: new kstrl/linear.py (client + sink), kstrl/observability.py (sink interface), kstrl/decompose.py (issue creation hook), kstrl/factory.py (branch naming + PR body wiring), config ([linear] section via the control plane), docs, tests.

Requirements:
1. LinearSink implements the ProgressLog event interface; factory.py stays untouched except branch/PR-body naming. Sink failures log warnings and never affect the run.
2. Decompose output creates one Linear project per manifest and one issue per component (stories as sub-issues or a checklist: pick one, document why); spec_issues from wave 3 filed as triage issues.
3. Idempotency: every created object carries an external-id key of run_id+component_id; retries and resumed runs UPDATE rather than duplicate (test this: it interacts with ks retry).
4. Branch names include the Linear issue id when the sink is active; PR bodies gain "Fixes <ID>".
5. All Linear calls behind a client class with one HTTP entry point, defensive parsing, and a dry-run mode that logs mutations instead of sending (default in tests).
6. No secrets in code or logs; token comes from the env var the user named.

Tests: sink event-to-mutation mapping against a recorded/dry-run client; idempotency on double-fire and on retry; branch/PR-body formatting; failure isolation (sink exception does not fail the component).

Finish per Shared rules: green checks, rebase, PR with Tested/Assumed referencing R7.4, flip the R7.4 checkbox.
```

## Session 10E: Platform hardening
Branch: `feat/r7-5-platform`

```text
Read CLAUDE.md, then the "Shared rules" section of docs/remediation-sessions.md, then item R7.5 in docs/remediation-roadmap.md. The SpecKit intake sub-item requires a DECOMPOSE_PROMPT change: that part follows the Session 8C process (H2/H3: version bump, snapshot, calibration commands for the user) and should be its own commit.

Sub-items (separable commits; split into two PRs if the diff grows large):
1. No-progress circuit breaker: halt a component when N consecutive iterations (default 3, configurable) produce an unchanged diff hash AND unchanged test-failure signature; the halt is a loud component failure with a distinct error, recorded in the journal. This is the most-repeated community fix for kstrl-loop stalls.
2. Sandboxing pass-through: agent adapters accept sandbox settings (Claude Code's network/write scoping flags) from config and pass them to the CLI; document the codex equivalent or its absence (verify empirically what each CLI supports and record findings in the PR: measure, do not assume).
3. Merge-conflict doctrine: where the factory hits a merge conflict integrating a component, prefer re-running the component against the freshly merged base over rebasing agent output; implement where wave-2/wave-9 machinery makes it natural and document the doctrine in docs/adversarial-design.md.
4. SpecKit intake: `ks decompose` accepts a SpecKit artifact set (spec.md/plan.md/tasks.md) as input, concatenated with provenance headers; DECOMPOSE_PROMPT gains a directive to demand EARS-style acceptance criteria ("WHEN <condition> THE SYSTEM SHALL <behavior>") per component (H2/H3 process applies).
5. Agent SDK spike: a WRITTEN comparison only (docs/sdk-spike.md): drive one component implementation through the Claude Agent SDK in a scratch script; compare against the CLI-subprocess path on: structured usage data, hook-based guardrails, budget enforcement, failure observability. End with a go/no-go recommendation for user decision 5. No production code changes from the spike.

Tests: circuit breaker unit + integration (fake agent producing identical diffs trips it; progressing agent does not); sandbox flag pass-through per adapter; SpecKit intake parsing; EARS directive covered by the calibration commands left for the user.

Finish per Shared rules: green checks, rebase, PR(s) with Tested/Assumed referencing R7.5, flip the R7.5 checkbox (leave [~] if the SDK decision or calibration run is pending).
```

---

## After wave 10

Re-run the A+ gates table in `docs/remediation-roadmap.md` top to bottom: every
gate names its test or measurement. Anything still `[~]` is either awaiting a
user decision or a user-run calibration capture: both are listed in the
roadmap's "User decisions required" section. When all gates are green, the
review's scorecard can be re-assessed against evidence rather than claims.
