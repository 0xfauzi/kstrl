"""What this repository's own CI is allowed to run, counted rather than listed.

#394: the `sense dampener` workflow ran the project's full test suite a second
time on every pull request, to compare a branch against a committed baseline
whose `signatures` are empty. An empty baseline makes "what the branch added"
and "what the branch has" the same set, and the second set is what the `test`,
`spine`, `lint` and `coverage` jobs already report, with the power to fail the
build that the dampener step did not have.

The census is over every workflow file in the directory rather than over a list
of names, so a job that comes back under a new filename is an unexplained
delta rather than a miss. The equality is what makes the walk self-controlling:
a walk that stopped matching returns an empty census and fails against the two
ci.yml runs below, where `assert offenders == []` would have passed.

This guard FLAGS, so it is allowed to over-match. It reads the text of `run:`
scripts, so a step that merely mentions `ks sense` in a diagnostic counts as
one: the deleted workflow contributed 2 rather than 1 for exactly that reason.
The cost of that is a false positive somebody reads. Blind spot, stated: a
suite run spelled some other way, a make target or a composite action or a
shell script under `scripts/`, is not seen here.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"

#: Tokens that mean "this step runs the project's test suite". `ks sense` is
#: here because it runs the same mechanical checks, the test suite among them.
SUITE_TOKENS = ("pytest", "ks sense")

#: Workflow file -> number of steps in it that run the project's test suite.
#: ci.yml runs it twice, once per tier: the fast tier (`-m "not spine"`) and
#: the spine tier (`-m spine`). Nothing else in this repository may run it.
EXPECTED_SUITE_STEPS: dict[str, int] = {"ci.yml": 2}


def _run_scripts() -> list[tuple[str, str]]:
    """(workflow filename, `run:` script) for every step in every workflow."""
    import yaml

    paths = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert paths, f"no workflow files under {WORKFLOWS}"

    found: list[tuple[str, str]] = []
    for path in paths:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), path
        for job in document["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    found.append((path.name, str(step["run"])))
    return found


def test_this_repository_runs_its_own_test_suite_where_the_census_says() -> None:
    census = Counter(
        name for name, run in _run_scripts() if any(token in run for token in SUITE_TOKENS)
    )

    assert dict(census) == EXPECTED_SUITE_STEPS, (
        "a workflow in this repository runs the project's test suite somewhere "
        "the census does not expect. Deleting the sense dampener job (#394) is "
        "what set this pin: the committed baseline carries no signatures, so a "
        "comparison against it reports what the test, lint and coverage jobs "
        "already report, and pays a second full suite run to do it. A workflow "
        "that genuinely needs to run the suite moves this dict in the same diff "
        "that adds it, with the reason in the commit"
    )
