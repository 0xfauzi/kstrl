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
- **rank rule**: the R10.2 rule that files each retry-context entry by attempt and phase rank: the latest attempt's entries are current, earlier entries ranked above the latest failing phase were not re-measured, the rest are resolved and render as a count.
- **admission gate**: one check `serve_cycle` evaluates before claiming a queue item; a refusal is a wait, a pause, or a halt, and the gates run in a fixed order.
- **flag bundle**: the permissions an autonomy level grants, derived at run start and never stored, so it can only withhold what the ladder did not award.

## Log

### PR 221: kstrl as a control loop, and the end state it defines

- file: pr-221.html
- range: 94fdbbd..79196de
- parts: Sense, MechanicalVerifier, CLI, RetryContext, Pipeline, Scheduler, Reviewer, SecurityReviewer, ContractTester, FixturesOracle, Findings, PRD, SafeMode, AutonomyLadder, ServeDaemon, Dampener, EvolutionJournal, FlowControl, PullRequests, OperatorContext, WorkQueue, Steering, GitHubIntake, Calibration, Inbox, HealthTrending, EngineerLoop, Operator
- rules: H1, H2, H3, H4, 6, 7, 8, 9, 12, 13, 14, doctrine 5 (frozen phase count), doctrine 6 (no new outcome vocabularies)
- reverses: (none)

What the lesson taught. The end architecture as one clickable map: the atlas's own drawing of kstrl with the components the R10 plan reaches marked, a TODAY and END STATE toggle, a per-component panel, and a thirteen-step walk of one spec from labelled issue to journal that lights, on the same drawing, which component acts and which component measures each step. The walk makes the two open loops visible by their missing amber card: the engineer's inner loop (only the breaker and the path guard measure it, and neither reads the code) and the unbuilt operate loop. Eight decisions, each as a rule the reader operates: who may say a story is done (the set-point agreement rule with its blocking rule folded in), why the agent sees only what is failing now (the rank rule, with the corrected legacy special case), what happens when the review budget runs out, why the daemon can refuse work (the admission gates in the order the code evaluates them, with R10.7's bound placed where issue #228 puts it), how autonomy is earned and lost (the ladder with its clamps and the manual-override note), the order the context is assembled and why the memory file follows the retry context, why a sensor must run by hand first, and which loop a change belongs to (the observe band is not a loop; a sink is never control flow). Every rule was swept outside the page in Python, and the page's inline script was then run under node against the same grids: 960, 1560, 144, 110592, 75, 80, 31 and 34 rows agree, and every "try this" move reproduces. The build order, the graduation rule, the three prohibitions and where each new file lives are one table.

Two corrections the sweeps forced on the lesson's own drafts: the level-triggered retry context is bounded, not smaller (it is longer than today's for one or two attempts and adds one history line per further attempt where the old format adds a full failure text), and "every permission in the bundle is monotone" only holds once the merge gate is read as a restriction rather than a permission. One place the material and the brief disagree, decided for the material: issue #228 inserts the open-PR bound into the gates tuple after `check_budget`, which is before the inbox cap and the factory lock, so the lesson draws it there and says the issue's "only when everything else admits" holds for the three ledger gates only.

What the lesson could not confirm, because the run data is not in the repository: the journal figures (five runs, eighteen entries, `review:prd_criterion` at five, the reviewer at fourteen of seventeen signatures, `avg_iterations` at 1.00), the design's claim that the one-open-PR default is proven in production elsewhere, and the "after PR #237" state of the command tree, which is that pull request's description of itself while it was open. The tracker section every R10 issue points at was still absent from `main` when this was written; PR #239 restores it.
