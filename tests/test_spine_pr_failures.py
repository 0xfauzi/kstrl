"""Spine tier I (R4.2): PR failure paths, fully unmocked.

tests/test_pr_outcomes.py already proves the wave-2 status semantics but
patches ``_run_component`` (the engineer) with a mock. These spine tests
close that last mocked boundary: the engineer is a real ``bash -lc``
subprocess running in a real worktree, pushes go to a real bare origin,
and only ``gh`` is a stub executable on PATH (tests/spine_utils.py,
driven by GH_SPINE_* env vars). Which components actually ran is proven
by the engineer's own side effect (it logs its worktree cwd), not by a
mock's call list.

Wave-2 semantics asserted per failure shape:
- push-fail / pr-create-fail / merge-fail: component FAILED, dependents
  cascade-SKIPPED and never run.
- wait-timeout: component MERGE_PENDING (re-pollable, not failed),
  dependents stay PENDING and never run.
- resume: a MERGE_PENDING component whose PR merged is re-polled to
  COMPLETED without re-running its engineer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kstrl.factory import FactoryResult, run_factory
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from tests.spine_utils import (
    STUB_PR_NUMBER,
    STUB_PR_URL,
    base_config,
    component,
    factory_config,
    git,
    init_ralph_repo,
    logging_engineer,
    make_manifest,
    ran_components,
    write_stub_gh,
)

pytestmark = pytest.mark.spine


@pytest.fixture
def stub_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "spine-bin"
    bin_dir.mkdir()
    write_stub_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for var in ("GH_SPINE_CREATE", "GH_SPINE_MERGE", "GH_SPINE_VIEW_STATE"):
        monkeypatch.delenv(var, raising=False)
    return bin_dir


def _alpha_beta_manifest() -> Manifest:
    return make_manifest([component("alpha"), component("beta", ["alpha"])])


def _run_real(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Manifest,
) -> tuple[FactoryResult, list[str]]:
    """run_factory with the real logging engineer; returns the result and
    the component ids whose engineer subprocess actually ran."""
    monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
    agent_log = tmp_path / "agent-calls.log"
    result = run_factory(
        manifest,
        factory_config(create_prs=True),
        base_config(root, logging_engineer(agent_log)),
        PlainUI(no_color=True),
        root,
    )
    return result, ran_components(agent_log)


class TestSpinePrFailurePaths:
    def test_merged_pr_completes_and_schedules_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy-path baseline for the harness itself: both engineers
        run, both branches are REALLY pushed to the bare origin, both
        components complete."""
        root = tmp_path / "repo"
        origin = init_ralph_repo(root, ("alpha", "beta"), with_origin=True)
        assert origin is not None
        manifest = _alpha_beta_manifest()

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.COMPLETED.value
        assert ran == ["alpha", "beta"]
        assert result.completed == ["alpha", "beta"]
        assert result.exit_code == 0
        assert len(result.pr_urls) == 2
        # The pushes were real - the PR lifecycle cannot complete
        # without them - and the post-merge remote cleanup then removed
        # both branch refs from origin (the --delete-branch replacement;
        # a stale remote branch would break the next same-name push).
        for branch in ("kstrl/factory/alpha", "kstrl/factory/beta"):
            refs = git("ls-remote", "--heads", "origin", branch, cwd=root)
            assert refs == "", branch + " not cleaned from origin: " + refs

    def test_push_failure_fails_component_and_skips_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        init_ralph_repo(root, ("alpha", "beta"), with_origin=True)
        # Break the push target AFTER the initial push so the
        # origin/main tracking ref exists but every push fails.
        git("remote", "set-url", "origin", str(tmp_path / "missing.git"),
            cwd=root)
        manifest = _alpha_beta_manifest()

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "push" in alpha.error
        assert beta.status == ComponentStatus.SKIPPED.value
        assert ran == ["alpha"]  # beta's engineer never started
        assert result.failed == ["alpha"]
        assert "beta" in result.skipped
        assert result.exit_code == 1

    def test_pr_create_failure_fails_component_and_skips_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_CREATE", "fail")
        root = tmp_path / "repo"
        origin = init_ralph_repo(root, ("alpha", "beta"), with_origin=True)
        assert origin is not None
        manifest = _alpha_beta_manifest()

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "Failed to create PR" in alpha.error
        assert beta.status == ComponentStatus.SKIPPED.value
        assert ran == ["alpha"]
        assert result.failed == ["alpha"]
        assert "beta" in result.skipped
        assert result.exit_code == 1
        # The failure was after the real push: origin has alpha's branch.
        assert git("rev-parse", "refs/heads/kstrl/factory/alpha", cwd=origin)

    def test_merge_failure_fails_component_and_skips_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_MERGE", "fail")
        # A REAL merge failure means the PR is not merged; without this
        # pin the stub's MERGED default would trigger _merge_and_wait's
        # (deliberate) merged-outcome rescue and complete the component.
        monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
        root = tmp_path / "repo"
        init_ralph_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "merge failed" in alpha.error
        # The PR was created before the merge failed: recorded for audit.
        assert alpha.pr_url == STUB_PR_URL
        assert beta.status == ComponentStatus.SKIPPED.value
        assert ran == ["alpha"]
        assert result.exit_code == 1

    def test_wait_timeout_marks_merge_pending_dependents_stay_pending(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
        root = tmp_path / "repo"
        init_ralph_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.MERGE_PENDING.value
        assert alpha.pr_url == STUB_PR_URL  # recorded for the re-poll
        assert "not merged within" in alpha.error
        # Not a failure: beta stays PENDING (never SKIPPED) and its
        # engineer never runs past an unconfirmed merge (CRIT-2).
        assert beta.status == ComponentStatus.PENDING.value
        assert ran == ["alpha"]
        assert result.merge_pending == ["alpha"]
        assert "alpha" not in result.completed
        assert "alpha" not in result.failed
        assert result.exit_code == 1

    def test_merge_pending_resume_repolls_without_rerunning_engineer(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Crash-recovery semantics: a MERGE_PENDING component whose PR
        has since merged is re-polled to COMPLETED on the next run; its
        engineer does not re-run, and dependents then schedule."""
        root = tmp_path / "repo"
        init_ralph_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()
        alpha = manifest.get_component("alpha")
        assert alpha is not None
        alpha.status = ComponentStatus.MERGE_PENDING.value
        alpha.pr_number = STUB_PR_NUMBER
        alpha.pr_url = STUB_PR_URL
        # Stub default view state is MERGED: the PR landed while the
        # factory was down.

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        beta = manifest.get_component("beta")
        assert beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.COMPLETED.value
        assert ran == ["beta"]  # alpha was re-polled, never re-run
        assert "alpha" in result.completed
        assert result.merge_pending == []
        assert result.exit_code == 0
