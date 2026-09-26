"""A component starts from the PRD of the manifest being run (#568).

#545 put each decomposed component's starting PRD under ``.kstrl/plan/``
and ``pre_run_prd_path`` preferred that copy whenever one existed. The file
carried no identity, so a hand-built manifest reusing a decomposed id was
seeded and judged against the other decompose's stories, and a planned copy
deleted before the run fell back to the root copy with no message.

Now the manifest records a ``planId`` on every component a plan wrote, the
planned copy lives under that id, and ``pre_run_prd_path`` reads the planned
copy only for a component that names one. The end-to-end tests drive the
real ``ks decompose`` and ``ks factory`` with a stub agent; the census below
keeps every read of the plan directory going through ``pre_run_prd_path``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.manifest import Component, Manifest
from kstrl.statedir import plan_prd_path, pre_run_prd_path
from tests.helpers.astwalk import (
    assert_census,
    blind_spot,
    folds_containing,
    folds_to,
    label,
    package_sources,
    parse,
    parsed,
    scope_of,
    spells,
)
from tests.test_root_checkout_merge import (
    BRANCH,
    COMP,
    _git,
    _initialised_project,
    _run,
    _stub_agent,
)

FEATURE_PRD = f"scripts/kstrl/feature/{COMP}/prd.json"
MANIFEST = Path("scripts") / "kstrl" / "manifest.json"


def _hand_prd() -> dict[str, Any]:
    """An operator's PRD for the decomposed id, with a story id the
    architect never wrote."""
    return {
        "branchName": BRANCH,
        "allowedPaths": ["src/", f"scripts/kstrl/feature/{COMP}/"],
        "userStories": [
            {
                "id": "US-HAND",
                "title": "Hand written",
                "acceptanceCriteria": ["prints hand"],
                "priority": 1,
                "passes": False,
                "notes": "",
            }
        ],
    }


def _decompose(root: Path, agent: Path) -> Manifest:
    run = _run(
        root,
        "decompose",
        "--root",
        str(root),
        "--spec",
        str(root / "spec.md"),
        "--project-name",
        "demo",
        "--agent-cmd",
        str(agent),
        "--ui",
        "plain",
        "--no-tui",
    )
    assert run.returncode == 0, run.stdout
    return Manifest.load(root / MANIFEST)


def _factory(root: Path, agent: Path, manifest: Path) -> tuple[int, str]:
    run = _run(
        root,
        "factory",
        "--root",
        str(root),
        "--manifest",
        str(manifest),
        "--no-prs",
        "--agent-cmd",
        str(agent),
        "--review-mode",
        "skip",
        "--no-verify",
        "--contract-check",
        "skip",
        "--max-parallel",
        "1",
        "--max-retries",
        "0",
        "--ui",
        "plain",
        "--no-tui",
        "-y",
    )
    return run.returncode, run.stdout


def _write_hand_prd(root: Path) -> None:
    (root / FEATURE_PRD).parent.mkdir(parents=True, exist_ok=True)
    (root / FEATURE_PRD).write_text(json.dumps(_hand_prd(), indent=2), encoding="utf-8")


def test_a_hand_built_manifest_reusing_a_decomposed_id_starts_from_its_own_prd(
    tmp_path: Path,
) -> None:
    root = _initialised_project(tmp_path)
    agent = _stub_agent(tmp_path)
    _decompose(root, agent)
    _write_hand_prd(root)
    hand = root / "hand.json"
    Manifest(
        version="1",
        spec_file="",
        project_name="hand",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=COMP,
                title="Hand",
                description="hand",
                dependencies=[],
                prd_path=FEATURE_PRD,
                branch_name=BRANCH,
            )
        ],
    ).save(hand)

    code, out = _factory(root, agent, hand)

    assert code == 0, out
    shown = _git(root, "show", f"{BRANCH}:{FEATURE_PRD}")
    assert shown.returncode == 0, shown.stderr
    assert [s["id"] for s in json.loads(shown.stdout)["userStories"]] == ["US-HAND"]


def test_a_missing_planned_copy_refuses_the_run_before_it_spends(tmp_path: Path) -> None:
    """A copy at ``prdPath`` in the root checkout does not stand in for it."""
    root = _initialised_project(tmp_path)
    agent = _stub_agent(tmp_path)
    manifest = _decompose(root, agent)
    for planned in (root / ".kstrl" / "plan").rglob("prd.json"):
        planned.unlink()
    _write_hand_prd(root)

    code, out = _factory(root, agent, root / MANIFEST)

    assert code == 2, out
    assert "Refusing to run: components cannot pass the scope check" in out
    (comp,) = manifest.components
    # The refusal names the file it read, relative to the root, and not
    # prdPath: the operator restores what the message names.
    assert f"pre-run PRD not found (.kstrl/plan/{comp.plan_id}/{COMP}/prd.json)" in out
    assert _git(root, "rev-parse", "--verify", BRANCH).returncode != 0, out


def test_a_later_decompose_writes_beside_an_earlier_one(tmp_path: Path) -> None:
    root = _initialised_project(tmp_path)
    agent = _stub_agent(tmp_path)
    first = _decompose(root, agent)
    second = _decompose(root, agent)

    (one,) = first.components
    (two,) = second.components
    assert one.plan_id and two.plan_id and one.plan_id != two.plan_id
    for comp in (one, two):
        assert pre_run_prd_path(root, comp.id, comp.prd_path, plan_id=comp.plan_id).is_file()


class TestTheOneReader:
    def test_no_plan_id_reads_prd_path_even_beside_a_planned_copy(self, tmp_path: Path) -> None:
        planned = plan_prd_path(tmp_path, "comp", plan_id="plan-1")
        planned.parent.mkdir(parents=True)
        planned.write_text("{}", encoding="utf-8")
        assert pre_run_prd_path(tmp_path, "comp", "x/prd.json", plan_id="") == (
            tmp_path / "x" / "prd.json"
        )

    def test_a_plan_id_never_falls_back_to_prd_path(self, tmp_path: Path) -> None:
        (tmp_path / "x").mkdir()
        (tmp_path / "x" / "prd.json").write_text("{}", encoding="utf-8")
        assert pre_run_prd_path(tmp_path, "comp", "x/prd.json", plan_id="plan-1") == (
            plan_prd_path(tmp_path, "comp", plan_id="plan-1")
        )

    def test_an_empty_plan_id_names_no_planned_copy(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no plan id"):
            plan_prd_path(tmp_path, "comp", plan_id="")

    def test_ks_run_has_no_plan(self, tmp_path: Path) -> None:
        manifest = Manifest.from_prd(Path("scripts/kstrl/prd.json"), "kstrl/x")
        assert [c.plan_id for c in manifest.components] == [""]


class TestTheManifestCarriesThePlan:
    def _raw(self, **extra: Any) -> dict[str, Any]:
        comp = {
            "id": "comp",
            "title": "t",
            "description": "d",
            "dependencies": [],
            "prdPath": "scripts/kstrl/feature/comp/prd.json",
            "branchName": "kstrl/factory/comp",
            **extra,
        }
        return {
            "version": "1",
            "specFile": "spec.md",
            "projectName": "p",
            "baseBranch": "main",
            "singlePr": False,
            "components": [comp],
        }

    def test_plan_id_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps(self._raw(planId="plan-1")), encoding="utf-8")
        loaded = Manifest.load(path)
        assert loaded.components[0].plan_id == "plan-1"
        loaded.save(path)
        assert json.loads(path.read_text(encoding="utf-8"))["components"][0]["planId"] == "plan-1"

    def test_an_absent_plan_id_is_no_plan(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps(self._raw()), encoding="utf-8")
        assert Manifest.load(path).components[0].plan_id == ""

    @pytest.mark.parametrize("value", ["../x", "a/b", "Plan", 5])
    def test_a_plan_id_that_is_not_one_path_segment_is_refused(self, value: object) -> None:
        errors = Manifest.validate_schema(self._raw(planId=value))
        assert any(e.startswith("components[0].planId:") for e in errors), errors


# --- the census: one reader of the plan directory ---------------------------


def _where(source: Path, node: ast.AST) -> str:
    return f"{label(source)}::{scope_of(parsed(source)).get(id(node), '?')}"


#: Every node in ``kstrl/`` spelling ``plan_prd_path``: its definition,
#: the two writers' imports, the two writers, and the one reader,
#: ``pre_run_prd_path``. A new row is a new way to reach a planned copy
#: that does not ask the manifest for the plan id.
EXPECTED_PLAN_PRD_PATH_SPELLINGS = {
    "decompose.py::<module>": 1,
    "decompose.py::_decompose_spec_impl": 1,
    "decompose.py::_generate_component_prd": 1,
    "integration_fix.py::<module>": 1,
    "integration_fix.py::write_fix_prd": 1,
    "statedir.py::<module>": 1,
    "statedir.py::pre_run_prd_path": 1,
}

#: Every expression in ``kstrl/`` folding to the directory name ``plan``:
#: ``STATE_SUBDIRS``, ``STATE_NOT_CARVED``, the join in ``plan_prd_path``,
#: and three TUI titles that are not paths.
EXPECTED_PLAN_LITERALS = {
    "statedir.py::<module>": 2,
    "statedir.py::plan_prd_path": 1,
    "tui/screens/decompose.py::plan_title": 1,
    "tui/screens/decompose.py::DecomposeScreen.compose": 1,
    "tui/screens/init_wizard.py::InitWizardScreen.compose": 1,
}


def test_only_pre_run_prd_path_and_the_writers_name_the_planned_copy() -> None:
    assert_census(
        sources=package_sources(),
        sees=spells("plan_prd_path"),
        expected=EXPECTED_PLAN_PRD_PATH_SPELLINGS,
        control="from kstrl.statedir import plan_prd_path\nplan_prd_path(r, c, plan_id=p)\n",
        message=(
            "a new module or function names plan_prd_path. Read the planned copy "
            "through statedir.pre_run_prd_path, which takes the component's plan id."
        ),
        key=_where,
    )


def test_nothing_else_joins_the_plan_directory_by_hand() -> None:
    assert_census(
        sources=package_sources(),
        sees=folds_to("plan"),
        expected=EXPECTED_PLAN_LITERALS,
        control='state_dir(root) / "plan" / cid / "prd.json"\n',
        message=(
            "a new expression spells the plan directory. Read the planned copy "
            "through statedir.pre_run_prd_path."
        ),
        key=_where,
    )


def test_nothing_spells_a_path_under_the_plan_directory() -> None:
    assert_census(
        sources=package_sources(),
        sees=folds_containing(".kstrl/plan"),
        expected={},
        control='root / ".kstrl/plan/p/c/prd.json"\n',
        message=(
            "a new expression spells a path under .kstrl/plan. Read the planned "
            "copy through statedir.pre_run_prd_path."
        ),
    )


def _joins_plan(source: str) -> bool:
    return any(folds_to("plan")(node) for node in ast.walk(parse(source)))


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_directory_name_built_at_run_time_is_not_seen() -> None:
    """Disclosed: a name the walk cannot fold, such as a ``join`` of two
    halves, is not counted by either census above."""
    blind_spot(_joins_plan, 'state_dir(root) / "".join(("pl", "an")) / cid\n')
