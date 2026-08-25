# Lessons register

One entry per lesson under `docs/lessons/`, newest last, plus the agreed
vocabulary the lessons rely on. A lesson teaches a change on the day it
landed and is not edited afterwards; when a later change reverses one, the
new entry names what it reverses, the old entry gains `superseded-by:`, and
the old lesson gains a banner. The checker:

    python3 ~/.claude/skills/explain-pr/scripts/lesson_lint.py --register docs/lessons/register.md

Each lesson's widgets have their rules pulled into standalone scripts under
`docs/lessons/verify/<lesson>/`; run them to reproduce the sweeps the lesson
quotes.

## Glossary

Definitions of what each term is. Prefer the repository's own words; a wrong
entry is revised in place.

- **set point**: the state a run steers toward. Per story, the acceptance criteria and the `passes` flag; per component, the PRD; per merge, the policy envelope and the adequacy gate.
- **sensor**: anything that measures the tree independently of the agent that changed it: mechanical verification, the code reviewer, the security reviewer, contract tests, the approved-fixtures oracle, and calibration.
- **error signal**: the gap between the set point and the measurement. In kstrl, the typed `Finding` and the `check:code` failure signature.
- **controller**: the code that turns findings into the next action: `ComponentPipeline.process_result`, the scheduler, and the autonomy ladder.
- **actuator**: the engineer agent; after the release stage lands, also the release driver.
- **disturbance**: a change kstrl did not command: model non-determinism, a transport failure, a base branch that moved, a concurrent commit.
- **windup**: error accumulating while nothing discharges it, so the controller acts on a debt no longer owed. The retry context before R10.2.
- **level-triggered**: acting on what is failing now rather than on the history of what failed.
- **advisory first**: a gate ships measuring and reporting without blocking, and graduates to blocking by the operator's judgement after reading its output on real runs.
- **flow control**: a bound on work in flight (open kstrl-authored pull requests) so the loop cannot outrun its reviewer.
- **dampener**: a stored baseline of finding counts plus a per-pull-request report of what the branch added.
- **safe mode**: one named predicate over the degraded states that already exist (autonomy clamped, queue paused, control directory untrusted, an adversarial phase skipped).
- **golden patterns**: an operator-authored file stating what a good change looks like in this repository, injected into the engineer's context prefix.
- **memory file**: an operator-authored file of standing corrections, loaded last in the context prefix, after the retry context.
- **human on the loop**: the loop runs itself and the human intervenes on exception; the roadmap's "over the loop".
- **loop nest**: kstrl as six loops at different rates (implement, accept, integrate, intake, trust, learn) plus an unbuilt seventh (operate). The phase chain is one tick of loops 2 and 3.
- **set-point agreement**: the R10.3 rule that a story counts as done only when the reviewer's per-criterion verdicts confirm the engineer's `passes: true`.
- **ks sense**: the mechanical sensors run by hand against any tree: no PRD, branch, worktree or agent spend.

## Log

### PR 221: kstrl as a control loop, and the end state it defines

- file: pr-221.html
- range: 94fdbbd..79196de
- parts: cli, verify, context, pipeline, review, findings, factory, serve, pr, intake_github, workqueue, autonomy, autonomy_replay, calibration, inbox, statedir, loop, breaker, init_cmd, config, prd, ARCHITECTURE.md, README.md, CLAUDE.md
- rules: H1, H2, H3, H4, doctrine 5 (frozen phase count), doctrine 6 (no new outcome vocabularies)
- reverses: (none)

What the lesson taught. kstrl is not one pipeline but six loops at different rates, and the phase chain everyone quotes is one tick of the middle two. Naming the parts (set point, sensor, controller, actuator) makes three defects visible that the pipeline framing cannot express: the engineer agent is the only writer of `UserStory.passes` and the harness reads it back as if it were a measurement; the retry context accumulates across attempts and never discharges; and no sensor can be run without a full paid factory run, which is why every threshold in the codebase is an unmeasured placeholder. The lesson's widgets let the reader operate each of those rules and the rules that replace them: set-point agreement, the rank rule that makes the retry context level-triggered, the advisory-to-block ladder, the open-pull-request bound, and the budget halt. Each widget's rule was swept outside the page; the sweeps confirmed every prose claim they were built to test, and two of them corrected the source: `ks --help` lists fifteen commands, not fourteen, and the R10.2 issue's sentence about legacy attempt-0 entries contradicted its own rank rule in three of five cases (now special-cased in that issue).

What the lesson could not confirm, because the run data is not in the repository: the five-run, eighteen-entry journal figures, `review:prd_criterion` at five occurrences, and `avg_iterations` at exactly 1.00. The lesson marks each as unconfirmed rather than teaching it as fact.

What the lesson found wrong on the day it was written: the design doc's tracker section, which every R10 issue points at, had been deleted from `main` by a later commit on the same pull request. Restored separately; see the pull request that adds this register.
