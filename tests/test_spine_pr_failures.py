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

import json
import os
from pathlib import Path
from typing import Any

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
    init_kstrl_repo,
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
    for var in (
        "GH_SPINE_CREATE",
        "GH_SPINE_MERGE",
        "GH_SPINE_VIEW_STATE",
        "GH_SPINE_MERGE_SHA",
    ):
        monkeypatch.delenv(var, raising=False)
    return bin_dir


def _alpha_beta_manifest() -> Manifest:
    return make_manifest([component("alpha"), component("beta", ["alpha"])])


#: A 40-character sha the stub publishes as the merge commit. Not a real
#: commit in any test repo: the point is that the factory records what
#: GitHub said, never something it derived from the local tree.
STUB_MERGE_SHA = "a1b2c3d4" * 5

#: The inert [release] section, so the run reaches the run-state rungs of
#: the gate. Nothing else is set, so every other section stays default -
#: in particular [policy] stays disabled, which is what keeps this file's
#: runs free of Phase 1 policy enforcement.
RELEASE_TOML = '[release]\nenabled = true\nenvironment = "staging"\n'


def _enable_release(root: Path) -> None:
    """Write and COMMIT the inert [release] section."""
    (root / "kstrl.toml").write_text(RELEASE_TOML, encoding="utf-8")
    git("add", "kstrl.toml", cwd=root)
    git("commit", "-m", "enable the inert release section", cwd=root)


def _release_row(root: Path) -> dict[str, Any]:
    """The single factory_completed row this run wrote."""
    runs = sorted((root / ".kstrl" / "runs").iterdir())
    assert runs, "no run dir written"
    rows = [
        json.loads(line)
        for line in (runs[-1] / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    completed = [r for r in rows if r["event"] == "factory_completed"]
    assert len(completed) == 1, f"expected one factory_completed row, got {len(completed)}"
    data = completed[0]["data"]
    assert isinstance(data, dict)
    return data


def _run_real(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Manifest,
) -> tuple[FactoryResult, list[str]]:
    """run_factory with the real logging engineer; returns the result and
    the component ids whose engineer subprocess actually ran."""
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
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
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy-path baseline for the harness itself: both engineers
        run, both branches are REALLY pushed to the bare origin, both
        components complete."""
        root = tmp_path / "repo"
        origin = init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
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
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
        # Break the push target AFTER the initial push so the
        # origin/main tracking ref exists but every push fails.
        git("remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=root)
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
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_CREATE", "fail")
        root = tmp_path / "repo"
        origin = init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
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
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_MERGE", "fail")
        # A REAL merge failure means the PR is not merged; without this
        # pin the stub's MERGED default would trigger _merge_and_wait's
        # (deliberate) merged-outcome rescue and complete the component.
        monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
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
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
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
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Crash-recovery semantics: a MERGE_PENDING component whose PR
        has since merged is re-polled to COMPLETED on the next run; its
        engineer does not re-run, and dependents then schedule."""
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
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


class TestSpineReleaseRef:
    """R8.7 slice 1 (#154): the release ref, recorded end to end."""

    def test_a_merged_run_records_the_commit_gh_published(
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_MERGE_SHA", STUB_MERGE_SHA)
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()
        _enable_release(root)

        _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.merge_sha == STUB_MERGE_SHA
        assert beta.merge_sha == STUB_MERGE_SHA
        row = _release_row(root)
        assert row["release_ref"] == STUB_MERGE_SHA
        assert row["release_ref_rule"] == "last_merge_by_completed_at"
        assert row["release_withheld"] == "policy_disabled"
        # The in-memory object is the one the run mutated; re-read the
        # saved manifest too, so this proves the field reached disk and
        # not only the object the test already holds a reference to.
        on_disk = json.loads(
            (root / "scripts" / "kstrl" / "manifest.json").read_text(encoding="utf-8")
        )
        disk_shas = {c["id"]: c["mergeSha"] for c in on_disk["components"]}
        assert disk_shas == {"alpha": STUB_MERGE_SHA, "beta": STUB_MERGE_SHA}

    def test_a_merge_with_no_published_commit_is_still_merged_and_records_nothing(
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()
        _enable_release(root)

        result, _ = _run_real(root, tmp_path, monkeypatch, manifest)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.COMPLETED.value
        assert alpha.merge_sha == ""
        assert beta.merge_sha == ""
        assert result.exit_code == 0
        row = _release_row(root)
        assert row["release_ref"] == ""
        assert row["release_withheld"] == "release_ref_unrecorded"

    def test_a_repolled_merge_pending_component_records_the_ref(
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_MERGE_SHA", STUB_MERGE_SHA)
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()
        alpha = manifest.get_component("alpha")
        assert alpha is not None
        alpha.status = ComponentStatus.MERGE_PENDING.value
        alpha.pr_number = STUB_PR_NUMBER
        alpha.pr_url = STUB_PR_URL
        _enable_release(root)
        # Stub default view state is MERGED: the PR landed while the
        # factory was down.

        _, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        beta = manifest.get_component("beta")
        assert beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert alpha.merge_sha == STUB_MERGE_SHA
        assert ran == ["beta"]  # alpha was re-polled, never re-run
        row = _release_row(root)
        assert row["release_ref"] == STUB_MERGE_SHA

    def test_a_merge_pending_run_is_not_clean(
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()
        _enable_release(root)

        result, _ = _run_real(root, tmp_path, monkeypatch, manifest)

        assert result.merge_pending == ["alpha"]
        row = _release_row(root)
        assert row["release_withheld"] == "run_not_clean"

    def test_an_idempotent_rerun_does_not_replay_a_previous_runs_merge(
        self,
        tmp_path: Path,
        stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#154 fix round, A1b: ``merge_sha`` persists on the manifest,
        so a second run that merges nothing must not report the first
        run's merge as its own release ref. Both components are already
        COMPLETED with an old merge_sha/completed_at (as a real earlier
        run would leave them); the scheduler launches neither, so
        nothing this run does could produce a fresh merge."""
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"), with_origin=True)
        manifest = _alpha_beta_manifest()
        for comp_id in ("alpha", "beta"):
            comp = manifest.get_component(comp_id)
            assert comp is not None
            comp.status = ComponentStatus.COMPLETED.value
            comp.merge_sha = STUB_MERGE_SHA
            comp.completed_at = "2020-01-01T00:00:00Z"
        _enable_release(root)

        result, ran = _run_real(root, tmp_path, monkeypatch, manifest)

        assert ran == []  # nothing scheduled: both already COMPLETED
        assert result.exit_code == 0
        row = _release_row(root)
        assert row["release_ref"] == ""
        assert row["release_withheld"] == "release_ref_unrecorded"
