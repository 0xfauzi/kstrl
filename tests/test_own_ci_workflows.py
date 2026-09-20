"""What this repository's own CI is allowed to run, counted rather than listed.

#394: the `sense dampener` workflow ran the project's full test suite a second
time on every pull request, to compare a branch against a committed baseline
whose `signatures` are empty. An empty baseline makes "what the branch added"
and "what the branch has" the same set, and the second set is what the `test`,
`spine`, `lint` and `coverage` jobs already report, with the power to fail the
build that the dampener step did not have.

The census is over every workflow file in the directory rather than over a
list of names, so a job that comes back under a new filename is an unexplained
delta rather than a miss. The equality is what makes the walk self-controlling:
a walk that stopped matching returns an empty census and fails against the two
ci.yml runs below, where `assert offenders == []` would have passed.

This guard does two different things, and they are wrong in different
directions. The TOKEN MATCH flags: it reads the text of `run:` scripts, so a
step that merely mentions `ks sense` in a diagnostic counts as one, and the
deleted workflow contributed 2 rather than 1 for exactly that reason. The cost
of that is a false positive somebody reads. The CENSUS EQUALITY clears: pinning
`EXPECTED_SUITE_STEPS` and `EXPECTED_REUSE_EDGES` is what licenses the
conclusion that nothing else in this repository runs the suite, so it has to
be narrow rather than permissive -- a shape it cannot see does not cost a
false positive, it costs the whole conclusion.

`_BLIND_SPOTS` below names what this walk cannot see, one line each on why,
tied to a single strict xfail so a later widening XPASSes rather than
silently staying green. A job-level `uses:` -- a reusable-workflow call --
was the undisclosed fourth one: A1 (#394) proved it by planting a caller of
`ci.yml` with no `steps` and getting `1 passed in 0.03s`, and it left this
record once `_reuse_edges` started covering it.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"

#: Tokens that mean "this step runs the project's test suite". `ks sense` is
#: here because it runs the same mechanical checks, the test suite among them.
SUITE_TOKENS = ("pytest", "ks sense")

#: Workflow file -> number of steps in it that run the project's test suite.
#: ci.yml runs it twice, once per tier: the fast tier (`-m "not spine"`) and
#: the spine tier (`-m spine`). Nothing else in this repository may run it.
EXPECTED_SUITE_STEPS: dict[str, int] = {"ci.yml": 2}

#: Workflow file -> number of jobs in it whose `uses:` calls another
#: workflow. Empty today: nothing in this repository reuses another
#: workflow. A job shaped this way has no `steps` key at all, which is
#: exactly what made it invisible before A1 (#394).
EXPECTED_REUSE_EDGES: dict[str, int] = {}


def _parsed_workflows() -> list[tuple[str, dict]]:
    """(workflow filename, parsed document) for every workflow file, parsed
    exactly once so `_run_scripts` and `_reuse_edges` below derive both
    censuses from the SAME read rather than opening every file twice."""
    import yaml

    paths = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    assert paths, f"no workflow files under {WORKFLOWS}"

    parsed: list[tuple[str, dict]] = []
    for path in paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), path
        parsed.append((path.name, document))
    return parsed


def _suite_steps_in_job(job: dict) -> list[str]:
    """`run:` script text for every step in `job` that has one.

    A step whose only key is `uses:` (a composite action, not a reusable
    workflow) has no `run:` and contributes nothing here -- see "local
    composite action" in `_BLIND_SPOTS` below.
    """
    return [str(step["run"]) for step in job.get("steps", []) if "run" in step]


def _run_scripts(parsed: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """(workflow filename, `run:` script) for every step in every job."""
    found: list[tuple[str, str]] = []
    for name, document in parsed:
        for job in document["jobs"].values():
            found.extend((name, run) for run in _suite_steps_in_job(job))
    return found


def _reuse_edges(parsed: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """(workflow filename, called workflow) for every job whose `uses:` calls
    another workflow. Such a job has no `steps` key at all, so `_run_scripts`
    above walks straight past it -- A1 (#394), proven by planting a caller of
    `ci.yml` with no steps and getting `1 passed in 0.03s`.
    """
    found: list[tuple[str, str]] = []
    for name, document in parsed:
        for job in document["jobs"].values():
            uses = job.get("uses")
            if uses is not None:
                found.append((name, str(uses)))
    return found


def test_this_repository_runs_its_own_test_suite_where_the_census_says() -> None:
    parsed = _parsed_workflows()

    # Not `dict(census) == ...`: Counter inherits dict.__eq__, and the bare
    # Counter compares equal just as well. Kept as a Counter, not cast, so a
    # failure here prints "Counter({...})" rather than a plain dict -- the
    # more informative repr, and the one chosen here.
    step_census = Counter(
        name for name, run in _run_scripts(parsed) if any(token in run for token in SUITE_TOKENS)
    )
    assert step_census == EXPECTED_SUITE_STEPS, (
        "a workflow in this repository runs the project's test suite "
        "somewhere the census does not expect (see the module docstring, "
        "#394). A workflow that genuinely needs to run the suite moves this "
        "dict in the same diff that adds it, with the reason in the commit."
    )

    reuse_census = Counter(name for name, _called in _reuse_edges(parsed))
    assert reuse_census == EXPECTED_REUSE_EDGES, (
        "a workflow in this repository calls another workflow with "
        "jobs.<id>.uses:, which schedules everything that workflow runs -- "
        "its own test suite included -- a second time (see the module "
        "docstring, #394). A workflow that genuinely needs to call another "
        "moves this dict in the same diff that adds it, with the reason in "
        "the commit."
    )


#: Shapes neither `_run_scripts` nor `_reuse_edges` can see today, one line
#: each on why. `kind` and `source` build the one step handed to
#: `_suite_steps_in_job` in the xfail below -- the SAME function the census
#: calls above, not a copy -- so a later widening of it is what turns one of
#: these cases into an XPASS. A job-level `uses:` used to be entry four here;
#: `_reuse_edges` covers it now, so it left this record rather than sitting
#: claimed-fixed in prose.
_BLIND_SPOTS: dict[str, tuple[str, str, str]] = {
    # name -> (reason, kind, source)
    "make target": (
        "a Makefile target can run pytest inside itself; the census reads "
        "only the run: text of the step that invokes make, never the "
        "Makefile",
        "run",
        "make test",
    ),
    "shell script under scripts/": (
        "a script under scripts/ can run pytest inside itself; the census "
        "reads only the run: text of the step that invokes the script, "
        "never the script",
        "run",
        "bash scripts/run-tests.sh",
    ),
    "local composite action": (
        "a step's own uses: is never read for suite steps; only a "
        "job-level uses: -- a reusable-workflow call -- is read, by "
        "_reuse_edges",
        "uses",
        "./.github/actions/run-suite",
    ),
}


def _sees(kind: str, source: str) -> bool:
    """Whether a step built from `kind` and `source` registers as a suite
    step under the same extraction and token match the census above uses."""
    step = {"run": source} if kind == "run" else {"uses": source}
    runs = _suite_steps_in_job({"steps": [step]})
    return any(token in run for run in runs for token in SUITE_TOKENS)


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_every_disclosed_blind_spot_is_still_actually_blind() -> None:
    """One assertion over every `_BLIND_SPOTS` entry, not one test per entry.

    `all()` over an EMPTY dict is vacuously True, so deleting every entry
    from `_BLIND_SPOTS` -- rather than actually closing one -- makes this
    assertion pass and XPASS the marker below, loudly, instead of the test
    quietly vanishing the way an empty `@pytest.mark.parametrize` would.
    A job-level `uses:` was entry four here until `_reuse_edges` (#394)
    started covering it, and it moved out of the record rather than sitting
    claimed-fixed in prose.
    """
    seen = {name: _sees(kind, source) for name, (_reason, kind, source) in _BLIND_SPOTS.items()}
    assert all(seen.values()), (
        f"still blind, as disclosed: {sorted(n for n, s in seen.items() if not s)}. "
        "If this assertion passes instead, either _BLIND_SPOTS is empty or the "
        "census now sees every disclosed shape; either way, move the resolved "
        "entries out of the record."
    )
