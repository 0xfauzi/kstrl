# kstrl Operator Runbook

Recovery procedures for the failure modes that actually happen during factory runs.

## Before you point kstrl at a repository

`ks doctor [--root <path>] [--json]` runs ten static checks over a
repository and reports whether kstrl can point at it. Every check is
mechanical: nothing here runs the repository's own test, typecheck or
lint commands, spawns an agent, or spends anything. The ten checks are
`git_repo` (a repository with commits and a base branch factory can cut
a worktree from), `git_clean` (uncommitted work does not reach the
engineer), `github_cli` (`gh` authenticated and an `origin` remote, for
pushing branches and opening PRs), `kstrl_config` (`kstrl.toml` resolves
in full), `build_manifest` (a build manifest at the repository root
that kstrl recognises; kstrl will not create one, and `ks decompose`
and `ks factory --spec` refuse with exit 2 before the architect runs
without one), `verify_commands` (the test, typecheck and lint commands
Phase 1 will run), `test_root` (tracked paths the
`[adequacy]` gate reads as tests), `gitignore` (whether git ignores
`.kstrl/`, so the in-loop scope guard does not count kstrl's run
journals against a component; what a `[stack]` check writes is caught by
`ks doctor --measure`, which refuses on every file `git status` shows
after the checks ran on the base), and `protected_paths` (CI,
migration and deploy paths that `[policy] paths_deny` does not cover).

There are three verdicts. `ready` (exit 0): every check passed.
`ready-with-warnings` (exit 0): at least one check warned, none failed.
`not-ready` (exit 1): at least one check failed, most commonly no git
repository, no build manifest, or a `kstrl.toml` that will not parse. The report is also
written as JSON under `.kstrl/doctor/report-<UTC stamp>.json`.

`ks doctor --measure` adds one row, `base_gates` (#654): it runs your
test, typecheck and lint commands on the base branch, the reading
`ks factory` takes before any engineer runs (see "`ks factory` refused:
the base branch fails a gate" below), and prints it under `base_gates` in
the JSON report. A gate that fails there fails the row, so the verdict is
`not-ready` exactly where `ks factory` would refuse. A gate that measured
nothing (pytest collecting no tests, a timeout) warns. A base branch that
could not be measured at all fails the row, because a ready verdict must
rest on a reading. The branch is the one `ks factory --spec` uses when
`--base-branch` is not given. A flakiness smoke and a cost projection are
not built.

It also adds the `isolation` row (#700). For each of two zones, setup
(writes confined, egress open) and test (writes confined, egress
blocked, localhost allowed), it runs canaries through `nono wrap` and
the same canaries with no sandbox as their control, and lists every
verdict under `isolation` in the JSON report with nono's version and
the SHA-256 of the policy file it wrote under the control directory. A
canary counts as contained only when its operation failed with an
errno, and the egress canary only with EPERM. A canary whose control
also failed is uninformative, and a timeout is never contained. The row
is a record: a refused zone warns and never fails the verdict, because
no command runs inside a rung yet, and every verification record says
`none: ran on the host`. No prover exists today on any system but
macOS, because nono cannot express a localhost-only test zone on Linux.
There the row is one label, a warning, and no canary runs: `none: no
isolation rung exists on linux, so every command ran on the host and
nothing was isolated`. A run under a confirmed `[stack]` there runs its
setup, `up` and checks on the host instead of refusing, and every record
of it (the terminal, `isolation.json`, `base-gates.json`, the
`verification_result` event and the PR body) carries that label (owner
decision 2026-10-05, #700). The replay below runs nothing there for a
stack no person confirmed. On macOS a zone whose canaries fail is still
refused: the host fallback is decided by the platform, never by a failed
proof. On macOS with nono 0.79, DNS resolves
inside the test zone whatever the policy says; the owner decided
(2026-10-04, #700) to accept that gap rather than refuse the zone on
it, so the `dns` canary is recorded but never gates, and the test
zone's label says "DNS open" whenever it escaped. nono comes from
`KSTRL_NONO`, else from PATH.

Under a `[stack]` it also adds the `replay` row (#700 slice 3). The
stack may declare `up`, one command that starts the application and
exits 0 once it is ready; kstrl never reads what it does. The replay
checks the base commit out into a throwaway worktree, proves both zones
with only that worktree and the stack's `writable` and `readable`
granted, and runs `setup` in the setup zone, then `up`, then every check
in the test zone, stopping at the first that fails. Then it stops the
`up` process group by the id it recorded when `up` started and removes
the worktree. `up` and each check get `[verify] subprocess_timeout`,
the setup `[factory] worktree_setup_timeout`; a timeout is never ready.
The row names the stage that failed: `boundary_refused` (no proven
rung, so nothing ran; it warns), `setup_failed:<exit>`,
`up_failed:<exit>`, `up_timeout`, `check_not_runnable` (126, 127, a
timeout or output that is not utf-8) or `base_contradiction` (a check
that ran and failed on the base), with the last lines the command
printed. The replay runs whether or not the stack is confirmed, because
it is the evidence a person confirms it on. Its record is filed on the
stack's confirmation item when the stack is not confirmed or the replay
failed, and a stack whose newest replay failed is not confirmed until a
replay of the same text passes. Replays on one machine take turns on a
lock under the XDG state home; a second one prints that it is waiting.

## Acceptance checks (`ks factory --acceptance <dir>`)

#700 slices 4 to 6. `<dir>` must be outside the repository and hold
`plan.json`, plus any files its checks use:

```json
{"components": {"greeter": {"createsApp": false, "checks": [
  {"id": "greets-ada", "criterion": "greets by name",
   "argv": ["/bin/sh", "check.sh", "Ada"], "onBase": "fails", "heldOut": false}]}}}
```

Every key of a check is required; `onBase` is `fails` or `passes`. The
run needs a confirmed `[stack]`, because every check runs in the rung's
test zone, in a fresh copy of the plan as its working directory, with
`KSTRL_TREE` naming the checkout it checks. Exit 0 passes, any other
exit fails, and 126, 127, a timeout or output that is not utf-8 did not
run. kstrl copies the plan under the control directory by its digest,
and an L1 plan approval covers that digest.

After the plan gate and before the first engineer, every check runs once
on the base. `Refusing to run: the acceptance checks do not hold on the
base` (exit 2) names each check that passed where the plan says it
fails, failed where it says it passes, or could not run. A check that
could not run is allowed only in a component the plan marks
`"createsApp": true`, and that component is recorded as `base not
runnable`. Once the base accepts the plan, the manifest pins its
digest. A later run of the same plan refuses when the directory was
edited, naming both digests, and when it names no `--acceptance`.

After Phase 1 passes, each component's checks run on its head (each
check runs three times and passes only when every run exits 0; a run
that failed is never run again) and the verdict is
printed, written outside the repository under the control directory,
`runs/<run_id>/acceptance/<component>/`, and repeated in the PR body's
`## Acceptance` section. The verdict gates the component (#700 slice 6). A held-out check that fails halts it with
no retry, on a `halted_run` inbox item that names the failing checks and
the commit. Any other check that did not pass, held-out checks that
could not run included, goes to the engineer's retry: a visible check
with its criterion, its command and what it printed, a held-out check by
its id alone. The records are kept outside the repository, but the
engineer is not confined, so each record says
`"heldOutReadDenied": "unknown"`.

To merge over a halt, approve its item (`ks inbox approve <id>`), then
run `ks retry <component>`. The retry keeps the commit the item names
and judges it again with no engineer. The acceptance checks pass it only
when the approval names every check that fails there, and the terminal
and the PR body then name the approval, who gave it and when. A
regenerated commit, or another failing check, halts again. A single-PR
run, or a component built on unmerged dependency code, keeps no commit,
so its halt cannot be merged over.

`ks recheck <record.json>` runs a head record's saved checks again
(#700 slice 5). The path is read from the current directory, and
`ks factory` prints the absolute path of each head record on a
`- record:` line under that component's Acceptance lines. It refuses
(exit 2), naming the file, when the file is not found, when a file beside
the record does not match its `index.json`, when the saved checks are
not the record's plan, or when the `[stack]` in kstrl.toml is not the
one the record ran under. Otherwise the checks run again at the
recorded head commit, in the replay of that `[stack]`, and each check
gets one line: the verdict the record states, the verdict of its
recorded exits and the new verdict. Exit 0 means the three agree for
every check, and 1 that one does not. Both isolation labels are
printed, and nothing is written under the record's directory.

## Exit codes

Every `ks` command uses the same three codes, so a script or a scheduler
can act on the number alone.

- `0`: the command did what was asked and found nothing that needs you.
  An empty answer counts: an empty queue or inbox, no failure patterns
  yet, too little history for `ks health` to call a breach.
- `1`: the command did what was asked and the answer is a finding you
  act on: `ks check` failed a check, `ks check --compare-baseline
  --fail-on-regression` found a regression, `ks health` found a breach,
  `ks doctor` says not-ready, `ks config show` names a rejected section,
  `ks factory` finished with a failed or unmerged component, `ks queue
  sync` could not sync an issue, `ks serve` left work waiting on a human.
- `2`: the command could not do what was asked: a usage error, a
  `kstrl.toml` or environment value the entry check rejects, an input it
  needs that is missing or unreadable (no manifest yet for `ks status`
  or `ks retry`, an inbox or queue item that does not exist), a feature
  that is off (`ks queue sync`, `ks signals poll`), or a refusal before
  any work starts. `ks autonomy replay` exits 2 when there is too little
  history to replay, so a script cannot read "nothing replayed" as a
  pass.

A factory run you stop with `q` exits 130.

A green verdict from `ks doctor` is not the same as a spec being ready
to run. Every report ends with the same four sentences, because none of
this is a small print an operator should have to find on their own:

- A green verdict is repo-readiness, not spec-readiness. Nothing here
  can tell you whether the work you are about to describe fits the
  component model.
- kstrl is not for cross-cutting refactors. The factory decomposes a
  spec into components that each merge on their own, and a change that
  has to land everywhere at once has no such decomposition.
- kstrl is not for spec-free exploration. Every iteration is graded
  against a PRD, so work whose acceptance criteria are not known yet has
  nothing to grade.
- Tier A reads the repository and runs none of your commands.
  `ks doctor --measure` runs your test, typecheck and lint commands once
  on the base branch, as `ks factory` does before any engineer and
  refuses to start when one of them fails there. One run cannot tell you
  whether your suite is fast or flaky.

## `ks factory` refused: the base branch fails a gate

**Symptom**: `Refusing to run: the base branch fails a gate Phase 1 runs, or its reading cannot be recorded`, exit 2, and no engineer was called.

**What it is**: before the first engineer call, `ks factory` runs every check of the confirmed `[stack]`, with Phase 1's timeout, on the commit the base branch names, in a throwaway worktree under `.kstrl/contract/` (#654). `ks run` and `ks retry` reach the same check. It never measures your checkout: components are cut from the commit, so a fix you have not committed does not count. Any check that does not pass refuses the run, as do a failed setup and output the checks leave behind, and the refusal names each check. Without the refusal every component fails Phase 1 on the same failure after its engineer has been paid. On `ks factory --spec` the architect runs, and is paid, before this check. A gate that ran and measured nothing (pytest collecting no tests, a timeout, a tool that is not installed) is printed as `measured nothing` and does not refuse, because Phase 1 still fails that row on every component. A base branch that does not resolve, a checkout that fails and a `worktree_setup_command` that fails on the base are printed the same way. Every reading is written to `.kstrl/runs/<run_id>/base-gates.json` beside `launch.json`: the base sha, each gate's row with its failing names, the gates that were turned off, and whether and why the run refused. A run that cannot write the file refuses. `ks doctor --measure` takes the same reading without starting a run.

**Resolve**: make the base green in a commit; or pass `--accept-red-base <sha>`, at least 12 characters of the base commit's sha (the refusal prints the first 12), to run on that commit as it is; or pass `--no-verify`, which turns off all of Phase 1 and this check with it. Under `--no-verify` the record says `--no-verify: Phase 1 runs no gate`.

`--accept-red-base` is for one commit and one run (#654). A base that moved since refuses again, and so does a value shorter than 12 characters or one that is not the start of the measured sha; the refusal names the value and the sha. `ks retry` replays it from the launch record. With `--no-verify` it refuses, because no base is measured. It waives only a gate that ran and measurably failed. Under a `[stack]` that is a check that exited with a status other than 0, 126 or 127; it never waives a failed setup (no check ran), a check that timed out or whose command was not found (it measured nothing, and no engineer's commit fixes it), or what a check leaves in `git status` (every engineer's diff would carry it). The run's `base-gates.json` records the value as `acceptRedBase` and the refusals it waived as `accepted`. Phase 1 still runs every gate on every component, so a component passes only once the base's failures are fixed on its branch.

**Under `ks serve`**: the daemon reads the refusal from the run's `base-gates.json` (`refused: true`), requeues the item with the attempt it was charged, pauses the queue with no expiry, and files one `halted_run` inbox item titled `Continuous intake paused: the base branch fails its own gates` per base commit. The poison streak does not move. Make the base green in a commit, then `ks queue resume`; the item runs on the next cycle. A run that refused because its reading could not be written leaves no such record, so serve poisons it as unclassifiable, as before.

## Refused: `[sandbox]` cannot reach a role

**Symptom**: `Refusing to run: [sandbox] is enabled and kstrl cannot apply it to a role this run would start`, exit 2, and no agent was called. Each line under it names one role: `the engineer`, `the code reviewer`, `the security reviewer` (`ks factory`, `ks run`), `the understand agent` (`ks understand`), `the engineer` or `the repair agent` (`ks feature`).

**What it is**: the sandbox is applied by the claude-code, claude-sdk and codex adapters. A custom agent command (`--agent-cmd`, `[agent] command`, `AGENT_CMD`, `--review-agent-cmd`, `--security-agent-cmd`, `--repair-agent-cmd`) is an arbitrary shell command with no sandbox surface, so kstrl refuses to start it rather than run it outside the boundary you asked for (#701). A reviewer whose phase is off (`review_mode = skip`, security `skip`) is not named. The integration reviewer uses the code reviewer's command, so `the code reviewer` covers it. The code and security reviewers fall back to the engineer's command when none of their own is set, so a custom engineer usually names them too. On `ks factory` and `ks run` the refusal comes after the run's launch record, `.kstrl/runs/<run_id>/launch.json`, is written.

**Resolve**: run the named role on an adapter (unset its custom command and set `[agent] type`, or the reviewer's agent type), or turn the sandbox off with `[sandbox] enabled = false` or `KSTRL_SANDBOX_ENABLED=0`.

## Phase 1: mechanical verification failed

**Symptom**: `Phase 1 FAILED for <comp_id>: <check_names>`

**Diagnose**:

- `prd_stories`: the agent never set `passes: true` on its assigned story. Either the iteration ran out, or the agent didn't understand the PRD. It also fails with "The PRD this run started from, <path>, could not be read" when that pre-run copy went missing or stopped parsing during the run: restore it and re-run. It also fails with "The PRD is not the one this run started with" when the component rewrote its own PRD in a way no engineer may: a story's criteria, title, priority or id, the approved fixtures, or `branchName`. Only `passes` and `notes` are the engineer's to write. Restore the file from the pre-run copy, which for a component with a `planId` in the manifest is `.kstrl/plan/<planId>/<component-id>/prd.json` and for `ks run` or a manifest you wrote yourself is the file at its `prdPath` in the main checkout; do not adjust what the component is measured against. Editing `allowedPaths` is NOT this failure and never has any effect: scope is resolved once, before the first engineer call, and neither guard re-reads the PRD for it. `specIssues` is not compared either, and it is not validated beyond being an array: it is the architect's spec audit, routed here so the engineer reads it, and no gate judges the component against it. A component may annotate, resolve or delete that block and nothing will report it, including on a later iteration, since the worktree copy is seeded once. If you need the copy no component can reach, it is `scripts/kstrl/spec-issues.json`, written before any worktree existed.
- `test_suite`, `typecheck`, `linter`: the project's commands failed. To inspect the failed state, re-run with `--keep-worktrees-on-failure`: by default cleanup removes component worktrees at the end of the run, so `.kstrl/worktrees/<comp_id>/` will not survive a failed run without the flag. With the flag set, check the preserved worktree and rerun the command manually.
- `diff_scope`: the agent wrote files outside `ALLOWED_PATHS`. Tighten the allowlist or relax it as appropriate. Do NOT widen it to cover kstrl's own files: on `ks factory`, `ks run` and `ks understand`, the component's PRD, its progress log and `scripts/kstrl/codebase_map.md` are carved out automatically per component and reported separately in the failure as `plus harness artifacts:`. Widening to the bare `scripts/kstrl/` prefix to reach them exposes the manifest and every sibling component's PRD. `ks feature` carves out its own `.kstrl/logs/` run directory the same way. The allowlist it enforces is the one this run resolved before its first engineer call, from the component's `allowedPaths` or, when the PRD carries none, from the run-wide `--allowed-paths`; the `component_scope_resolved` event in the run's `events.jsonl` records which, per component. Editing the PRD mid-run does not move it. A scope that could not be READ at all is a different check, `scope_unreadable`, below. A lockfile is a file like any other (#696): it is in scope only when the component's `allowedPaths` cover it. A project whose checks write a lockfile the base does not track is refused at the base measurement, before any engineer runs; commit the lockfile or ignore it.
- `scope_unreadable`: this run has no scope for the component that it can trust, so it refuses without judging any diff. Two different faults produce it and they have different remedies, so read the `Error:` line before acting: either the component's pre-run PRD could not be read or parsed, or the manifest and the run's resolved scope disagree about which components exist and the component was never given a plan-time scope at all. The first is fixed by restoring the file the `Error:` line names: `.kstrl/plan/<planId>/<component-id>/prd.json` for a component with a `planId` in the manifest, or its `prdPath` in the main checkout for `ks run` and a manifest you wrote yourself. A copy at `prdPath` does not stand in for a missing planned copy. A manifest written by a kstrl between #545 and #568 has no `planId`, so its planned copies are never read: re-run its decomposition. The second fault is fixed by re-running the decomposition so the manifest and the scope snapshot are built together. **Setting `--allowed-paths` fixes neither**, even though it is the fallback when a PRD simply carries no `allowedPaths`: scope resolution returns `unresolved` before it reaches the flag, on the argument that a scope nobody could read is not a scope that does not exist, so a re-run with the flag set fails identically. Nothing the engineer writes can clear it either, since the file is outside every worktree and the snapshot is fixed for the life of the run.

  Where you see it depends on how far the component got. The scheduler checks the component's scope immediately before launching an engineer and fails it there (#294), so the normal case costs no agent call at all and never reaches Phase 1; the `phase` on the failure record is `scope`, not `verify`. `_preflight_component_scope` catches the common case earlier still, refusing the whole run before the first component starts. The Phase 1 check of the same name is the backstop for a worktree that already had an engineer in it, and is ungated: `[verify] check_diff_scope = false` switches off the diff COMPARISON, not the report that there was nothing to compare against. Because the launch gate sits outside Phase 1, turning verification off entirely does not turn this refusal off. Either way the component is failed immediately rather than retried, so you will not see three attempts burn on it, and the failure signature is `scope_unreadable:...`, which `ks autonomy` counts as an infrastructure abort rather than a decisive run.
- `bad_patterns`: a secret-like pattern landed in the diff, or a changed Python file is empty or does not compile. With `[policy] enabled = true` the envelope owns the secret rule (#646): a secret fails `policy_envelope` alone, as a `policy_secret_pattern` finding an inbox approval can waive, and the `bad_patterns` row says `secrets: checked by policy_envelope`. With the envelope off, `bad_patterns` keeps the secret rule and nothing can waive it.
- `policy_envelope`: the change broke a `[policy]` rule - a denied path, a size cap or a secret pattern. New dependencies are not a policy rule (#696): the security reviewer lists each one in the PR body. The envelope this run enforces is the one it resolved before the first component started; it is recorded as `policyHash` in the manifest and every component is held to that same object, so the hash and the enforcement cannot disagree (#192). Editing `kstrl.toml` while a run is in flight changes neither and takes effect at the next run. The adequacy posture and the autonomy level the Phase 1 gates use are resolved with it, so they do not move mid-run either; the level enforced is the CLAMPED one `ks factory` printed at run start, not the raw stored level. `[sandbox]`, `[fixtures]`, `[inbox]` and `[divergence]` are resolved in the same pass, and a section that will not resolve refuses the run with exit code 2, naming the section and the key, before the run directory exists - the entry preflight has already checked all of them, so the only way to reach that refusal is an edit made while the architect was running.
- `dead_code` / `mutation`: the optional advanced checks failed. `dead_code` is the vulture scan, or your own `[verify] dead_code_command` when you set one; the ruff F401/F811/F841 phase beside it reports as `dead_code_ruff` and can never fail a component, because auto-removing an unused import is not a verdict. A phase that could not run at all - no vulture on PATH, nothing non-test in the diff, a timeout, a detector that exited non-zero and reported no finding the check could read - gets NO row and is reported under `not measured` with the reason (#335), so a gate that never ran is never counted as a pass. It is also never a failure, since installing a binary is not something the engineer's next diff can do. The ruff phase needs **ruff 0.2.0 or newer** (January 2024): it pins `--output-format=concise` so a project's own `[tool.ruff] output-format` cannot remove the line it parses, and that flag value does not exist before 0.2.0. An older ruff on PATH exits 2 and the phase records `command_failed` carrying ruff's own `error:` line, so it is a visible refusal rather than a wrong number. A project's pinned ruff is far past this; the case to watch for is `ks check` against a live checkout with a system-wide old ruff first on PATH.
- `self_critique`: the engineer prompt's self-critique block is missing, too short, or filled with placeholder content.

**Resolve**: the agent retries automatically up to `FactoryConfig.max_retries` (default 3). After that the component is marked FAILED and cascade-skips dependents.

The retry prompt shows the failures measured in the most recent attempt first, under `## Current failures`, because those are the ones still happening; findings from an earlier attempt whose gate never ran again are listed separately under `## Not re-measured`, and the retry prompt tells the agent to re-check them rather than assume they still apply. Findings that a later attempt re-measured or got past are replaced by a one-line count under `## Resolved or superseded`, so reading the prompt and not finding an old failure means it was cleared, not lost.

Manual options:

1. Edit the PRD to clarify the story; re-run.
2. Increase `--max-retries`.
3. Re-run with `--keep-worktrees-on-failure`, then run the agent loop manually against the preserved worktree to debug interactively.

## `ks feature` reported `verification: FAIL`

**Symptom**: `verification: FAIL (N of M checks failed)` after the baseline, the implement phase or a repair attempt, with the command's exit code unchanged.

**What it is**: `ks feature` runs the same mechanical checker `ks factory` and `ks check` run, in the read-only mode `ks check` uses, against your live checkout: once immediately before the implement loop, and again after every engineer loop that actually called the agent (#288). It REPORTS. It does not gate: the flow's control flow and exit codes are exactly what they were, and a failure is not routed into the repair loop. Each verdict also lands in the run's `events.jsonl` as a `verification_result` event carrying `phase` (`baseline`, `implement`, `repair-2`) and `advisory: true` - the copy that matters under `--implementation-auto-run`, where nobody is reading the terminal.

**Was it the agent, or was your tree already broken?** That is what the `baseline` row is for. This report measures the WHOLE checkout, not the diff, so a checkout whose lint was already red before the agent started would otherwise read as the agent having broken lint. On the terminal, a failure that was already failing at baseline is called out under the verdict. In `events.jsonl`, diff the `phase: "baseline"` event's `failures` against the later one: anything in both was not this loop's doing. Only the baseline is ever the before-picture: a repair report is attributed against the baseline, never against the implement report that preceded it, so a failure the implement loop caused is never excused by the repair report that follows.

**When there is no `baseline` row**: the comparison is unavailable, not merely missing, and the reports that follow stand on their own. Three reasons, all stated on the terminal.

- `Verification report (baseline) skipped: --no-verify` - you declined.
- `Verification report (baseline) skipped: the implement loop will check out the existing branch ...` - your PRD's `branchName` names a branch that exists and is not the one checked out now. `run_loop` performs that checkout AFTER the baseline would have run, so a measurement here would be of a tree the loop never sees. Check out the branch yourself first and re-run if you want the baseline.
- You stopped the run before it got there.

**What it measures**: every check of your confirmed `[stack]` (rows `stack:<name>`), printed before they run so a long suite is not a dead terminal. Plus `self_critique` if, and only if, you set BOTH `[verify] require_self_critique` and `[verify] progress_file_path`; without the second key there is no PRD sibling to derive the log from on this path, so the check is skipped rather than pointed at a file that may not exist. When it does run it is announced on a `reading:` line. When you asked for it and it cannot run, the report says `NOT running self_critique` and names the missing key rather than quietly reporting a clean pass over three checks. Point `progress_file_path` at the same file as `[paths] progress`, which is where the engineer prompt tells the agent to write.

The checks are the literal commands of the confirmed `[stack]`, read once per run, so the baseline, every later report, and the `[stack]` block the engineer is given all name the same strings. With no confirmed `[stack]`, `ks feature` refuses before the understand loop (exit 2) unless you pass `--no-verify` (#696).

Every check that answers its question by reading `git diff <base>...HEAD` is deliberately skipped and named in the report (`verify.DIFF_DEPENDENT_CHECKS`: `diff_scope`, `bad_patterns`, `policy_envelope`, `test_adequacy`, `dead_code`, `mutation_testing`, `patch_coverage`, `diff_mutation`). Nothing in this flow commits, and the branch it works on comes from the PRD's `branchName`, which may be the base branch itself, so that diff is empty whenever the agent left its work uncommitted or worked on the base branch - and an empty diff is indistinguishable from nothing changed. Those checks would report a pass having measured nothing, which is worse than not running. Use `ks check` when you want them, on a checkout where the diff is real. `dead_code_ruff` is not in that list and does not need to be, since ruff scans `.` and has an honest answer with no base to diff against - but it is suppressed here all the same, because one toggle (`[verify] dead_code_cleanup`) owns both dead-code phases and the narrowing turns it off. The report names it on a line of its own, with that reason rather than the diff one, so nothing is suppressed here without being named.

**When it does not run**: any exit before the implement loop (understand incomplete, review gate declined, review gate unavailable in a non-TTY, a PRD with no user stories), an engineer loop that never called the agent, and a loop you stopped. The last one is deliberate: stopping should not make you wait out a test suite. A stop pressed while a report is already running cannot be honoured - each command is killed at its own `[verify] subprocess_timeout` when that is set, so that window is bounded by one per check. Unset (the default), it is not bounded.

**What it costs, and how to decline**: one run of every `[stack]` check for the baseline, plus one per engineer loop, so `2 + repair_max_runs` at worst. Measured on the kstrl repo itself: 246s per report, essentially all test suite, against 317-348s for the engineer loop each one follows. On a project with a fast suite it is seconds; on a 20-minute suite it is not.

Pass `--no-verify` to run none of them, the same flag `ks run` and `ks factory` have always had, and the only way to run with no `[stack]`. It also stops the engineer prompt being given the `[stack]` block, which is the point: do NOT instead confirm a `[stack]` of no-op checks, because the SAME config feeds that block, so the agent would be told a gate will run commands that do nothing.

**`verification: could not run`**: the measurement itself failed to start (a `Popen` that could not fork, a removed working directory). The event records `passed: false` with an EMPTY `checks` list, which is the unambiguous "nothing was measured". It never halts the flow.

**Diagnose**: run the failing command yourself in the checkout. The `running:` lines in the report name the `[stack]` checks, and they are in the run's `events.jsonl` and in `.kstrl/logs/feature_<name>/`. The `[stack]` in kstrl.toml is what ran: it is the only source of these commands.

**Resolve**: fix the code, or fix the command in `kstrl.toml`. There is no retry to consume and no gate to override.

## Phase 2: review failed (hard mode)

**Symptom**: `Phase 2 FAILED for <comp_id>: N failures`

**Diagnose**:

- Inspect `comp.review_findings` (also written to the PR body when the PR gets created).
- If the failures are PRD-criterion failures, the diff genuinely does not implement what was asked.
- If the failures are concerns (`scope_creep`, `security_concern`, `test_quality`, `unrelated_change`, `dead_code`, `error_handling`, `copy_paste`), the reviewer surfaced cross-cutting issues.

**Resolve**: the retry path injects the review findings back into the agent's context so the implementer has a concrete checklist. If the reviewer is wrong, switch the run to `--review-mode advisory` and the failures become warnings.

If `ReviewResult.infrastructure_error=True`, the reviewer agent itself failed (timeout, API outage, parse error). Same retry path, but check API health.

## Phase 2: claim disagreement

**Symptom**: `Phase 2 FAILED for <comp_id>: claim disagreement on N story(ies); passes reverted in the PRD`, with `failed_check = claim`.

A story is marked done when the engineer agent sets `passes: true` in the PRD. That is the agent that did the work reporting on the work, so it is a claim rather than a measurement. R10.3 checks the claim against the reviewer's per-story verdicts, which are an independent reading. This fires when the engineer said done and the reviewer did not confirm it - because it judged a criterion unmet, raised an advisory on one, or never covered the story at all.

**Diagnose**:

- Look for `claim_disagreement` findings in the PR body, under the callouts block. Each names the story in `location`, the reviewer's verdict in the explanation, and the criteria it would not pass in the suggestion.
- The PRD itself carries the audit trail: each reverted story gains a `reverted by reviewer (attempt N): <criterion>` note.
- The explanation says how the claim failed to be confirmed, and the three readings mean different things. A verdict of `fail` or `advisory` means the reviewer looked and was not satisfied. "not covered" means it returned no verdict for that story at all, usually a story the diff did not touch. "pass on only N of M acceptance criteria" means it passed everything it judged but did not judge everything: the story is unconfirmed rather than judged unmet, and the reviewer's coverage is what to look at first.

**Symptom, second form**: `Phase 2 FAILED for <comp_id>: claim agreement cannot be confirmed, the reviewer did not report`.

In advisory review mode a crashed or unparseable reviewer still passes the review (`passed = review_mode != hard`), so with `claim_agreement = "block"` a story claiming done would otherwise sail through with nothing having checked it. Nothing is reverted in this case: no evidence points at any story. The failure is recorded as `failed_check = infrastructure` and journalled as `review:infrastructure`, not as a disagreement, because no reviewer disagreed with anything. Check reviewer API health, as for any `infrastructure_error`, and re-run.

**Symptom, third form**: `Phase 2 FAILED for <comp_id>: Claim agreement cannot be confirmed: the reviewer never ran (adversarial LLM budget (N) exhausted) and a story is still marked passes=true`.

The adversarial budget covers review, security and knowledge distillation together. When it runs out, an **advisory** Phase 2 downgrades to a skip, and in blocking mode a skipped reviewer cannot confirm anything. A **hard-mode** Phase 2 does not reach this form at all since R10.5: it halts the component, which is the symptom below rather than this one. This does not retry, because retrying cannot recover budget: raise `max_adversarial_calls`, or accept the components already done and re-run the rest.

**Resolve**: the retry resets `passes` to false on each unconfirmed story and puts the disagreement in the agent's context. The engineer's own story selection then picks the story up again, because it takes the highest-priority story where `passes` is false. Nothing needs doing by hand.

If `claim_agreement = "block"` is set together with `review_mode = "skip"`, the run warns at startup that the gate can never fire: with no reviewer there is no verdict to confirm with.

If the reviewer is the one that is wrong, set `[factory] claim_agreement = "advisory"` (the default). Disagreements are then recorded on the PR and in the journal without failing anything. Note the gate also blocks whenever the autonomy ladder is at L1 or above, regardless of this setting: autonomy tightens a gate and never loosens one, so turning it off there means turning the ladder down.

## Phase 2: the retry loop is diverging (#265)

**Symptom**: `divergence detector tripped: the change is outgrowing the reviewer. Across attempts 1, 2, 3 the review failed every time, the change got larger at every step (...), and not one of the reviewer's blocking findings was retired at any step (...)`.

In advisory mode (the default) this is a warning line and a `review_divergence` finding, and the component keeps retrying. Under `[divergence] mode = "block"` it is terminal, with `failed_check = divergence` and journal signature `review:divergence`.

The loop drove a component the wrong way. Every retry hands the engineer the review findings and asks it to address them, the engineer correctly answers by writing more code, and the change gets larger while the reviewer stays exactly as unhappy. #265 measured one such component at $21.44 and 71 minutes across four attempts, with zero completions, and that is what motivated the detector. It is deliberately narrower than that run: on the #265 trajectory itself (6 blocking findings, then 1, then 10) attempt 2 retired findings, which resets the streak, so this predicate would not have fired there. It catches the case where nothing at all is being retired.

**Diagnose**:

- The message carries the whole case: the attempt numbers, lines changed (added plus removed) per attempt, files touched per attempt, and the reviewer's blocking-finding count per attempt. The same series is on the `review_divergence` event in `.kstrl/runs/<run_id>/events.jsonl`, with `blocking` recording whether the trip actually failed the component. In advisory mode the finding is also journalled to `.kstrl/evolution.jsonl` as `findings_superseded` when the attempt is retried, so it survives a component that later passes.
- Read the per-attempt review findings alongside it. A genuine trip looks like the same objections restated attempt after attempt while the diff climbs. Retiring even ONE blocking finding at any step resets the streak, so a trip means none was retired at any of them.
- The known false-positive channel is the reverse of what most people expect. The retirement half reconstructs a finding's identity from reviewer prose, and any instability there (a reworded criterion, a moved line, a rephrased explanation) makes an old key vanish, which reads as a retirement and resets the streak. The heuristic therefore errs toward staying quiet. If a trip looks wrong, the thing to check is whether the reviewer really was repeating itself verbatim, because that is what it takes to fire.

**Resolve**: split the component into smaller ones, or narrow its PRD. The message says so because that is the only fix: the change has grown past what one review pass can converge on, and another attempt from the same branch can only add to it.

`ks retry <comp_id>` is the override. It resets `retries` and clears the finding stream, and the detector's reading history is in-run only, so a retry starts with a clean slate and a full retry budget. That is the escape hatch when the operator disagrees with a blocking trip.

To stop it failing components, set `[divergence] mode = "advisory"` (the default) so trips are recorded without blocking, or `mode = "skip"` to stop measuring. To make it more patient, raise `[divergence] growth_steps`; it must stay >= 1.

## Phase 2.5: security review failed (hard mode)

**Symptom**: `Phase 2.5 FAILED for <comp_id>: N failures`

**Diagnose**: same logic as Phase 2, but the findings are typed against the security taxonomy. Each finding has `category`, `severity`, `location`, `explanation`, `suggestion`. N is the number of findings at or above `[security] fail_threshold` (default `high`), the same number the `review_result` event records as `fail_count`.

**Resolve**:

- For genuine security issues, the retry context goes back to the agent.
- For false positives, switch to `--security-mode advisory` (findings logged, not blocking) or `--security-fail-threshold critical` (only critical findings block).
- If `infrastructure_error=True`, the security reviewer didn't actually run. In hard mode this fails the component; in advisory mode it passes with a warning.

## Phase 3: contract test breaker

**Symptom**: `Contract breaker '<comp_id>' sent back for retry`

**Diagnose**: the merged tier branch's tests failed; Phase 3 attributes the failure to a "breaker" component (the most recent one merged into that tier). The breaker gets reset to PENDING and re-runs.

**Resolve**: the system handles this automatically up to `max_retries`. If it keeps breaking, the integration is genuinely broken: inspect the merged tier branch, fix the spec or the components' contracts, re-run.

## Knowledge layer reports no valid fact

**Symptom**: `Knowledge: the distiller returned no valid fact (raw: ...)`

**Diagnose**: the distiller LLM returned output, the JSON parsed, but `_coerce_facts` rejected every fact. Common causes:

- Fact ids don't match `/^fact-\d{3}$/` (e.g. `fact-1` instead of `fact-001`)
- Unknown scope value (the agent invented categories beyond handler/adapter/schema/contract/invariant/gotcha)
- Empty evidence array
- Empty claim text
- Prompt-injection pattern matched in claim text (Phase A1 rejection)

**Resolve**:

- Inspect `.kstrl/knowledge/<comp_id>/<run_id>/_distill_raw.txt` (saved automatically on failure paths) to see the agent's actual output.
- If the agent consistently produces malformed output, the distill prompt may need to be tightened.
- If the line says `the distiller returned no facts` instead, the JSON didn't parse at all; usually means the agent emitted prose around the JSON.

## After a factory run, `scripts/kstrl/manifest.json` is modified

**Symptom**: `git status` shows `scripts/kstrl/manifest.json` modified after `ks factory` finishes, often by hundreds of lines.

**What it is**: the manifest is the factory's record of the run: each component's status, retries, PR number and merge commit. The factory rewrites it in your checkout as the run progresses, and `ks status`, `ks retry`, `ks inbox approve` and `ks inbox retry` read it back. Nothing is wrong.

**Resolve**:

- While you may still retry a component or approve a parked merge from this run, leave the file as it is: those commands act on the statuses in it.
- Keep it out of unrelated commits. Stage the paths you changed by name rather than with `git add -A`.
- When you are done with the run, choose one. To keep the record in history, commit the file on its own, for example `git add scripts/kstrl/manifest.json && git commit -m "Record factory run <run id>"`. To drop it, restore the committed copy with `git restore scripts/kstrl/manifest.json`; `ks retry` and `ks inbox approve` can then no longer act on that run.
- The next `ks decompose` or `ks factory --spec` writes a new manifest over it either way.

## `git merge` of a component branch refuses: untracked `scripts/kstrl/feature/<id>/prd.json`

**Symptom**: merging a component branch into the checkout kstrl ran from fails with "untracked working tree files would be overwritten by merge: scripts/kstrl/feature/<id>/prd.json".

**What it is**: a kstrl from before #545 wrote each component's starting PRD at that path in your checkout, and the component branch commits the engineer's copy at the same path. kstrl now writes the starting PRD to `.kstrl/plan/<planId>/<id>/prd.json`, which no branch commits, and records the `planId` on the component in the manifest.

**Resolve**: move the file out of the checkout, merge, and move it back if you still need `ks retry` or a resumed run of that manifest. A manifest from before #545 has no `planId`, so its components start from the file at `prdPath`.

## Concurrent factory runs clobbering each other

**Symptom**: One run's worktree disappears or its branch gets force-pushed by the other.

**Diagnose**: on POSIX, Phase A4's `fcntl.flock` on `.kstrl/worktrees/<comp_id>.lock` should prevent this. On Windows there is no flock and the runs race.

**Resolve**: avoid running concurrent factory invocations against the same `root_dir` on Windows. On POSIX, the lock serializes worktree setup but doesn't prevent two runs from doing different work on the same component. Use distinct `root_dir`s for distinct factory invocations.

## Adversarial budget exhausted mid-run

**Symptom, advisory mode**: `Phase 2 SKIPPED for <comp_id>: adversarial LLM budget exhausted`, or `Phase 2.5 SKIPPED for <comp_id>: adversarial LLM budget exhausted`. The component continues and completes.

**Symptom, hard mode** (R10.5): the component halts instead.

```
Phase 2 FAILED for <comp_id>: Review infrastructure error: adversarial LLM budget (N) exhausted before the phase ran; hard mode refuses to merge unreviewed
Phase 2.5 FAILED for <comp_id>: Security review infrastructure error: adversarial LLM budget (N) exhausted before the phase ran; hard mode refuses to merge unreviewed
```

It is recorded as `failed_check = adversarial_budget` and journalled as `adversarial_budget:review` or `adversarial_budget:security`, with an `infrastructure_error` finding for the phase. The signature leads with the check name rather than the phase because `ks autonomy replay` reads everything before the first colon as the check: under a `review:` prefix a run whose reviewer never ran would have counted as a verdict about the factory's judgement. It does not retry: the budget only shrinks, so a retry would burn engineer iterations against the same cap. `ks serve` reads that `failed_check` and classifies the whole run as `budget_halt`, which is terminal: the queue item is not requeued, because the cap starts again at zero on the next attempt and the run would stop at the same component.

Three consequences worth knowing before you set a cap. Every component that depends on a halted one is SKIPPED (`cascade_skip`), so one halt near the root of the DAG can end most of the run. Each halted component files an inbox item keyed `halted:<comp>:<phase>:adversarial_budget`; `[inbox] open_item_cap` (default 50) stops `ks serve` admitting new queue work once the inbox is that full, so a cap far below the run's need can quietly reach it. And because `budget_halt` is terminal, `ks serve` poisons the queue item and counts it against `[serve] max_consecutive_poison` (default 3): three under-budgeted runs in a row stop the daemon admitting work at all, with `N consecutive items poisoned (limit 3); something systemic is failing, not one bad spec`. That is the breaker working as designed, and raising the cap is the fix, but it arrives long before the 50-item inbox cap does.

**Diagnose**: `FactoryConfig.max_adversarial_calls` is set and the count of review + security + distillation calls has hit the cap. Every one of those phases that runs costs one call per component, and the knowledge distiller spends from the same cap even though it gates nothing, so with hard review, hard security and `[knowledge] enabled = true` (the default) a run costs `3 * components`. Budget `3 * components`, or `2 * components` with `[knowledge] enabled = false`.

Anything less halts SOME component, but which one and at which phase depends on the component count, so do not read the two-component case as a rule. Measured: at two components and a cap of 4 (`2 * components`), comp-a completes and comp-b halts at security, because comp-a's distiller spent the call comp-b's security needed. At three components and a cap of 6 (`2 * components` again), comp-a and comp-b complete and comp-c halts at REVIEW, one phase earlier.

**Resolve**: raise the cap to cover the run, or set `review_mode` (and `[security] mode`) to `advisory` as a deliberate decision to merge on mechanical checks alone. Those are the only two: hard mode will not drop the reviewer to stay inside a budget. The knowledge distiller is still skipped rather than halted in every mode, because it is not a merge gate - but it is charged to the cap before it gets there, which is why it appears in the arithmetic above.

## Spec was rejected by the architect

**Symptom**: factory exits with code 2; stderr lists `[blocker/<kind>] <summary>` lines.

**Diagnose**: the architect's red-team pass found blocker-severity issues. The pipeline halts rather than implementing against a vague spec.

**Resolve**: read the surfaced issues, edit the spec to address them, re-run. There is no override flag; that's deliberate: the alternative was producing brittle code from ambiguous instructions.

## Calibration suite reports a regression

**Symptom**: `tests/test_calibration.py` test fails after a prompt edit; detection rate dropped.

**Diagnose**: the prompt change made the role miss a planted bug it previously caught.

**Resolve**: either revert the prompt change or update the fixture's `must_detect` if the change deliberately narrowed scope. Do not just unskip the test: a calibration regression is the signal you wrote the system to produce.

**With the autonomy ladder on**: `python -m kstrl.calibration compare <old> <new> --root <repo>` also opens a `calibration_drift` inbox item, deduped on the PAIR of baselines so re-reading the report does not add rows while a different old baseline against the same new one still gets its own item. When EITHER file has no `timestamp` key, the comparison is deduped on a digest of both baselines' detection rates instead, because the fill-in value that stands in for a missing timestamp is shared by every such file and is not an identity. With `[autonomy] demote_on_calibration_regression = true` it additionally demotes one level, trigger `calibration_regression`, once per comparison however often that comparison is re-run. `[autonomy] enabled` and both new switches are refused unless they are written as unquoted booleans: `= "false"` is a string, every non-empty string is true, and a typo that arms a switch which revokes autonomy is worse than one that does not.

Arguments in the wrong order are refused rather than acted on: `compare <newer> <older>` reads every recovered fixture as newly missed, so it reports a regression that is an artifact of the order. When both timestamps parse and the second file is the older one, the ladder is not consulted and the command says so.

With the ladder off it prints `autonomy ladder disabled; regression recorded in the report only`, followed by the root it consulted, so a mistyped `--root` (a directory with no `kstrl.toml` loads as "disabled") does not read as "the ladder is off". The exit code is unchanged either way (0 pass, 1 regression). Exit 2 now also covers a `kstrl.toml` that will not load OR cannot be read on a regression, because "the config is broken" must not read as "the ladder is off"; a passing comparison never consults the ladder, so it never refuses on the config. Anything that fails after the config resolves (the inbox write, the demotion itself) is reported on stderr and leaves the exit code alone: the regression is the measurement's answer, and the bookkeeping does not get to change it.

## The dashboard (TUI)

`ks factory` on a terminal runs the embedded dashboard by default
(`--no-tui`, `--ui plain`, or `KSTRL_NO_TUI=1` opt out; automatic
selection uses plain output for non-TTY stdio, while explicit `--tui`
requires a terminal). `ks dash` attaches a read-only
dashboard to a live run from another terminal, or replays a finished
one (`--run-id` takes a unique prefix; newest run is the default).

Keys: `enter` opens a component's detail (phase timeline, findings,
live transcript, evidence paths), `escape` returns, `f` toggles
transcript follow, `c` reopens a pending E6 checkpoint, `q` quits.

Quit semantics differ by mode. In `ks dash`, `q` detaches
immediately - the run is not yours to stop. Embedded, `q` asks first:
confirming group-kills in-flight agents, runs the worktree cleanup
pass, flushes the manifest, and exits 130; a second `q` (or second
Ctrl-C) force-kills. This is also what Ctrl-C now does in plain mode -
the pre-TUI behavior (skipped cleanup, orphaned agents) was a bug,
fixed in the same rewrite. Until #642 the confirm only set a flag, and
without worktrees the agent ran on until its iteration ended; every stop
request now ends the agents itself. A closed terminal (SIGHUP) is a stop
too: the run records the abort, flushes the manifest and exits (with
130, or 120 when the closed terminal makes the last write fail). A run
started under `nohup` ignores the hangup and keeps running.

Commands an agent's shell tool starts run in process groups of their
own, so ending the agent's group does not end them (#461). At the end of
every engineer attempt, and before any component worktree is removed or
recreated, kstrl kills the process group of every process whose working
directory is inside that worktree. One found at the end of an attempt or
at the run's cleanup is recorded on the component as an `orphan_process`
finding naming its pid and command; one found when a stale worktree is
pruned or a worktree is recreated for a retry is named in a warning line,
`orphan_process (stale worktree)` or `(worktree setup)`, which a factory
run also writes to its events.jsonl (#642).
The same kill runs before kstrl removes any other worktree it created
(#528): a Phase 3 contract worktree, the integration review's worktree,
and the failed attempt's evidence worktree that `ks retry` removes. No
component owns those, so each process found there is named in a warning
line, `orphan_process (contract)`, `(integration)` or `(retry)`, which a
factory run also writes to its events.jsonl. A shell you opened inside
any of these worktrees counts as one of those processes. If the census
cannot run (`lsof` missing, or listing nothing), the finding or the
warning says so instead of reporting a clean worktree.

When the kstrl process itself dies (#642). Every agent runs under a small
leash process, `kstrl/agents/leash.py`, which leads the agent's process
group and holds one end of a pipe to the kstrl process that started it.
However that process ends (SIGKILL, an OOM kill, a crash), the kernel
closes its end of the pipe, and the leash sends
SIGTERM to the agent's group, waits until nothing else is left in the
group or 5 seconds have passed, and sends SIGKILL (#708). A pool
worker whose parent dies ends too, and takes its agents with it. Measured
on macOS: the agent was gone within 0.04 s, a process in its group
that ignores SIGTERM within 5.04 s, and the leash itself within 0.10 s
when nothing in the group outlived the SIGTERM. Three things this does not cover. A
process an agent's tool started in a group or session of its own is not
in the agent's group: in a worktree the next run's prune kills it and
names it, as above, and in the project root nothing does. If the leash
is killed together with kstrl, its agent survives, and nothing reports
it yet. And
nothing is written when a leash fires: the run looks like any
interrupted run, with the manifest still `running`, an `events.jsonl`
that ends without `run_completed`, and the next run's recovery lines
naming what it reset and carried.

The same leash holds every verification command (#642 slice 5): each
check, setup and `[stack]` command that `verify.run_scrubbed` starts.
When the kstrl process that started a command dies, the leash ends the
command's group in the same order: SIGTERM, the 5 second grace, SIGKILL.
Under a `[stack]` the leash starts `nono wrap`, which replaces itself
with the command, so the command and everything it starts stay in the
leash's group. Measured on macOS, n=20 each at load average 21 to 25:
after a SIGKILL of `ks factory`, a base-gate check and a process it
started were gone within 0.021 s on the host and within 0.018 s inside
the nono rung. Two things an operator can see change. A command killed
by signal N reports exit 128 + N (137 for SIGKILL) where it reported
-N. And each command starts one more Python process, which measured
45 ms more per command at load average 24 (66.7 ms median against
21.7 ms for `true`). A stop does not end a running command: the run
waits for it to finish or reach its timeout, as it did before.

A `[stack]`'s `up` runs under the same leash in its hold mode (#642
slice 6). `up` exits while the servers it started keep running, so when
`up` exits the leash leaves with `up`'s status and a copy of it stays in
the group. When the replay ends, kstrl closes the copy's pipe and stops
the group. When the kstrl process dies first, the copy ends the group:
SIGTERM, the 5 second grace, SIGKILL. Measured on macOS inside the nono
rung, n=5 each at load average 14 to 18, after a SIGKILL of
`ks doctor --measure`: a server that honours SIGTERM was gone within
0.027 s, and one that ignores it was gone within 5.064 s. An `up` killed
by signal N fails the replay as `up_failed:` 128 + N where it gave -N,
and each replay takes 0.2 to 0.8 s longer than before (measured at load
average 15 to 32).

What a resume counts (#463). A retry count carries across runs on the
manifest, and a Ctrl-C does not reset it. A run that reached its summary
keeps the attempts and the spend it recorded, and the next run answers
for its attempts from there. A run that was killed before its summary
recorded no journal result, no experiments.tsv row and no run total, so
the run that resumes the manifest takes its record over: it prints
`Carried from interrupted run <id>: ...`, its run total and its cost
ceiling include the killed run's spend, and its journal and
progress.jsonl carry the killed run's retries and attempt readings. A
resume whose carried spend already meets `--max-cost-usd` halts before
the next call.

The E6 checkpoint modal shows the diff excerpt, review + security
findings, and the attempt's spend; approve/reject/retry with
`a`/`r`/`t`, or `escape` to leave it pending (the run stays blocked -
that is what a checkpoint is - and the banner points back at it).

Tradeoff to know: in embedded mode, notify hooks run with their
output captured (a hook writing to the terminal would corrupt the
alt screen - measured in the Stage 0 spike), so a `printf '\a'`
terminal bell only rings in plain mode. Everything the dashboard
shows also exists on disk: `.kstrl/runs/<run_id>/events.jsonl`
(schema-v2 event stream), `components/<id>/engineer.log` (agent
transcripts), `components/<id>/{review,security,distill}.log` (phase
transcripts), and `orchestrator.log` (embedded-mode narration). When
a run breaks, those files are the record; the TUI is only a view.

## Safe mode

kstrl is in **safe mode** whenever any of the four signals below is
degraded. It is one name for four states that already existed
separately, and asking about it costs nothing.

Two surfaces print it. `ks status` prints `safe mode: nominal` or
`safe mode: <n> reason(s)` followed by one line per reason, and
`ks serve --dry-run` prints the same block above its admission gates.

The dashboard shows it too. On a terminal `ks status` opens the
dashboard rather than the plain report whenever a run directory exists,
so `f2` opens a safe-mode panel from any screen, and a warning banner
appears under the run masthead the moment a signal goes degraded, naming
the sources.

A function key rather than a letter, and a priority binding rather than
an ordinary one. Neither is cosmetic. Textual's text inputs consume
printable keys before application bindings, so a letter key would
silently type itself into the launch, config, decompose and init fields;
and an ordinary application binding never reaches a system modal such as
the command palette. The argument below only holds if the key always
works.

The banner is hidden while everything is clear, and `f2` is what makes
that safe: the panel distinguishes the three states the banner cannot,
telling "not checked yet" apart from "checked and clear" apart from a
list of reasons. So an absent banner never has to carry a meaning on its
own. On the home screen, where there is room, a chip in the masthead
carries the same three states at a glance.

The run masthead has no chip, and that was measured rather than chosen:
at 120 columns the run header wants 41 cells and the cost meter 79, so
the topbar is already over-subscribed and anything added there costs the
run its own state label.

The dashboard re-checks every few seconds on a background thread, not on
its event poll: the predicate reads a run's whole event stream, and doing
that at the poll rate would stutter the display.

Safe mode itself refuses nothing. Each signal below already refuses
where refusing is right, and the predicate only reads them, so leaving
safe mode always means fixing the underlying signal rather than
clearing a flag.

A reason line looks like this:

```
  safe mode:      1 reason(s)
  - [queue] daily budget exhausted (see docs/runbook.md#queue-paused)
```

The label in brackets is the source; the anchor at the end is the
subsection to read here.

### Control directory untrusted

Live control state (`autonomy.json`, `pause.json`, `spend.json`,
`inbox.jsonl`, `github_processed.json`) must sit outside the tree the
agent can write to. When it does not, kstrl refuses to spend and treats
the queue as paused, because a factory that can edit its own budget
ledger is not bounded by it.

The detail line is the reason verbatim: `XDG_STATE_HOME` resolving under
the repository, legacy in-tree control files left behind by a partial
migration, a control file that is a symlink or resolves outside the
control directory, or a control directory that cannot be created or
listed at all.

To recover, point `XDG_STATE_HOME` outside the repository and finish the
migration. See "Control plane (R8.9)" under
[Where to find things](#where-to-find-things) for the exact layout, and
note that L3+ autonomy refuses to run at all while this is unresolved.

### Autonomy fell back or was clamped

Two distinct things land here.

**Fell back.** `AutonomyState.load` validates every field, not just the
level. Any malformed field, history entry, or out-of-range level
discards the whole record and returns a fresh L1 Supervised state,
because the safe direction for unknown autonomy is the least autonomy.
The earned level is lost. Restore `autonomy.json` from a backup if you
have one, or re-earn the level; `ks autonomy status` shows what the next
promotion needs.

**Clamped.** The run is executing below the level the ladder awarded.
Three independent ceilings can do this and the lowest wins: `[autonomy]
max_level` in `kstrl.toml`, `[policy] enabled = false` (L3 is
auto-merge inside the policy envelope, so with no envelope there is
nothing to merge inside and L2 is the ceiling), and control state that
still resolves under the repository (R8.9). The detail line names the
ceiling that fired. Clamping does not consume the earned level: raise
the ceiling and the level returns.

This source is silent while `[autonomy] enabled` is false, which is the
default. A ladder nobody switched on is not a ladder that fell down.

### Queue paused

`ks serve` admits no new work while the queue is paused. A pause is
either deliberate (`ks queue pause`) or self-inflicted by the daemon:
the daily budget stop sets tomorrow's local midnight as `resume_after`
and clears itself, and the poison breaker pauses after consecutive
poisoned items. Run `ks queue` to see the marker, and `ks queue resume`
to lift a pause that no longer applies. The resume also restarts the
poison streak at 0; without that the next cycle would pause the queue
again on the same streak.

An unreadable pause marker also reads as paused, and the detail line
says so. That is deliberate: resuming unattended spending on the
strength of a corrupt file is the one failure this marker exists to
prevent. Repair or delete `pause.json` in the control directory.

### An adversarial phase did not run

Review or security did not execute for at least one component of the
newest run. The count and the run id are in the detail line, and the
per-component reason is in the pull request body and in that run's event
stream (`.kstrl/runs/<run_id>/events.jsonl`, `phase_skipped` events).

The usual causes are `mode = skip` in `[security]` or the review config,
a security reviewer that was never configured, and the adversarial LLM
budget (`max_adversarial_calls`) running out mid-run. The first two are
choices; the third is worth acting on, because under an **advisory**
mode it means components merged on mechanical checks alone. Under a hard
mode the budget produces no skip at all since R10.5: the component halts,
and shows up as a failure rather than here. See also
[Adversarial budget exhausted mid-run](#adversarial-budget-exhausted-mid-run).

This reason clears when the next factory run **completes** without
skipping a phase. A run that is still in flight does not clear it: a run
writes its first event long before it reaches review, so treating "no
skip recorded yet" as "no skip" would clear the verdict the moment the
next run started. Runs of other kinds (`decompose`, `feature`,
`understand`) never clear it either, because they have no phase chain
and so finish clean by construction without ever asking the question.

A separate detail line beginning `could not read run` means the question
could not be answered rather than answered clean: the newest factory run
left no event stream (`[factory] progress_log_enabled = false` writes
accounting files and no events), or its `events.jsonl` could not be
opened. When that happens the last finished run's verdict is still
reported beside it.

## Standing feedback

When you find yourself correcting the same thing on a second pull request,
the correction belongs in `scripts/kstrl/memory.md`, not in another PR
comment. A comment steers one change; a line in that file is read into every
subsequent engineer prompt of every run that reads it at all, after the retry
context, so it steers those runs until you remove it. Which runs those are is
the paragraph below: `ks factory`, `ks retry`, `ks run`, `ks serve` and
`ks feature`, and not `ks understand`.

What belongs in it: permanent scope exclusions ("never touch the migrations
directory"), areas whose findings are known false positives, and review
feedback that should change how future work is done. What does not: one-off
instructions for the change in front of you, and anything that reads as a run
log. The file is version controlled and it is yours; with `[intake_github]
steer_enabled` on, kstrl is also a WRITER of it, through `/memory` comments
on its own pull requests (see below) - every write is an ordinary,
uncommitted change to your working tree, visible in `git diff` like anything
else you wrote by hand.

Two operational notes. `ks init` scaffolds it, and while it is unchanged
nothing is injected, so a fresh project pays nothing for the feature. Keep a
`## Guidance` heading in it: both the daemon's `/memory` writer and the
prompt-loader's truncation key on that heading specifically, not on the
literal end of the file, so a section you add stays yours - a `## Notes`
section written after `## Guidance` keeps its own content rather than
collecting future `/memory` appends. If you remove the heading, the next
`/memory` write adds it back at the end of the file rather than refusing.
The budget is about 1000 tokens; past that the block keeps the NEWEST
content of the `## Guidance` section and drops anything after that section
first, so your newest standing corrections are the ones that survive and
pruning the oldest lines from the top of `## Guidance` is what preserves
them. Both the prompt and the terminal warning say which section and
direction. `ks factory`, `ks retry`, `ks run`, `ks serve` and `ks feature`
read it; `ks understand` does not.

**The polled steering channel** (`[intake_github] steer_enabled`, off by
default): with issue intake also configured, a comment `/memory <text>` on
one of kstrl's own open pull requests appends `<text>` under `## Guidance`
the way described above, and `/iterate [text]` does that (if `text` is
given) and then re-queues the story that produced the PR, to run again once
it is merged or closed. Who may issue either command reuses
`[intake_github] allowed_actors` - there is no second allowlist. See
`docs/continuous-intake.md` for the full contract, including the one
efficiency-driven behaviour change: a per-pull-request watermark means a
comment refused before that watermark advanced is not re-read even if you
later widen `allowed_actors` to include its author.

Golden patterns truncate the other way, keeping the start and dropping the
end, because that file is written once and pruned by hand and its sections do
not carry an order.

## Where to find things

- Tracker for the hardening roadmap: `docs/adversarial-roadmap.md`
- Adversarial design overview: `docs/adversarial-design.md`
- Env-var reference: `docs/env-vars.md`
- Per-run captures: `.kstrl/evolution.jsonl`, `.kstrl/experiments.tsv`
- Run event stream + transcripts: `.kstrl/runs/<run_id>/`
- The PRD a decomposed component starts from: `.kstrl/plan/<planId>/<component-id>/prd.json` (#545, #568), where `planId` is the component's field in the manifest; one directory per decompose, and per run that built an integration fix. The component branch commits the engineer's copy at `scripts/kstrl/feature/<component-id>/prd.json`.
- Distillation debug dumps: `.kstrl/knowledge/<comp>/<run>/_distill_raw.txt` (on failure)
- Control plane (R8.9): `${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/`
  (`autonomy.json`, `inbox.jsonl`, `spend.json`, `pause.json`,
  `github_processed.json`). Marker `.kstrl/control_relocated` records where
  legacy in-tree files were moved. L3+ autonomy refuses to proceed while
  control state still resolves under the repo.
- Phase F sample real-world run log: `docs/phase-f-run-log.md`
