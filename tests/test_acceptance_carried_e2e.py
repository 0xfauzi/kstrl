"""#466: the acceptance checks of features that merged earlier are replayed.

A run whose components merged into the base (one PR per component) and
whose Phase 3 passed there keeps their acceptance checks under the control
directory. Every later run replays them once on its base before anything
is spent, and again on the commit its Phase 3 tests, each ``HEAD_RUNS``
times. One that fails in Phase 3 fails the run as any acceptance check
does. One that does not pass on the base refuses the run and files an
inbox item; approving the item retires the check, recorded with who
approved it.

End to end: the real ``run_factory`` over a real git repository whose
``main`` holds each feature's merged components
(``tests/helpers/integration_harness.py``), the real base replay, Phase 3
and nono rung, the real ``ks inbox approve`` as a subprocess, and the real
``ks factory --no-prs`` for the run that merges nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.decompose import spec_digest
from kstrl.factory import FactoryResult
from kstrl.factory import _resolve_round_base as real_resolve_round_base
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.statedir import control_dir
from kstrl.ui.base import UI
from tests.helpers import integration_harness as h
from tests.helpers.component_prd import PASSING_STORY, write_component_prd
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import _factory, _repo, _spawn, _stack

#: Feature 1's behaviour: src/api.py defines handle, and src/store.py save.
HANDLE = 'grep -q "def handle" "$KSTRL_TREE/src/api.py" || { echo "handle is gone"; exit 1; }'
SAVE = 'grep -q "def save" "$KSTRL_TREE/src/store.py" || { echo "save is gone"; exit 1; }'

#: Feature 2's merged change: it rewrites src/api.py without handle.
BROKEN_API = "def other() -> int:\n    return 0\n"


def _plan(where: Path, checks: dict[str, list[tuple[str, str]]], *, held_out: bool = False) -> Path:
    """A plan outside the repository: each check runs ``/bin/sh <id>.sh``."""
    where.mkdir()
    document: dict[str, Any] = {"components": {}}
    for comp, rows in checks.items():
        entries = []
        for check_id, body in rows:
            write_executable(where / f"{check_id}.sh", f"#!/bin/sh\n{body}\n")
            entries.append(
                {
                    "id": check_id,
                    "criterion": f"{check_id} holds",
                    "argv": ["/bin/sh", f"{check_id}.sh"],
                    "onBase": "passes",
                    "heldOut": held_out,
                }
            )
        document["components"][comp] = {"checks": entries}
    (where / "plan.json").write_text(json.dumps(document), encoding="utf-8")
    return where


def _carried(root: Path) -> dict[str, Any]:
    path = control_dir(root) / "acceptance" / "carried.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _rows(root: Path, key: str) -> list[tuple[str, str]]:
    return [(row["component"], row["check"]) for row in _carried(root)[key]]


def _first_feature(tmp_path: Path, root: Path, *, held_out: bool = False) -> None:
    """Feature 1: comp-a merged on main, comp-b completed with no PR, so it
    did not merge. Its run passes Phase 3."""
    h.merged_feature(root)
    manifest = Manifest.load(h.manifest_file(root))
    unmerged = manifest.get_component("comp-b")
    assert unmerged is not None
    unmerged.pr_url, unmerged.merge_sha = "", ""
    manifest.save(h.manifest_file(root))
    plan = _plan(
        tmp_path / "plan1",
        {
            "comp-a": [("keeps-handle", HANDLE), ("keeps-save", SAVE)],
            "comp-b": [("b-holds", "true")],
        },
        held_out=held_out,
    )
    result, out = h.run_factory_over(
        root, h.FakeReviewer("{}"), acceptance_dir=str(plan), integration_review=False
    )
    assert result.exit_code == 0, out


def _second_feature(tmp_path: Path, root: Path, *, merged: bool = True) -> None:
    """Feature 2's manifest: comp-c, completed and merged on main, and its plan.
    With ``merged`` False, comp-c is completed on its branch with no PR, as a
    run that merges after Phase 3 leaves it."""
    text = "# Spec 2\n\nA second feature.\n"
    (root / "spec2.md").write_text(text, encoding="utf-8")
    write_component_prd(root, "scripts/kstrl/feature/comp-c/prd.json", stories=[PASSING_STORY])
    comp = Component(
        "comp-c",
        "Comp-C",
        "Desc",
        [],
        "scripts/kstrl/feature/comp-c/prd.json",
        "kstrl/factory/comp-c",
    )
    comp.status = ComponentStatus.COMPLETED.value
    comp.pr_url = "https://github.com/o/r/pull/3" if merged else ""
    comp.merge_sha = h.rev(root) if merged else ""
    manifest = Manifest(
        version="1",
        spec_file="spec2.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[comp],
        spec_path="spec2.md",
        spec_digest=spec_digest(text),
    )
    manifest.feature_base_sha = h.rev(root)
    manifest.save(h.manifest_file(root))
    _plan(tmp_path / "plan2", {"comp-c": [("c-holds", "true")]})


def _run_second(
    tmp_path: Path, root: Path, *, merges_break: bool = False, **overrides: Any
) -> tuple[FactoryResult, str]:
    """Feature 2's run. With ``merges_break``, its change lands on main after
    the base replay and before Phase 3, as a merged component's does."""

    def resolve(manifest: Manifest, root_dir: Path, ui: UI) -> str:
        if merges_break:
            h.commit_file(root_dir, h.API, BROKEN_API)
        return real_resolve_round_base(manifest, root_dir, ui)

    with patch("kstrl.factory._resolve_round_base", side_effect=resolve):
        return h.run_factory_over(
            root,
            h.FakeReviewer("{}"),
            acceptance_dir=str(tmp_path / "plan2"),
            integration_review=False,
            **overrides,
        )


def _halts(root: Path) -> list[InboxItem]:
    items = Inbox(root, InboxConfig.load(root)).scan().folded_items()
    return [item for item in items if item.kind is ItemKind.HALTED_RUN]


@runs_a_stack
def test_a_later_feature_that_breaks_an_earlier_features_check_fails_phase_3(
    tmp_path: Path,
) -> None:
    """Feature 1's checks of its merged component are carried, and not the
    unmerged comp-b's. Feature 2's change removes handle once its component
    merges: the carried check passes on feature 2's base and fails on the
    commit its Phase 3 tests, so the run fails and says why."""
    root = tmp_path / "repo"
    _first_feature(tmp_path, root)
    assert _rows(root, "checks") == [("comp-a", "keeps-handle"), ("comp-a", "keeps-save")]
    _second_feature(tmp_path, root)

    result, out = _run_second(tmp_path, root, merges_break=True)

    assert "Replaying 2 carried acceptance checks on the base" in out, out
    assert result.exit_code == 1, out
    assert result.contract_failures == [
        "tier 0: contract tests failed, no blame attributed (components: comp-c): handle is gone"
    ], out
    (item,) = _halts(root)
    assert item.evidence["carried"]["check"] == "keeps-handle", item.evidence
    assert item.evidence["where"] == "Phase 3 tier 0", item.evidence


@runs_a_stack
def test_a_carried_held_out_check_is_named_by_its_id_alone_and_runs_every_time(
    tmp_path: Path,
) -> None:
    """A held-out check stays held out for a later feature: the failure names
    its id and where it came from, never its output, and shows each of its
    ``HEAD_RUNS`` runs."""
    root = tmp_path / "repo"
    _first_feature(tmp_path, root, held_out=True)
    plan_id = _carried(root)["checks"][0]["planId"]
    _second_feature(tmp_path, root)

    result, out = _run_second(tmp_path, root, merges_break=True)

    assert result.exit_code == 1, out
    assert result.contract_failures == [
        "tier 0: contract tests failed, no blame attributed (components: comp-c): "
        f"acceptance:keeps-handle (carried:{plan_id[:12]}/comp-a): fail, exits [1, 1, 1]"
    ], out
    assert "handle is gone" not in out, out


@runs_a_stack
def test_a_base_where_a_carried_check_fails_refuses_until_an_approval_retires_it(
    tmp_path: Path,
) -> None:
    """Feature 2 merged before its run looked: on the base, keeps-handle
    fails. The run refuses before Phase 3 and files one item naming that
    check. ks inbox approve retires it, with who approved it, and the next
    run replays the other carried check and passes; its own check is now
    carried too."""
    root = tmp_path / "repo"
    _first_feature(tmp_path, root)
    plan_id = _carried(root)["checks"][0]["planId"]
    h.commit_file(root, h.API, BROKEN_API)
    _second_feature(tmp_path, root)

    refused, out = _run_second(tmp_path, root)

    assert refused.exit_code == 2, out
    assert "Refusing to run: an earlier feature's acceptance check does not hold on the base" in out
    assert f"the carried check keeps-handle (carried:{plan_id[:12]}/comp-a)" in out, out
    assert "Integrated check" not in out, out
    (item,) = _halts(root)
    assert item.evidence["carried"] == {
        "planId": plan_id,
        "component": "comp-a",
        "check": "keeps-handle",
    }
    approve = ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain", "--no-color"]
    code, said = _spawn(approve, root, None)
    assert code == 0, said
    assert "retires the acceptance check keeps-handle of comp-a" in said, said
    (approved,) = _halts(root)

    passed, out = _run_second(tmp_path, root)

    assert passed.exit_code == 0, out
    assert "Replaying 1 carried acceptance checks on the base" in out, out
    assert _rows(root, "checks") == [("comp-a", "keeps-save"), ("comp-c", "c-holds")]
    assert [(r["check"], r["item"], r["by"]) for r in _carried(root)["retired"]] == [
        ("keeps-handle", item.id, approved.decided_by)
    ]


@runs_a_stack
def test_a_run_that_merges_nothing_carries_nothing(tmp_path: Path) -> None:
    """ks factory --no-prs merges no component into the base: its checks
    pass Phase 3 on a merged tree that is not the base, and are not carried."""
    root = _repo(tmp_path, _stack({"tests": "true"}), comps=("comp-a", "comp-b"))
    plan = _plan(tmp_path / "plan", {"comp-a": [("a-holds", "true")]})

    run = _factory(
        tmp_path,
        root,
        "--acceptance",
        str(plan),
        contract="final",
        engineer='touch "$(basename "$PWD").marker" && git add -A && git commit -q -m m',
    )

    assert run.code == 0, run.out
    assert "contract tests passed" in run.out, run.out
    assert not (control_dir(root) / "acceptance" / "carried.json").exists()


@runs_a_stack
def test_a_carried_check_a_run_that_merges_after_phase_3_breaks_can_retire(
    tmp_path: Path,
) -> None:
    """With no PR per component, feature 2's change reaches the base only
    after Phase 3, so the base replay never sees it fail. Phase 3's tier
    fails on the carried check, blames comp-c, and files the item, and
    approving it retires the check: an intended change that makes an earlier
    check obsolete has a way through. The third run stands for the retry
    that rebuilds comp-c with the same change."""
    root = tmp_path / "repo"
    _first_feature(tmp_path, root)
    plan_id = _carried(root)["checks"][0]["planId"]
    git_in(root, "checkout", "-q", "-b", "kstrl/factory/comp-c")
    h.commit_file(root, h.API, BROKEN_API)
    git_in(root, "checkout", "-q", "main")
    _second_feature(tmp_path, root, merged=False)

    failed, out = _run_second(tmp_path, root, create_prs=False)

    assert "Replaying 2 carried acceptance checks on the base" in out, out
    assert "Refusing to run" not in out, out
    assert failed.exit_code == 1, out
    assert failed.contract_failures == [
        "tier 0: breaker 'comp-c' (retries exhausted): handle is gone"
    ], out
    (item,) = _halts(root)
    assert item.evidence["carried"] == {
        "planId": plan_id,
        "component": "comp-a",
        "check": "keeps-handle",
    }
    approve = ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain", "--no-color"]
    code, said = _spawn(approve, root, None)
    assert code == 0, said
    manifest = Manifest.load(h.manifest_file(root))
    comp = manifest.get_component("comp-c")
    assert comp is not None
    comp.status, comp.error = ComponentStatus.COMPLETED.value, ""
    manifest.save(h.manifest_file(root))

    passed, out = _run_second(tmp_path, root, create_prs=False)

    assert passed.exit_code == 0, out
    assert "Replaying 1 carried acceptance checks on the base" in out, out
    assert [r["check"] for r in _carried(root)["retired"]] == ["keeps-handle"]


@runs_a_stack
def test_a_carried_file_that_cannot_be_read_refuses_the_run(tmp_path: Path) -> None:
    """An unreadable carried file is a refusal, never an empty set: an empty
    read would replay no earlier feature's check and pass."""
    root = tmp_path / "repo"
    _first_feature(tmp_path, root)
    path = control_dir(root) / "acceptance" / "carried.json"
    _second_feature(tmp_path, root)
    for body, said in (
        ("{not json", "are not JSON"),
        ('{"schemaVersion": 1, "checks": [{"planId": ""}], "retired": []}', "checks[0]"),
    ):
        path.write_text(body, encoding="utf-8")

        refused, out = _run_second(tmp_path, root)

        assert refused.exit_code == 2, out
        assert "Refusing to run: an earlier feature's acceptance check does not hold" in out
        assert said in out, out
        assert "Integrated check" not in out, out


@runs_a_stack
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes a 0o555 directory"
)
def test_a_carry_that_cannot_be_written_fails_the_run(tmp_path: Path) -> None:
    """The components merged and Phase 3 passed, but their checks could not
    be kept: a later feature could break them unseen, so the run fails."""
    root = tmp_path / "repo"
    h.merged_feature(root)
    plan = _plan(tmp_path / "plan1", {"comp-a": [("keeps-handle", HANDLE)]})
    acceptance = control_dir(root) / "acceptance"

    def resolve(manifest: Manifest, root_dir: Path, ui: UI) -> str:
        acceptance.chmod(0o555)
        return real_resolve_round_base(manifest, root_dir, ui)

    try:
        with patch("kstrl.factory._resolve_round_base", side_effect=resolve):
            result, out = h.run_factory_over(
                root, h.FakeReviewer("{}"), acceptance_dir=str(plan), integration_review=False
            )
    finally:
        acceptance.chmod(0o755)

    assert "Integrated check: contract tests passed" in out, out
    assert result.exit_code == 1, out
    (failure,) = result.contract_failures
    assert failure.startswith("the carried acceptance checks cannot be written"), failure
