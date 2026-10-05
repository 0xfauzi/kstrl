# The coordinator workflow

How one session triages the open issues and takes each to a merged pull request.
This is a record of what has actually been run, not a proposal. Every rule below
exists because something went wrong without it, and where a number appears it
was measured rather than estimated.

The shape is always the same: a stronger model turns the issue into an airtight
plan, a second stronger model attacks the plan, a weaker model implements it, an
independent stronger model tries to break the result with planted mutations and
closes the test gaps it finds, a simplify pass reads the finished diff from four
angles, and only then does it merge. The coordinator never implements and never
reviews its own work.

## Roles and models

| Stage | Model | Job |
|---|---|---|
| Plan | Opus | Reproduce the defect, decide the simplest fix, write an airtight plan, prototype it; on the small route, build it and open the pull request |
| Critique | Opus | Full route only: attack the plan, fix it in place, find what a weaker model would get wrong |
| Implement | Sonnet | Full route only: follow the plan exactly, tests red first, open the pull request |
| Verify | Opus | Assume it is wrong, run every plant, design its own, commit the tests that close a surviving plant |
| Fix | Sonnet | Address blockers, nothing else |
| Publish | Sonnet, low effort | Set the pull request body the lane script built from the verifier's result |
| Simplify | Opus x4 | Reuse, simplification, efficiency, altitude, on the finished diff |

The implementer is deliberately the weaker model. That is the whole reason the
plan and its acceptance checks have to be exact: every judgement left to the
implementer is a judgement made by the weakest link in the chain. On the small
route (below) there is no hand-off: the planner builds the change itself.

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

## What an issue holds, and what the plan adds

An issue states the goal and the evidence for it:

- **What** the change is, in one paragraph.
- **Why, measured.** The command that produced each number and its output. Never
  a number that was not run.

The planner turns the issue into the specification the implementer works from,
because the planner reproduces the defect on current main and the issue writer
usually has not. Of the last 40 issues an issue lane ran (#621 to #727), none carried
acceptance or plant sections, and every plan did. The plan carries these
sections, and vagueness in any of them becomes a defect later:

- **Change**, file by file, with the current code quoted and the new shape
  described.
- **Do not**, listing the specific mistakes a weaker model would make here.
- **Acceptance**, the exact commands that must pass.
- **Plants**, the mutations the verifier will apply, each naming the file, the
  edit, and the end-to-end test that must go red. A unit test is not a valid
  target: only end-to-end tests are committed, so a plant a unit test catches
  is a plant nothing on main catches.

The critic earns its stage on the plan. Over 136 issue-lane runs (2026-09-24 to
2026-10-05) it revised every plan and listed 1135 gaps, 4 to 12 per plan. Sorted
by keyword, the largest groups were guards, census pins or ratchets the plan
missed (322) and plants that would not have gone red (248).

## Small and full routes

The issue lane takes one of two routes, chosen after the plan, because the
critic and the separate implementer are worth their cost on large changes and
much less on small ones. The planner always prototypes the fix and measures it
with `git diff --numstat -- kstrl/`, counting product code only.

- **Small route**: fewer than 100 product-code lines, and the coordinator has not
  set `fullLane`. The planner builds the change itself: it sets the prototype
  aside, writes the end-to-end tests and shows them red, puts the change back,
  commits, opens the pull request and runs the full suite once. No critic and no
  Sonnet implementer run. An independent Opus verifier attacks the result next,
  as on the full route.
- **Full route**: everything else, as described above.

The cut is on product code because about 70% of the lines in a lane's pull
request are tests. Over 127 issue-lane runs with a pull request, measured by
product-code lines changed:

| Product-code lines | Lanes | First verify found a production-code problem | Median cost | Median time |
|---|---|---|---|---|
| under 100 | 42 | 2 (5%) | $9.2 | 83 min |
| 100 to 200 | 36 | 3 (8%) | $11.7 | 84 min |
| 200 to 400 | 25 | 3 (12%) | $12.4 | 97 min |
| 400 or more | 24 | 4 (17%) | $15.3 | 115 min |

In small lanes, the critic and the implementer together took 49% of the cost
and a median of 32 of 81 minutes. Where the planner prototyped, the prototype
was 95% of the final pull request's size at the median.

The coordinator sets `fullLane` for work in a new design area, whatever its
size. Five of the 12 lanes with a production-code problem were language-neutral
charter work (#696, #621, #623, #624, #629), at 161 to 787 product-code lines.

The limit: no run skipped the critic, so whether the critic is what keeps small
changes at 5% is unknown. `coord-ralph/tools/lane_stats.py` reports the
first-verify problem rate per route; if the small route's rate rises above the
full route's, set `fullLane` by default again.

## Lane shapes

Four scripts, chosen by what the work is.

- **issue lane**: plan, then critique and implement on the full route only,
  then verify, fix, publish. The default.
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
- The verifier closes a test gap itself when the production code is correct: a
  surviving plant, a census pin to re-derive, a test file over the length
  ratchet. It changes files under `tests/` only, shows the plant go red against
  the new test, and commits the test to the pull request. A change anywhere else
  is a blocker for the fixer. Before this rule, 110 of 136 first verifies ended
  on a test gap that the verifier had already written out in full, and the
  coordinator applied it by hand.
- A plant names an end-to-end test. The implementer may write unit tests to
  verify its own work, but they are scratch: run, then deleted before the
  commit. If no end-to-end test would go red under the plant, the lane builds
  one; "caught by a unit test" is not an outcome.

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
- The pull request fix lane rebuilds the body from its own verifier's result,
  as the issue lane does, so a fix never leaves the body stale.

The altitude reviewer has repeatedly been the most valuable, because it is the
only one asked whether the change is at the right depth at all. It has caused a
pull request to be closed and redesigned.

## Pull request bodies

No agent writes the pull request body. The lane script builds it from the
verifier's structured result at the end of the lane, and a low-effort agent sets
it and reads it back. The reason is timing: the implementer used to write the
body before the plants and the suite had run, so it carried "Full suite:
pending" and plants marked not yet run. 117 of 136 first verifies carried a
correction to the body, and the coordinator made each one by hand.

The built body holds the implementer's one paragraph on what was wrong and what
changed, the closing line (`Closes #N`, or `Part of #N` for a slice), the tests
on the final head by name, the plants as one line each with the outcome and the
failing count on the final head, the verifier's full-suite line, at most five
one-line handoffs, and the H1 line. A lane that ends unverified lists its open
blockers as handoffs marked `UNRESOLVED`. Measurement tables, reproduction
transcripts, design rationale and the history of fix rounds stay in the lane
directory. One batch measured hand-written bodies of 300 to 540 lines, and a
large share of every review addendum was corrections to claims inside them.

Verifiers run the full suite in parallel only. The merge gate and CI test the
merged tree, so a serial run inside the lane buys nothing.

## Merging

Two scripts, in order, and both must pass.

`premerge.sh` merges current `origin/main` into the branch, then runs the Mac
tests the change can affect, the type checker, the linter, every commit hook,
and the documentation freshness check on the merged tree, and checks the pull
request's shape. `land.sh` records the Mac scope and result in the body as a
"Merge gate" line.

The Mac run is scaled to the change because CI already runs the full suite on
Linux, on the merged ref, before `merge-chain.sh` merges, and again on the push
to main. The Mac run is there for what CI cannot see. #730's own new test failed at the
merge gate because the land chain runs under `nohup`, which leaves SIGINT
ignored in every child; CI does not run that way, and the launcher now resets
SIGINT.
`bin/affected_tests.py` selects the changed test files, the test files that name
a changed `kstrl` module, and every static guard, because a guard walks all of
`kstrl/` without importing the module it pins. It selects the full suite when
the change touches a `conftest.py`, `tests/helpers/`, `pyproject.toml` or
`uv.lock`, and `FULL=1` forces it. On five merges of 2026-10-04 and 2026-10-05,
the recorded test durations put the selected set at 23% to 69% of the suite
(median 49%), and a sixth merge touched `tests/helpers/` and ran in full. The
limit: a Mac-only failure in a test outside the selection reaches main, and
the next lane's own Mac suite is where it shows up.

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
neither attached to any conflict. So the merge and the CI run on the merged ref
are not optional, and every static guard's census is re-derived before and
after.

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
- **The full suite runs at most once per role, and only in three roles:** the implementer (on the final change), the verifier, and a fixer (after its fix). The planner, the critic and a finisher run only the test files the change touches, plus the guard and census files that pin that code. CI runs the full suite again on the merged tree, so nothing is lost. The full suite uses eight workers through `coord/bin/suite.py`, which holds a machine-wide lock so one runs at a time.
- **Clean up when a lane merges.** Delete its worktree, and delete any clone or virtualenv in its lane directory. Keep the lane's text records (plan, measurements, PR body).

**Option: background priority for long runs.** On Apple Silicon, `taskpolicy -b -p <pid>` moves a running process onto the efficiency cores. This cuts heat and fan noise, and the process runs slower. Processes it starts afterwards inherit the policy; this was measured on 2026-09-25 (priority 4, against 31 for a normal child). It suits long runs that nobody is waiting on, such as a paid calibration or a replay. Apply it to the whole process tree, including the wrapper shell, so later steps inherit it too. Do not use it on a lane whose result the next merge waits for: it trades wall-clock time for temperature. To start a run in the background from the beginning, use `taskpolicy -b <command>`.
