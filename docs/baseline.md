# The baseline

A baseline comparison stops the thing you are measuring from getting worse
while you improve it. kstrl's disturbances are ordinary and named: a teammate's commit, a
dependency bump, a base branch that moves under a component. Nothing in the
factory notices when one of those undoes progress the loop already made.

The mechanism is three parts and no LLM:

1. Run the mechanical checks on a known-good tree and record the structured
   failure signatures in a file the repository tracks.
2. Run the same checks on a branch.
3. Report what the branch ADDED.

It ships advisory. It prints the report, it exits 0, and it never fails a pull
request until somebody chooses that.

## The signature vocabulary

A signature is `"<check>:<code>"`: `linter:E501`, `typecheck:arg-type`,
`test_suite:assertion-error`. It comes from
`kstrl.evolution.signature_counts_from_verification`, the same function the
evolution journal records failures with, so the baseline and the journal cannot
disagree about what a failure is called.

The baseline counts OCCURRENCES, not distinct signatures. Twelve `E501`s are
twelve, so a branch that adds a thirteenth is a regression rather than a
no-change.

## Writing a baseline

```
uv run ks check --write-baseline
```

Writes `scripts/kstrl/baseline.json` under `--root`, and prints one line:

```
baseline written: scripts/kstrl/baseline.json (12 signatures, 47 total findings); unmeasured: none
```

Exit code follows the checks: 0 when the tree is green, 1 when it is not. A
RED baseline is expected and fine. The baseline exists for brownfield
repositories; recording what is wrong today is the point.

It refuses to overwrite an existing baseline. Pass `--force` when you mean to
replace one, and do it in its own commit so the diff shows exactly what moved.
Regenerating it on a branch cannot flatter that branch's own report, as long as
the workflow reads the baseline from the base ref rather than from the pull
request's checkout.

Two things to get right when you write one:

- **Write from a clean tree.** `base_ref` records the commit HEAD points at,
  not the working tree. A baseline written from a dirty tree names a commit
  that is not what was measured.
- **Read the `unmeasured:` list.** It names every check that was asked for and
  measured nothing: it timed out, its tool is not installed, or it applied no
  rule because none was configured. Those checks contribute NO signatures to
  the baseline, so a baseline with names on that line has holes in it, and the
  holes are wherever those names are. The file records the reason for each one
  in `unmeasured_reasons`. Fix the cause and regenerate, or accept the holes
  deliberately.

kstrl's own baseline names two: `diff_scope`, because `ks check` with no
`--allowed-path` applies no scope rule at all, and `bad_patterns`, because the
diff against the base on `main` is empty so it opened no files. Both are
vacuous passes.

These two holes do NOT surface as `stopped measuring`. That bucket is a set
difference taken from the baseline's own `measured_checks`, so a check that is
not in that list cannot enter it, whatever a later run does. They surface as
`new` instead: see [Comparing a branch](#comparing-a-branch).

| check | why the baseline never measured it | a branch's finding is reported as |
|---|---|---|
| `bad_patterns` | no files in the diff | `new` |
| `diff_scope` | No scope constraints (allowed_paths not set) | `new` |

The timeout is the other usual cause. kstrl's own test suite takes about 327
seconds and the default verify timeout is 300, so its own baseline is generated
with `KSTRL_TIMEOUT_VERIFY=1800`, pinned as `BASELINE_TIMEOUT_SECONDS` in
`tests/test_check_committed_baseline.py`; your workflow must set the same
value. A baseline and a comparison measured at different timeouts are not a
comparison, and that is enforced rather than asked for: the baseline records
a digest of the three verify commands and the timeout, and
`--compare-baseline` refuses a mismatch with exit 2, naming both digests.

## Comparing a branch

```
uv run ks check --compare-baseline
```

Five buckets:

| bucket | keyed on | means |
|---|---|---|
| `new` | signature | present now, absent from the baseline |
| `increased` | signature | present in both, and the count went up |
| `stopped measuring` | check | the baseline measured this check and this run did not |
| `fixed` | signature | in the baseline, absent now, and its check MEASURED something now |
| `unmeasured` | signature | in the baseline, absent now, and its check measured nothing now |

`new`, `increased` and `stopped measuring` decide the verdict. `fixed` and
`unmeasured` are reported so improvement is visible, and they never make a run
red.

`stopped measuring` is the one keyed on a check rather than a signature, and it
is not a duplicate of `unmeasured`. That bucket holds baseline SIGNATURES, and
a baseline can be green: kstrl's own records `"signatures": {}`. So on a branch
where the test suite stops finishing there is no signature anywhere for the
other four buckets to hold, and without this the report read `no regression`
and exited 0 even under `--fail-on-regression`. A check going dark is the most
important thing a pull-request check can catch.

The split between `fixed` and `unmeasured` is the part worth understanding. A
signature going away can mean two things: somebody fixed it, or the check
stopped running. Those look identical from the outside. `fixed` is a CLAIM that
the problem is gone, so it is only made when the check that produced the
signature measured something in this run; otherwise the signature lands in
`unmeasured` and the report says the check did not run. Without that rule,
uninstalling a linter reads as fixing every one of its findings.

What a gate says it measured is decided by its PARSER, not by its exit status.
`measured=True` means a parser for that gate saw its own tool reporting a
failure: pytest's summary line with a failure count, a FAILED or ERROR line,
mypy's `Found N errors in M files (checked ...)`, a ruff or eslint diagnostic,
a tsc `TS` code. Anything else is `measured=False` - an empty stream, a
traceback, a launcher's complaint.

Measured, running `check_linter` against a tool that is not installed, in both
command shapes:

    uv run <missing> check .   -> exit 2,   measured=False
    <missing> check .          -> exit 127, measured=False

and with a baseline holding `linter:E501` 12 and `linter:F401` 3, both compare
to `fixed={}`, `unmeasured={'linter:E501': 12, 'linter:F401': 3}` and
`stopped_measuring={'linter': ...}`.

Both shapes, because the exit status cannot tell them apart from a real
finding. The first version of this feature refused exit 126 and 127, which are
the POSIX shell's statuses for a command word it could not run - and the gate
commands kstrl resolves are `uv run pytest`, `uv run mypy .` and `uv run ruff
check .`, where uv spawns the child itself and reports its own exit 2. So the
`uv run` row above produced `fixed={'linter:E501': 12, 'linter:F401': 3}`:
uninstalling a linter read as fixing every one of its findings, for the exact
command shape everybody runs. Exit 2 could not simply be added to the refused
set either, since pytest, mypy and ruff all use 2 for their own errors.

A gate that PASSES is measured on its status alone, and that is a different
question with a different answer: a command that never started cannot exit 0,
while a clean run prints no failure for any parser to recognise.

A vacuous pass counts as measuring nothing for the same reason. `diff_scope`
with no `--allowed-path` applies no rule; over an empty diff both `diff_scope`
and `bad_patterns` apply their rule to nothing and report the same reason, `no
files in the diff`; and `bad_patterns` counts the files it OPENED, so a
deletion-only commit measures nothing however many files it names.

A signature from a check the BASELINE never measured is reported as `new`.
There are two ways to arrive there and they are not the same case.

Both `bad_patterns` and `diff_scope` only look at paths the branch put in the
diff, so a finding from either concerns a file the branch touched. A baseline
written on the base ref has an empty diff, so neither check has any paths to
compare with. `diff_scope` is exact: every path it can flag came from the
diff. `bad_patterns` is exact for its secret rule, which reads added lines.
Its empty-file and syntax-error rules read the whole file and then read the
same file at the MERGE BASE of the base branch and HEAD (#414), not the base
branch's current tip (#425), following a rename to the path the content came
from, so a finding either rule reports is one the base was not SHOWN to
already carry. A base read that cannot be done keeps the finding, which is
the blocking direction, so the two rules can still over-report when git
could not be asked. A finding the base did carry is counted in the row's
message and listed in its details for `ks check --json`.

For a TOOL-DRIVEN check the same rule does over-report: a baseline written
before `vulture` was installed leaves `dead_code` unmeasured, and the first
comparison after it is installed reports the tree's existing dead code as new.
Over-reporting costs a comment somebody reads; under-reporting costs the
mechanism.

### Formats and exit codes

- default: a plain-text report on stdout
- `--format markdown`: the same content as GitHub-flavoured markdown, first
  line `<!-- kstrl-sense-dampener -->` so a workflow can find and edit its own
  earlier comment
- `--json`: the whole `ks check` document with a `baseline` block added

`--compare-baseline` and `--write-baseline` both take an optional path, and a
RELATIVE one resolves under `--root`, exactly as the bare flag's default does.
One rule for one flag: passing the path `--help` advertises as the default,
together with `--root`, used to read a different file.

| condition | exit |
|---|---|
| no regression | 0 |
| regression, no `--fail-on-regression` | 0 |
| regression, with `--fail-on-regression` | 1 |
| a check that measured on the baseline and not here | 0, or 1 with `--fail-on-regression` |
| baseline missing, unreadable, malformed, or the wrong schema version | 2 |
| baseline measured with different verify commands or a different timeout | 2 |
| bad `kstrl.toml`, a path that is not a directory, git cannot diff | 2 |

The comparison's exit code never follows whether the tree is green. A red tree
is the normal state for the repository this exists for.

A baseline the tool cannot read is exit 2, never an empty baseline. Reading a
malformed file as `{}` would make every current signature `new`, or make every
baseline signature vanish, with nothing failing anywhere. The one thing that is
a note rather than a refusal is a `check_schema_version` that has moved since
the baseline was written: the document still parses, and refusing would break
every consumer's pull-request check the moment the check version bumped.

## kstrl does not run this on itself

`main` is green here, so the set of failures a branch ADDS and the set it HAS
are the same set, and `scripts/kstrl/baseline.json` records no signatures
at all. The second set is what the `test` and `lint` jobs in
`.github/workflows/ci.yml` already report, and unlike a baseline comment those
can fail the build. Measured over five runs before it was deleted, the job took
360 to 454 seconds per push to repeat them; over the last seven pull requests it
reported one finding twice, and both times the finding was WRONG: a false
positive, the secret-pattern fixture in `tests/test_verify.py` matching itself
rather than a real secret. That scanner defect is now filed as #399. So the
job went in #394, and `tests/test_own_ci_workflows.py` pins that this
repository's test suite runs in one workflow only.

The baseline file stays: it is the worked example this page points at, and
`tests/test_check_committed_baseline.py` checks it still matches this checkout.
The baseline comparison is for brownfield repositories, which is what the
rest of this page is about.

## Adding it to a repository

1. Write and commit a baseline:

   ```
   uv run ks check --write-baseline
   git add scripts/kstrl/baseline.json
   git commit -m "chore: record the check baseline"
   ```

2. Add a `pull_request` workflow that checks the branch out, runs `uv run ks
   check --compare-baseline <the baseline from the base ref> --format
   markdown`, and posts the report as one pull-request comment.
   [`docs/examples/check-baseline.yml`](examples/check-baseline.yml) is a
   complete worked example; copy it and adjust `KSTRL_TIMEOUT_VERIFY` to what
   your own baseline was written at. Six things in it are load-bearing and
   easy to lose:

   - `fetch-depth: 0` on the checkout. `ks check` asks git for the diff
     against the base strictly; a shallow clone cannot reach the base and the
     command exits 2 on every pull request. It is also what puts every head in
     `refs/remotes/origin/*`, which the next point needs.
   - the baseline read out of the BASE ref with `git show`, into a temporary
     file the run passes to `--compare-baseline`. The bare flag resolves inside
     the checkout, and on a `pull_request` event that checkout is the merge
     ref, carrying the pull request's own copy of the file: a branch that
     regenerates its baseline and commits it is then compared against its own
     signatures, and reports no regression. The step names the file after the
     base commit, so the report says which ref supplied the yardstick.
   - `timeout-minutes` above the WORST case, which is not one timeout. Six
     subprocesses in a check run are each handed `KSTRL_TIMEOUT_VERIFY` in
     full - the three gates, the two halves of the dead-code phase, and R8.5
     Layer 1's patch coverage run - so at 1800 seconds the job needs 180
     minutes plus install. This number has been wrong before; re-derive it
     rather than trust it, by walking `kstrl/verify.py` for every call that
     hands it `config.subprocess_timeout`:

     ```
     uv run python3 -c "
     import ast, pathlib
     src = pathlib.Path('kstrl/verify.py').read_text()
     calls = [a for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
              for a in [*n.args, *(k.value for k in n.keywords)]
              if isinstance(a, ast.Attribute) and a.attr == 'subprocess_timeout']
     print(len(calls))
     "
     ```

     prints 6 on this checkout. A cap below the worst case is a cancelled
     job, and a cancelled job produces no report at all - which is the one
     state this feature cannot report on.
   - `--base "$BASE_REF"` passed explicitly, from the event payload through an
     environment variable. A `pull_request` checkout is a detached merge ref;
     do not make base detection guess, and do not interpolate a ref name into
     a shell script.
   - the fork guard on the comment step. A pull request from a fork gets a
     read-only token whatever the `permissions:` block says, so the comment
     would fail. The report also goes to `$GITHUB_STEP_SUMMARY`
     unconditionally, which a fork author can read. Do NOT reach for
     `pull_request_target` to fix this: it runs the pull request's own test
     suite with a write token.
   - the last step, which fails on ANY nonzero exit from `ks check`. Without
     `--fail-on-regression` that is only the check failing, because a
     regression exits 0. It is also the half of
     graduating to blocking that is easy to lose: see below.

3. Set `KSTRL_TIMEOUT_VERIFY` in the workflow's env to whatever you generated
   the baseline with.

## Graduating to blocking

In this order, and do not skip the middle step:

1. **Advisory.** Start without `--fail-on-regression`. Read the comments it
   posts.
2. **Blocking.** Once the comments have been right on several real pull
   requests, add `--fail-on-regression` to the `ks check` invocation in the
   workflow. That is the whole change, and it is only the whole change because
   the last step keys on `steps.check.outputs.rc != '0'`.

   That detail is load-bearing, so do not simplify it away. The check step runs
   under `set +e` and restores `set -e` afterwards, so its OWN exit status is
   always 0 and the exit code reaches the job only through `GITHUB_OUTPUT`. The
   first version of this workflow failed on `rc == '2'` alone: adding the flag
   produced `ks check` exiting 1, `rc=1` recorded, and a green job. The
   documented graduation did not block. `bash -e -c 'set +e; false; echo
   "rc=$?"; set -e'` prints `rc=1` and exits 0, which is the whole mechanism.
3. **Refresh deliberately.** After an intentional change to the measured
   property, regenerate with `ks check --write-baseline --force` in its own
   commit, with nothing else in it, so the diff shows exactly which signatures
   moved and by how much.

The middle step is not ceremony. A baseline comparison that fails a
teammate's pull request before anyone has checked its output is a baseline
comparison somebody turns off, and a turned-off baseline comparison is worse
than none: it looks like a control.
