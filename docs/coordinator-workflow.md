# The coordinator workflow

How one session triages the open issues and takes each to a merged pull request.
This is a record of what has actually been run, not a proposal. Every rule below
exists because something went wrong without it, and where a number appears it
was measured rather than estimated.

The shape is always the same: an issue is made airtight, a stronger model plans
it, a second stronger model attacks the plan, a weaker model implements it, an
independent stronger model tries to break the result with planted mutations, a
simplify pass reads the finished diff from four angles, and only then does it
merge. The coordinator never implements and never reviews its own work.

## Roles and models

| Stage | Model | Job |
|---|---|---|
| Plan | Opus | Reproduce the defect, decide the simplest fix, write an airtight plan |
| Critique | Opus | Attack the plan, fix it in place, find what a weaker model would get wrong |
| Implement | Sonnet | Follow the plan exactly, tests red first, open the pull request |
| Verify | Opus | Assume it is wrong, run every plant, design its own |
| Fix | Sonnet | Address blockers, nothing else |
| Simplify | Opus x4 | Reuse, simplification, efficiency, altitude, on the finished diff |

The implementer is deliberately the weaker model. That is the whole reason the
plan and its acceptance checks have to be exact: every judgement left to the
implementer is a judgement made by the weakest link in the chain.

## Triage

Issues carry one priority label and the coordinator works top to bottom.

- `P0` not working: a runtime defect, a dying daemon, or a gate that lies
- `P1` blocks or wastes real operator spend
- `P2` real friction with a workaround
- `P3` papercut or polish
- `P4` deferred by decision; do not start in a stabilisation session

New features go last. The rule is a working core first.

Two things the coordinator checks before starting anything:

1. **Does the label still reflect reality?** An issue the owner has authorised
   but that still carries `P4` will be read by an agent as a blocker and the
   lane will refuse to build it. Relabel first.
2. **Do the file sets collide with a lane already running?** Lanes run in
   parallel only when the files they own are disjoint. Two lanes that touch the
   same documentation page or the same test module get serialised instead, and
   the second one is told what the first owns.

## Writing an issue a weaker model can build

An issue is the specification the implementer works from, so it carries these
sections. Vagueness here becomes a defect later.

- **What** the change is, in one paragraph.
- **Why, measured.** The command that produced each number and its output. Never
  a number that was not run.
- **Change**, file by file, with the current code quoted and the new shape
  described.
- **Do not**, listing the specific mistakes a weaker model would make here.
- **Acceptance**, the exact commands that must pass.
- **Plants**, the mutations the verifier will apply, each naming the file, the
  edit, and the test that must go red.

## Lane shapes

Four scripts, chosen by what the work is.

- **issue lane**: plan, critique, implement, verify, fix. The default.
- **feature lane**: three independent designs with tracer bullets, a judge
  panel, then the issue lane. For work where the solution space is wide and the
  wrong shape is expensive to unwind.
- **build from plan**: starts at implement, reusing a plan that already exists.
  For restarting a build stage without paying for the design work again.
- **pull request fix lane**: applies a coordinator-supplied blocker list to an
  open pull request, then verifies it. This is what a simplify pass feeds.

Each lane gets its own git worktree, created explicitly from `origin/main`, and
never touches the shared checkout or another lane's tree.

## Plants: the mechanism that makes verification mean something

A test suite that passes proves nothing about whether the tests would notice the
code being wrong. So every lane applies mutations and records an outcome per
mutation, from exactly four:

- **caught**: the named test went red
- **still green**: the mutation survived, which is a blocker
- **could-not-plant**: the anchor moved and the edit applied to nothing, which
  is its own outcome and never a pass
- **hung**: the run did not finish, which is distinct from a failure

Rules that came from getting these wrong:

- Purge `__pycache__` and set `PYTHONDONTWRITEBYTECODE=1` before every plant
  run. Two edits of the same size within one second reuse stale bytecode and
  report a false pass.
- Run plants serially. Parallel workers make a failure harder to attribute, and
  a plant must fail for the reason it was planted.
- Bound every run, and report a hang as a hang. Deleting a synchronisation
  point does not make an assertion fail, it makes it never run.
- The verifier designs at least one mutation of its own that a plausible wrong
  implementation would survive. Several real defects were found only this way.

## The simplify pass

Four independent reviewers read the diff, one angle each: reuse, simplification,
efficiency, altitude. They do not hunt for correctness bugs. They start when the
pull request opens, on a detached copy of its head, so they run alongside the
verifier and not after it.

Their findings are deduplicated into a coordinator addendum appended to the
lane's plan, grouped so the fix lane works them in order, with the groups that
change behaviour first. The addendum also states what the reviewers measured as
clean, so the fix lane does not re-litigate it, and what is explicitly deferred.

Not every finding earns a fix round. A second fix-and-verify cycle costs about
as much as the first (49 to 155 minutes, measured over one batch), so:

- `P0` and `P1` issues get the full cycle for every finding the addendum keeps.
- `P2` and `P3` issues get a fix round only for a finding that changes
  behaviour: a wrong result, a fail-open, a guard that clears what it should
  flag, or a control switched off. Everything else is written into the pull
  request's handoff list as a one-line follow-up.
- Corrections to the pull request body are made by the coordinator by hand. They
  never go through a fix lane.

The altitude reviewer has repeatedly been the most valuable, because it is the
only one asked whether the change is at the right depth at all. It has caused a
pull request to be closed and redesigned.

## Pull request bodies

Short and to the point: about 40 lines. One paragraph on what was wrong and what
changed, `Closes #N`, the tests added by name, the plants as one line each with
the outcome and the failing count on the final tree, the full-suite summary
line, at most five one-line handoffs, and the H1 line. Measurement tables,
reproduction transcripts, design rationale, commands that name scratch scripts
and the history of fix rounds stay in the lane directory. One batch measured
bodies of 300 to 540 lines, and a large share of every review addendum was
corrections to claims inside them, each paid for with a fix and a verify.

Verifiers run the full suite in parallel only. The merge gate runs it again on
the merged tree, so a serial run inside the lane buys nothing.

## Merging

Two scripts, in order, and both must pass.

`premerge.sh` merges current `origin/main` into the branch, then runs the full
suite, the type checker, the linter, every commit hook, and the documentation
freshness check on the merged tree, and checks the pull request's shape.

`merge-chain.sh` pushes, waits for remote checks, squash merges with the pull
request body as the squash message, deletes the branch and reports the issue
state. The body is passed explicitly because `gh` otherwise builds the squash
message from the branch's commits, and a commit message that says `Closes #N`
closes the issue even when the body deliberately says `Part of #N`. For the
same reason `premerge.sh` prints every closing keyword in the body, in any
case, before the merge.

Merge main from main's side when the lane is old. The complexity hook runs
`--staged`, which diffs the index against HEAD, so merging a moving main INTO
a long-lived lane fails on every function that grew on main and that the lane
never touched, and skipping the hook is forbidden. Check out `origin/main`
detached, merge the lane, resolve, commit with HEAD on main so the hook
measures only the lane's contribution, then point the lane branch at that
commit. The resulting tree is byte-identical to the other direction.

Merging into a moving main is where defects hide. Two have been caught only by
running the full suite on the merged tree, neither present in either branch and
neither attached to any conflict. So the merge and the re-run are not optional,
and every static guard's census is re-derived before and after.

## Rules that cost something to learn

**Measure against the tree you are actually shipping.** A shared checkout drifts
behind main while lanes merge. Verification done there describes a tree that no
longer exists. Use a worktree pinned to current main.

**A reviewer reads a branch, not the codebase.** Findings from a pull request
review are about that branch. Before filing one as an issue, check it exists on
main. Three were filed that did not.

**One defect class, one pull request.** Fix every instance plus the guard that
catches the next one. Filing siblings behind a single-site fix produces a pile
of issues that a later class change absorbs anyway.

**Scope discipline beats thoroughness.** A simplify pass that adds five groups
of work to a one-round change turns it into a multi-hour lane. Real findings
that do not belong get filed, with their measurements attached, not folded in.

**A guard that clears must be narrow; a guard that flags may over-match.** If a
clearing guard cannot prove a site is compliant, it must flag. A disclosed blind
spot needs an explicit record plus a strict expected-failure, so a later
widening fails loudly instead of silently agreeing.

**A prose disclosure rots.** A population measured once and written into a
docstring drifts with nothing failing. Record it as an executed census instead.

**The harness relays the triggering message to every agent as authoritative.**
Launching a lane while the last human message is narrow makes the implementer
refuse to build. Either launch on the broad instruction or restate the mandate
in the prompt.

**Keep coordinator tooling out of the lanes' scratch directory.** Lane agents
write there, and a merge script was deleted mid-session.

## Speed

The suite runs in one process by default, which leaves most of the machine idle
while several lanes wait. Running it with four parallel workers through a
one-invocation install measured 202 seconds against 545 serial at the same
commit, with identical counts, and leaves the project file and lockfile
untouched so it conflicts with no branch in flight. It is not used for a single
test, a small named set, or a plant.

If a parallel run and a serial run ever disagree on counts, that is a finding.
Report both; do not pick one.

## Machine budget

Running many lanes at once overheated the machine on 2026-09-25. Measured at the time: a load average of 22.5 on 10 cores, the macOS file-event daemon at 95% of a core and 6.9 GB of memory, 58 throwaway virtualenvs, and 23 GB of lane scratch. The agents themselves used little CPU. The load came from repeated full-suite runs and from copies of the repository. Three rules follow.

- **No repository copies and no new virtualenvs.** A lane runs code only in its own worktree, with that worktree's environment. Before the implementer starts, the planner and critic may prototype in the worktree. They save the change as a diff in the lane directory and leave the tree clean.
- **The full suite runs at most once per role, and only in three roles:** the implementer (on the final change), the verifier, and a fixer (after its fix). The planner, the critic and a finisher run only the test files the change touches, plus the guard and census files that pin that code. The merge gate runs the full suite again on the merged tree, so nothing is lost. The full suite uses three workers.
- **Clean up when a lane merges.** Delete its worktree, and delete any clone or virtualenv in its lane directory. Keep the lane's text records (plan, measurements, PR body).
