"""End to end: how a fix component is created, made visible, and refused (#483).

The three creation stages and their reconcile on resume, and the checks that
must refuse a fix before its worker launches. Every test drives the real
``run_factory`` through ``tests/helpers/integration_loop``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig
from kstrl.integration_fix import fix_prd_rel
from kstrl.integration_state import state_payload_errors
from kstrl.manifest import Component, Manifest
from kstrl.scope import RunScope
from kstrl.statedir import plan_prd_path
from tests.helpers import integration_harness as h
from tests.helpers import integration_loop as lp
from tests.helpers.gitrepo import git_in


class _Crash(BaseException):
    """A process death between two creation stages: nothing may catch it."""


def _crash(*_args: Any, **_kwargs: Any) -> None:
    raise _Crash


@pytest.mark.parametrize(
    ("stage", "on_disk"),
    [
        ("kstrl.integration_loop.write_fix_prd", "state entry yes, PRD no, manifest component no"),
        (
            "kstrl.integration_loop.append_fix_component",
            "state entry yes, PRD yes, manifest component no",
        ),
    ],
)
def test_a_crash_between_stages_is_refused_on_resume(
    tmp_path: Path, stage: str, on_disk: str
) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    with patch(stage, side_effect=_crash), pytest.raises(_Crash):
        lp.run_loop(root, lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL])))

    assert [f["id"] for f in lp.state(root)["fixes"]] == [lp.FIX_1]
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]

    reviewer = lp.ScriptedReviewer(base, [{}])
    rig = lp.Rig(root, reviewer)
    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    assert reviewer.calls == 0
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "not_run"
    assert stop["reason"] == f"integration fix {lp.FIX_1} is incomplete: {on_disk}"
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1


def test_a_crash_after_the_manifest_append_is_reconciled_on_resume(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    with patch("kstrl.integration_loop._make_visible", side_effect=_crash), pytest.raises(_Crash):
        lp.run_loop(root, lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL])))

    assert lp.manifest_ids(root) == ["comp-a", "comp-b", lp.FIX_1]
    assert plan_prd_path(root, lp.FIX_1).is_file()

    reviewer = lp.ScriptedReviewer(base, [{}])
    rig = lp.Rig(root, reviewer)
    result, _out = lp.run_loop(root, rig)

    assert rig.launched == [lp.FIX_1]
    assert rig.scopes[lp.FIX_1].allowed_paths == lp.state(root)["fixes"][0]["scope"]
    assert lp.state(root)["findings"][0]["status"] == "closed"
    assert result.exit_code == 0


def test_the_fix_prd_is_not_written_where_the_fix_branch_commits_it(tmp_path: Path) -> None:
    """#545: the fix's branch commits its PRD at ``fix_prd_rel``, so a copy
    at that path in the root checkout blocks ``git merge`` of the branch.
    Stopped after the three creation stages, before the fix is launched,
    so nothing but the integration loop has written anything."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    with patch("kstrl.integration_loop._make_visible", side_effect=_crash), pytest.raises(_Crash):
        lp.run_loop(root, lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL])))

    assert plan_prd_path(root, lp.FIX_1).is_file()
    assert not (root / fix_prd_rel(lp.FIX_1)).exists()


def test_a_dag_error_in_the_extended_manifest_refuses_the_fix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    real = Manifest.validate_dag

    def planted(self: Manifest) -> list[str]:
        extended = any(c.id.startswith("integration-fix-") for c in self.components)
        return [*real(self), *(["planted DAG error"] if extended else [])]

    rig = lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL]))
    with patch.object(Manifest, "validate_dag", planted):
        result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert "fails the DAG check: planted DAG error" in stop["reason"]
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1


def test_a_skipped_snapshot_extension_is_refused_before_the_worker_launches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)

    def skipped(self: RunScope, comp: Component, root_dir: Path, cfg: KstrlConfig) -> RunScope:
        return self

    rig = lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL]))
    with patch.object(RunScope, "with_component", skipped):
        result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert "no plan-time scope was resolved" in stop["reason"]
    assert result.exit_code == 1


def test_a_branch_collision_refuses_the_fix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    git_in(root, "branch", f"kstrl/factory/{lp.FIX_1}", base)
    git_in(root, "checkout", "-q", f"kstrl/factory/{lp.FIX_1}")
    h.commit_file(root, "stale.txt", "left by an earlier run\n")
    git_in(root, "checkout", "-q", "main")
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL]))

    result, _out = lp.run_loop(root, rig, use_worktrees=True)

    assert rig.launched == []
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert "already exists with commits not merged" in stop["reason"]
    assert result.exit_code == 1


def test_run_scope_with_component_resolves_only_the_new_id(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lp.loop_feature(root)
    manifest = Manifest.load(h.manifest_file(root))
    cfg = h.kstrl_config(root)
    first = RunScope.resolve(manifest, root, cfg)
    fix = Component(lp.FIX_1, "t", "d", [], "scripts/kstrl/feature/comp-a/prd.json", "b")

    extended = first.with_component(fix, root, cfg)

    assert extended.by_component["comp-a"] is first.by_component["comp-a"]
    assert extended.for_component(lp.FIX_1).allowed_paths == lp.SCOPES["comp-a"]
    with pytest.raises(ValueError, match="already has a plan-time scope"):
        extended.with_component(fix, root, cfg)


def test_the_state_accepts_a_slice_2_file_and_refuses_a_malformed_fix() -> None:
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "manifestPath": "m",
        "project": "p",
        "specFile": "s",
        "featureBaseSha": "b",
        "lastReviewedSha": "",
        "findings": [],
        "stops": [],
    }
    assert state_payload_errors(base) == []
    assert state_payload_errors({**base, "fixes": [{"id": "x"}]}) == [
        "fixes[0].prdPath: must be a string, got NoneType",
        "fixes[0].findings must be a list of strings",
        "fixes[0].scope must be a list of strings",
    ]


@pytest.mark.parametrize(
    ("load", "expected"),
    [
        (lambda root: FactoryConfig.load(root), (False, 1)),
        (
            lambda root: _toml(root, "integration_blocking = true\nintegration_max_rounds = 3\n"),
            (True, 3),
        ),
    ],
)
def test_the_two_keys_load(
    tmp_path: Path, load: Callable[[Path], FactoryConfig], expected: tuple[bool, int]
) -> None:
    config = load(tmp_path)
    assert (config.integration_blocking, config.integration_max_rounds) == expected


def _toml(root: Path, body: str) -> FactoryConfig:
    (root / "kstrl.toml").write_text(f"[factory]\n{body}", encoding="utf-8")
    return FactoryConfig.load(root)


def test_the_env_overrides_the_two_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSTRL_FACTORY_INTEGRATION_BLOCKING", "1")
    monkeypatch.setenv("KSTRL_FACTORY_INTEGRATION_MAX_ROUNDS", "2")
    assert (
        FactoryConfig.load(tmp_path).integration_blocking,
        FactoryConfig.load(tmp_path).integration_max_rounds,
    ) == (True, 2)
    assert FactoryConfig.from_env().integration_max_rounds == 2
    monkeypatch.setenv("KSTRL_FACTORY_INTEGRATION_MAX_ROUNDS", "two")
    with pytest.raises(ValueError, match="KSTRL_FACTORY_INTEGRATION_MAX_ROUNDS must be a whole"):
        FactoryConfig.load(tmp_path)


@pytest.mark.parametrize("value", ["0", "-1", "true", '"2"'])
def test_a_max_rounds_below_one_or_not_a_number_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(
        ValueError, match="integration_max_rounds must be a whole number of at least 1"
    ):
        _toml(tmp_path, f"integration_max_rounds = {value}\n")
