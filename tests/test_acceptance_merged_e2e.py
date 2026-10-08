"""#700 slice 8: Phase 3 replays every component's acceptance checks on the merged tree.

Each component can pass its own checks on its own head and still break
another component's checks once the branches meet. Phase 3 merges the
branches, runs the ``[stack]`` checks, and then replays the acceptance
checks of every merged component on that commit, each ``HEAD_RUNS``
times, judged as a head is. A failure there fails the tier and the
bisection names the breaker. A failed held-out check is named by its id
alone and its breaker gets no retry (owner decision 3). A plan the
verification designer wrote fails Phase 3 as an operator's does (owner
decision of 2026-10-07).

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

from tests.helpers import integration_harness as h
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
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
@pytest.mark.parametrize("designed", [False, True], ids=["operator", "designed"])
def test_a_held_out_check_that_fails_on_the_merged_tree_halts_its_breaker_by_id_alone(
    tmp_path: Path, designed: bool
) -> None:
    """The same check, held out, prints a random token when it fails. A
    retry is left, and the breaker comp-b still gets none (owner decision
    3): the run names the check by its id and never prints the token. A
    plan the verification designer wrote halts the breaker the same way
    (owner decision of 2026-10-07)."""
    hidden = f"hidden-{secrets.token_hex(4)}"

    checks = {"comp-b": [_check("markers-apart", APART, "passes", held_out=True)]}

    run = _run(tmp_path, _plan(tmp_path, hidden, checks, designed=designed), "--max-retries", "1")

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
def test_a_designed_check_that_fails_on_the_merged_tree_fails_phase_3(tmp_path: Path) -> None:
    """The same check in a plan the verification designer wrote fails
    Phase 3 as an operator's does (owner decision of 2026-10-07): the
    bisection names the breaker and the run fails."""
    checks = {"comp-b": [_check("markers-apart", APART, "passes")]}

    run = _run(tmp_path, _plan(tmp_path, "both markers meet", checks, designed=True))

    assert run.calls == 2, run.out
    assert "contract tests FAILED" in run.out, run.out
    assert "breaker 'comp-b' (retries exhausted): both markers meet" in run.out, run.out
    assert "record only" not in run.out, run.out
    assert run.code != 0, run.out


@runs_a_stack
@pytest.mark.parametrize("kept_too", [False, True], ids=["alone", "beside_a_kept_check"])
def test_a_designed_check_the_base_removed_does_not_run_in_phase_3(
    tmp_path: Path, kept_too: bool
) -> None:
    """A designed check that passes on the base it says fails on is removed
    (owner decision of 2026-10-06), so it runs on no head and does not run
    in Phase 3 either: a vacuous designed check cannot block a run (owner
    decision of 2026-10-07). The removal is by check, not by component: a
    kept check of the same component does not bring the removed one back."""
    checks = {"comp-b": [_check("markers-apart", APART, "fails")]}
    if kept_too:
        checks["comp-b"].append(_check("has-own", [*APART, "--own", "comp-b"], "fails"))

    run = _run(tmp_path, _plan(tmp_path, "both markers meet", checks, designed=True))

    assert "it is removed" in run.out, run.out
    assert "contract tests FAILED" not in run.out, run.out
    assert run.code == 0, run.out


#: Counts its runs in the tree under test, and fails only the second run
#: on a tree where both markers meet: one run of it passes, three fail.
TWICE = (
    "#!/bin/sh\n"
    'if [ -f "$KSTRL_TREE/comp-a.marker" ] && [ -f "$KSTRL_TREE/comp-b.marker" ]; then\n'
    '  [ -f "$KSTRL_TREE/run-1" ] || { : > "$KSTRL_TREE/run-1"; exit 0; }\n'
    '  [ -f "$KSTRL_TREE/run-2" ] || { : > "$KSTRL_TREE/run-2"; exit 1; }\n'
    "fi\n"
    "exit 0\n"
)


@runs_a_stack
def test_phase_3_judges_each_check_on_every_one_of_its_runs(tmp_path: Path) -> None:
    """A check that fails only its second run on the merged tree fails
    Phase 3: each check runs ``HEAD_RUNS`` times and every run must pass,
    as on a head. A replay that ran each check once, or passed it on any
    passing run, would let the tier pass."""
    checks = {"comp-b": [_check("twice", ["/bin/sh", "twice.sh"], "passes")]}
    plan = _plan(tmp_path, "unused", checks)
    write_executable(plan / "twice.sh", TWICE)

    run = _run(tmp_path, plan)

    assert run.calls == 2, run.out
    assert "contract tests FAILED" in run.out, run.out
    assert (
        "breaker 'comp-b' (retries exhausted): acceptance:twice (comp-b): fail, exits [0, 1, 0]"
        in run.out
    ), run.out
    assert run.code != 0, run.out


@runs_a_stack
def test_a_later_tier_replays_the_checks_of_the_tiers_merged_before_it(tmp_path: Path) -> None:
    """comp-b depends on comp-a, so tier 0 holds comp-a and tier 1 comp-b.
    comp-a's check passes in tier 0 and fails in tier 1, where comp-b's
    marker meets comp-a's on the merged tree. Tier 1 replays the checks of
    the prior tier's components too, so it fails and comp-b is the breaker."""
    root = _repo(tmp_path, _stack({"tests": "true"}), comps=COMPS)
    manifest_path = root / "scripts" / "kstrl" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"][1]["dependencies"] = ["comp-a"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    git_in(root, "commit", "-q", "-am", "comp-b depends on comp-a")
    checks = {"comp-a": [_check("markers-apart", APART, "passes")]}
    plan = _plan(tmp_path, "both markers meet", checks)

    run = _factory(tmp_path, root, "--acceptance", str(plan), contract="tier", engineer=MARKER)

    assert run.calls == 2, run.out
    assert "Tier 0: contract tests passed" in run.out, run.out
    assert "Tier 1: contract tests FAILED" in run.out, run.out
    assert "breaker 'comp-b' (retries exhausted): both markers meet" in run.out, run.out
    assert run.code != 0, run.out


@runs_a_stack
def test_the_integrated_check_replays_every_merged_components_checks(tmp_path: Path) -> None:
    """In create_prs mode every component is already merged into main, so
    Phase 3 is the integrated check of main's head. It replays the checks
    of every merged component there: comp-b's check, which fails once
    src/api.py exists, fails the run with no breaker to blame."""
    root = tmp_path / "repo"
    h.merged_feature(root)
    plan = tmp_path / "plan"
    plan.mkdir()
    write_executable(
        plan / "check.sh",
        "#!/bin/sh\n"
        '[ -f "$KSTRL_TREE/src/api.py" ] && { echo "api meets store"; exit 1; }\n'
        "exit 0\n",
    )
    document = {"components": {"comp-b": {"checks": [_check("no-api", APART, "fails")]}}}
    (plan / "plan.json").write_text(json.dumps(document), encoding="utf-8")
    reviewer = h.FakeReviewer("{}")

    result, out = h.run_factory_over(
        root, reviewer, acceptance_dir=str(plan), integration_review=False
    )

    assert result.exit_code == 1, out
    assert result.contract_failures == [
        "tier 0: contract tests failed, no blame attributed "
        "(components: comp-a, comp-b): api meets store"
    ], out
