"""#700 slice 8: Phase 3 replays every component's acceptance checks on the merged tree.

Each component can pass its own checks on its own head and still break
another component's checks once the branches meet. Phase 3 merges the
branches, runs the ``[stack]`` checks, and then replays the acceptance
checks of every merged component on that commit, each ``HEAD_RUNS``
times, judged as a head is. A failure there fails the tier and the
bisection names the breaker. A failed held-out check is named by its id
alone and its breaker gets no retry (owner decision 3). A plan the
verification designer wrote is record only (owner decision 10): its
failures are printed and do not fail Phase 3.

End to end: the real ``ks factory`` as a subprocess on a real git
repository after the real ``ks init``, with a confirmed ``[stack]``, two
planned components and a stub engineer that adds one marker file named
for its worktree (the harnesses of ``tests/test_stack_e2e.py`` and
``tests/test_acceptance_e2e.py``).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.executables import write_executable
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import Run, _factory, _repo, _stack

COMPS = ("comp-a", "comp-b")

#: Each engineer adds a marker named for its own component.
MARKER = (
    'touch "$(basename "$PWD").marker" && git add -A && git commit -q -m marker >/dev/null 2>&1'
)


def _script(said: str, code: int = 1) -> str:
    """``--own COMP`` passes when the tree holds COMP's marker; with no
    argument the check passes until both markers meet, and then prints
    ``said`` and exits ``code``."""
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "--own" ]; then\n'
        '  [ -f "$KSTRL_TREE/$2.marker" ] || { echo "no $2.marker"; exit 1; }\n'
        "  exit 0\n"
        "fi\n"
        'if [ -f "$KSTRL_TREE/comp-a.marker" ] && [ -f "$KSTRL_TREE/comp-b.marker" ]; then\n'
        f'  echo "{said}"; exit {code}\n'
        "fi\n"
    )


def _check(check_id: str, argv: list[str], on_base: str, held_out: bool = False) -> dict[str, Any]:
    return {
        "id": check_id,
        "criterion": f"{check_id} holds",
        "argv": argv,
        "onBase": on_base,
        "heldOut": held_out,
    }


#: Passes on the base and on either head alone; fails where both markers meet.
APART = ["/bin/sh", "check.sh"]


def _plan(
    tmp_path: Path,
    said: str,
    checks: dict[str, list[dict[str, Any]]],
    *,
    designed: bool = False,
    code: int = 1,
) -> Path:
    """A plan outside the repository: plan.json with ``checks`` by
    component, and check.sh; with ``designed``, the file that marks a plan
    the verification designer wrote."""
    plan = tmp_path / "plan"
    plan.mkdir()
    write_executable(plan / "check.sh", _script(said, code))
    document = {"components": {comp: {"checks": rows} for comp, rows in checks.items()}}
    (plan / "plan.json").write_text(json.dumps(document), encoding="utf-8")
    if designed:
        (plan / "designer.json").write_text('{"writtenBy": "test"}', encoding="utf-8")
    return plan


def _run(tmp_path: Path, plan: Path, *extra: str) -> Run:
    root = _repo(tmp_path, _stack({"tests": "true"}), comps=COMPS)
    return _factory(
        tmp_path, root, "--acceptance", str(plan), *extra, contract="final", engineer=MARKER
    )


@runs_a_stack
@pytest.mark.parametrize("code", [1, 127], ids=["fail", "not_run"])
def test_phase_3_fails_on_a_check_that_breaks_only_on_the_merged_tree(
    tmp_path: Path, code: int
) -> None:
    """comp-a's check passes on the base and on comp-a's head, and comp-b's
    check needs comp-b's marker, so Phase 1 and both head gates pass. On the
    merged tree both markers meet and comp-a's check fails: Phase 3 fails.
    The bisection merges comp-a alone and replays only comp-a's check, which
    passes, then comp-b, where it fails: the breaker is comp-b. A bisection
    that replayed comp-b's check before comp-b was merged would blame comp-a.
    The run's summary carries the failing check's own output. Exit 127 is a
    check that did not run, and a check that did not run is not a pass."""
    checks = {
        "comp-a": [_check("markers-apart", APART, "passes")],
        "comp-b": [_check("has-own", [*APART, "--own", "comp-b"], "fails")],
    }

    run = _run(tmp_path, _plan(tmp_path, "both markers meet", checks, code=code))

    assert run.calls == 2, run.out
    assert "Phase 1 FAILED" not in run.out, run.out
    assert "contract tests FAILED" in run.out, run.out
    assert "breaker 'comp-b' (retries exhausted): both markers meet" in run.out, run.out
    assert run.code != 0, run.out


@runs_a_stack
def test_a_held_out_check_that_fails_on_the_merged_tree_halts_its_breaker_by_id_alone(
    tmp_path: Path,
) -> None:
    """The same check, held out, prints a random token when it fails. A
    retry is left, and the breaker comp-b still gets none (owner decision
    3): the run names the check by its id and never prints the token."""
    hidden = f"hidden-{secrets.token_hex(4)}"

    checks = {"comp-b": [_check("markers-apart", APART, "passes", held_out=True)]}

    run = _run(tmp_path, _plan(tmp_path, hidden, checks), "--max-retries", "1")

    assert run.calls == 2, run.out
    assert "contract tests FAILED" in run.out, run.out
    assert (
        "breaker 'comp-b' (a held-out check failed, no retry): "
        "acceptance:markers-apart (comp-b): fail" in run.out
    ), run.out
    assert hidden not in run.out, run.out
    assert hidden not in run.prompts, run.prompts[-3000:]
    assert run.code != 0, run.out


@runs_a_stack
def test_a_designed_plan_is_record_only_in_phase_3(tmp_path: Path) -> None:
    """The same check in a plan the verification designer wrote: Phase 3
    prints the failure as record only and passes, and the run exits 0."""
    checks = {"comp-b": [_check("markers-apart", APART, "passes")]}

    run = _run(tmp_path, _plan(tmp_path, "both markers meet", checks, designed=True))

    assert run.calls == 2, run.out
    assert "record only (designed checks): acceptance:markers-apart (comp-b): fail" in run.out
    assert "contract tests passed" in run.out, run.out
    assert run.code == 0, run.out
