"""R0.2 tests: PR/merge outcome gates completion (CRIT-2, H-1).

Real git repositories with a stub ``gh`` binary placed on PATH. Covers:

- push-fail / create-fail / merge-fail -> component FAILED, dependents
  do not schedule (cascade-skipped).
- wait_for_merge timeout -> MERGE_PENDING, dependents stay PENDING
  (re-pollable, not failed); crash recovery re-polls on the next run.
- ``git fetch origin <base>`` updates only the remote-tracking ref: the
  operator's checked-out branch, local base branch, and uncommitted
  state are never touched.
- origin/<base> resolution for worktree cuts and diffs, with a local
  fallback when no remote exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_py import git
from ralph_py.config import RalphConfig
from ralph_py.factory import (
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    run_factory,
)
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.pr import PrOutcome, push_create_and_merge_pr, wait_for_merge
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import VerifyConfig

STUB_PR_URL = "https://github.com/stub/repo/pull/7"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed: {result.stderr}"
    )
    return result.stdout.strip()


def _prd_json() -> str:
    return json.dumps({
        "branchName": "test",
        "userStories": [{
            "id": "US-001", "title": "Test",
            "acceptanceCriteria": ["AC1"],
            "priority": 1, "passes": True, "notes": "",
        }],
    })


def _make_repo(
    tmp_path: Path, comp_ids: list[str], with_origin: bool = True,
) -> Path:
    """Real git repo with committed ralph scaffolding and, optionally, a
    bare origin. The operator is parked on a side branch with an
    uncommitted file so H-1 violations (checkout mutation) are visible.
    """
    root = tmp_path / "repo"
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for cid in comp_ids:
        feature = ralph_dir / "feature" / cid
        feature.mkdir(parents=True)
        (feature / "prd.json").write_text(_prd_json())
    (root / ".gitignore").write_text(
        ".ralph/\nscripts/ralph/manifest.json\n"
    )
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "ralph-test@example.com", cwd=root)
    _git("config", "user.name", "Ralph Test", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    if with_origin:
        origin = tmp_path / "origin.git"
        _git("init", "--bare", str(origin), cwd=tmp_path)
        _git("remote", "add", "origin", str(origin), cwd=root)
        _git("push", "-u", "origin", "main", cwd=root)
    _git("checkout", "-b", "operator-branch", cwd=root)
    (root / "OPERATOR_NOTES.txt").write_text("uncommitted operator state")
    return root


def _advance_origin(tmp_path: Path, filename: str = "file1.txt") -> str:
    """Push a new commit to origin/main from a second clone, simulating a
    squash merge landing remotely. Returns the new origin/main sha."""
    clone = tmp_path / f"clone-{filename}"
    _git("clone", "-b", "main", str(tmp_path / "origin.git"), str(clone),
         cwd=tmp_path)
    _git("config", "user.email", "other@example.com", cwd=clone)
    _git("config", "user.name", "Other", cwd=clone)
    (clone / filename).write_text("remote change")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", f"add {filename}", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    return _git("rev-parse", "HEAD", cwd=clone)


@pytest.fixture
def stub_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Executable ``gh`` stub on PATH, behavior driven by GH_STUB_* env
    vars (inherited by subprocesses): GH_STUB_CREATE ("ok"|"fail"),
    GH_STUB_MERGE ("ok"|"fail"|"fail_once" - fail_once fails the FIRST
    merge call, tracked in GH_STUB_STATE_DIR, then succeeds),
    GH_STUB_VIEW_STATE ("MERGED"|"OPEN"|"CLOSED") and
    GH_STUB_VIEW_MERGEABLE ("MERGEABLE"|"CONFLICTING"|"UNKNOWN").

    The stub is merge-aware like real GitHub: a successful ``pr merge``
    drops a marker in GH_STUB_STATE_DIR and ``pr view`` then reports
    MERGED; before any successful merge it reports OPEN. An explicit
    GH_STUB_VIEW_STATE always wins (for resume scenarios where the PR
    merged before the test began). This mirrors the production
    semantics the _merge_and_wait MERGED-rescue depends on: a failed
    merge COMMAND on an unmerged PR must not read as merged.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "stub-state"
    state_dir.mkdir()
    monkeypatch.setenv("GH_STUB_STATE_DIR", str(state_dir))
    gh = bin_dir / "gh"
    gh.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "auth" ]; then exit 0; fi
        if [ "$1" = "pr" ]; then
          case "$2" in
            create)
              if [ "${{GH_STUB_CREATE:-ok}}" = "fail" ]; then
                echo "stub: pr create failed" >&2; exit 1
              fi
              echo "{STUB_PR_URL}"; exit 0 ;;
            merge)
              if [ "${{GH_STUB_MERGE:-ok}}" = "fail" ]; then
                echo "stub: pr merge failed" >&2; exit 1
              fi
              if [ "${{GH_STUB_MERGE:-ok}}" = "fail_once" ]; then
                count_file="${{GH_STUB_STATE_DIR:-/nonexistent}}/merge-attempts"
                n=$(cat "$count_file" 2>/dev/null || echo 0)
                n=$((n+1)); echo "$n" > "$count_file"
                # merge_pr tries --auto then direct: fail BOTH calls of
                # the first invocation, succeed from the second one on.
                if [ "$n" -le 2 ]; then
                  echo "stub: pr merge conflict" >&2; exit 1
                fi
              fi
              touch "${{GH_STUB_STATE_DIR:-/nonexistent}}/merged" 2>/dev/null
              exit 0 ;;
            view)
              if [ -n "${{GH_STUB_VIEW_STATE:-}}" ]; then
                state="$GH_STUB_VIEW_STATE"
              elif [ -f "${{GH_STUB_STATE_DIR:-/nonexistent}}/merged" ]; then
                state="MERGED"
              else
                state="OPEN"
              fi
              printf '{{"state": "%s", "mergeable": "%s"}}\\n' \\
                "$state" \\
                "${{GH_STUB_VIEW_MERGEABLE:-UNKNOWN}}"
              exit 0 ;;
          esac
        fi
        exit 0
    """))
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def _two_component_manifest() -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="test",
        base_branch="main", single_pr=False,
        components=[
            Component(
                "alpha", "Alpha", "First", [],
                "scripts/ralph/feature/alpha/prd.json",
                "ralph/factory/alpha",
            ),
            Component(
                "beta", "Beta", "Depends on alpha", ["alpha"],
                "scripts/ralph/feature/beta/prd.json",
                "ralph/factory/beta",
            ),
        ],
    )


def _factory_config(**overrides: object) -> FactoryConfig:
    config = FactoryConfig(
        use_worktrees=True, create_prs=True, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        merge_timeout=2.0,
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _base_config(root: Path) -> RalphConfig:
    return RalphConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _run(
    manifest: Manifest, root: Path, config: FactoryConfig | None = None,
) -> tuple[FactoryResult, list[str]]:
    """run_factory with a fake always-succeeding engineer; returns the
    result and the component ids the scheduler actually ran."""
    calls: list[str] = []

    def fake_run(
        component_id: str, *args: object, **kwargs: object,
    ) -> ComponentResult:
        calls.append(component_id)
        return ComponentResult(component_id, success=True, iterations=1)

    with patch("ralph_py.factory._run_component", side_effect=fake_run):
        result = run_factory(
            manifest, config or _factory_config(), _base_config(root),
            PlainUI(no_color=True), root,
        )
    return result, calls


class TestPrFlowGatesCompletion:
    """CRIT-2: each PR-flow failure shape produces the right status and
    dependents do not schedule."""

    def test_push_failure_fails_component_and_blocks_dependents(
        self, tmp_path: Path, stub_gh: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha", "beta"])
        # Break the push target AFTER the initial push, so the
        # origin/main tracking ref exists but pushes fail.
        _git("remote", "set-url", "origin", str(tmp_path / "missing.git"),
             cwd=root)
        manifest = _two_component_manifest()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "push" in alpha.error
        assert beta.status == ComponentStatus.SKIPPED.value
        assert calls == ["alpha"]
        assert result.failed == ["alpha"]
        assert "beta" in result.skipped
        assert result.exit_code == 1

    def test_create_failure_fails_component_and_blocks_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_CREATE", "fail")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = _two_component_manifest()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "Failed to create PR" in alpha.error
        assert beta.status == ComponentStatus.SKIPPED.value
        assert calls == ["alpha"]
        assert result.exit_code == 1

    def test_merge_failure_fails_component_and_blocks_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_MERGE", "fail")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = _two_component_manifest()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "merge failed" in alpha.error
        # The PR itself was created and is recorded for the audit trail.
        assert alpha.pr_url == STUB_PR_URL
        assert beta.status == ComponentStatus.SKIPPED.value
        assert calls == ["alpha"]
        assert result.exit_code == 1

    def test_wait_timeout_marks_merge_pending_dependents_stay_pending(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "OPEN")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = _two_component_manifest()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.MERGE_PENDING.value
        assert alpha.pr_url == STUB_PR_URL  # recorded for the re-poll
        # NOT failed: dependents stay PENDING (re-pollable), never
        # SKIPPED, and are not scheduled.
        assert beta.status == ComponentStatus.PENDING.value
        assert calls == ["alpha"]
        assert result.merge_pending == ["alpha"]
        assert "alpha" not in result.completed
        assert "alpha" not in result.failed
        assert result.exit_code == 1

    def test_merged_pr_completes_and_schedules_dependents(
        self, tmp_path: Path, stub_gh: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = _two_component_manifest()
        main_before = _git("rev-parse", "main", cwd=root)
        head_before = _git("rev-parse", "HEAD", cwd=root)

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.COMPLETED.value
        assert calls == ["alpha", "beta"]
        assert result.completed == ["alpha", "beta"]
        assert result.exit_code == 0
        assert len(result.pr_urls) == 2

        # H-1: the operator's checkout was never touched.
        assert _git("branch", "--show-current", cwd=root) == "operator-branch"
        assert _git("rev-parse", "main", cwd=root) == main_before
        assert _git("rev-parse", "HEAD", cwd=root) == head_before
        notes = root / "OPERATOR_NOTES.txt"
        assert notes.read_text() == "uncommitted operator state"


class TestMergeConflictRerun:
    """R7.5 doctrine end-to-end with real git: a CONFLICTING PR re-runs
    the component against the freshly merged base and then completes."""

    def test_conflicting_pr_reruns_component_then_completes(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_dir = tmp_path / "gh-state"
        state_dir.mkdir()
        monkeypatch.setenv("GH_STUB_STATE_DIR", str(state_dir))
        monkeypatch.setenv("GH_STUB_MERGE", "fail_once")
        monkeypatch.setenv("GH_STUB_VIEW_MERGEABLE", "CONFLICTING")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = _two_component_manifest()

        result, calls = _run(
            manifest, root, _factory_config(max_retries=1),
        )

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        # The conflict consumed one retry; the re-run merged cleanly.
        assert calls == ["alpha", "alpha", "beta"]
        assert alpha.retries == 1
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["alpha", "beta"]
        assert result.failed == []
        assert result.exit_code == 0
        # The re-run created a fresh PR (pointers were cleared).
        assert alpha.pr_url == STUB_PR_URL

    def test_conflicting_pr_with_no_retries_fails_loudly(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_MERGE", "fail")
        monkeypatch.setenv("GH_STUB_VIEW_MERGEABLE", "CONFLICTING")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = _two_component_manifest()

        result, calls = _run(manifest, root)  # max_retries=0

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert calls == ["alpha"]
        assert alpha.status == ComponentStatus.FAILED.value
        assert alpha.failed_check == "merge_conflict"
        assert "conflicts with" in alpha.error
        assert beta.status == ComponentStatus.SKIPPED.value
        assert result.exit_code == 1


class TestMergePendingResume:
    """Crash recovery treats MERGE_PENDING as re-pollable, not failed."""

    def _manifest_with_pending_alpha(self) -> Manifest:
        manifest = _two_component_manifest()
        alpha = manifest.get_component("alpha")
        assert alpha is not None
        alpha.status = ComponentStatus.MERGE_PENDING.value
        alpha.pr_number = 7
        alpha.pr_url = STUB_PR_URL
        return manifest

    def test_resume_repolls_merged_pr_and_unblocks_dependents(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The PR merged BEFORE this resume run - no in-test merge call
        # exists to flip the stub's merge-aware view state, so the
        # already-merged reality is pinned explicitly.
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "MERGED")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = self._manifest_with_pending_alpha()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.COMPLETED.value
        # alpha's engineer never re-ran: only its PR state was polled.
        assert calls == ["beta"]
        assert "alpha" in result.completed
        assert result.merge_pending == []
        assert result.exit_code == 0

    def test_resume_keeps_merge_pending_while_pr_still_open(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "OPEN")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = self._manifest_with_pending_alpha()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.MERGE_PENDING.value
        assert beta.status == ComponentStatus.PENDING.value
        assert calls == []
        assert result.merge_pending == ["alpha"]
        assert result.exit_code == 1

    def test_resume_fails_component_when_pr_closed_without_merge(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "CLOSED")
        root = _make_repo(tmp_path, ["alpha", "beta"])
        manifest = self._manifest_with_pending_alpha()

        result, calls = _run(manifest, root)

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.FAILED.value
        assert "closed without merge" in alpha.error
        assert beta.status == ComponentStatus.SKIPPED.value
        assert calls == []
        assert result.exit_code == 1


class TestPrOutcomeDataclass:
    """push_create_and_merge_pr returns a typed PrOutcome per scenario."""

    def _single_component(self, manifest: Manifest, root: Path) -> Component:
        comp = manifest.get_component("alpha")
        assert comp is not None
        _git("branch", comp.branch_name, "main", cwd=root)
        return comp

    def test_merged_outcome(self, tmp_path: Path, stub_gh: Path) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        manifest = _two_component_manifest()
        comp = self._single_component(manifest, root)

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=2.0,
        )

        assert outcome == PrOutcome(
            pushed=True, pr_number=7, pr_url=STUB_PR_URL,
            merged=True, merge_pending=False, error=None,
        )

    def test_merge_command_failure_with_merged_pr_still_completes(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first real factory run's regression (2026-07-20): gh pr
        merge exited nonzero on post-merge cleanup while the PR sat
        MERGED on GitHub, and the component was failed despite a
        delivered artifact. The merge OUTCOME gates completion (R0.2),
        so a failed merge COMMAND with a MERGED PR is a success."""
        monkeypatch.setenv("GH_STUB_MERGE", "fail")
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "MERGED")
        root = _make_repo(tmp_path, ["alpha"])
        manifest = _two_component_manifest()
        comp = self._single_component(manifest, root)

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=2.0,
        )

        assert outcome.merged is True
        assert outcome.error is None

    def test_merge_command_failure_with_open_pr_still_fails(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rescue must not soften REAL merge failures: a failed
        merge command on a PR that is genuinely not merged stays an
        error."""
        monkeypatch.setenv("GH_STUB_MERGE", "fail")
        root = _make_repo(tmp_path, ["alpha"])
        manifest = _two_component_manifest()
        comp = self._single_component(manifest, root)

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=2.0,
        )

        assert outcome.merged is False
        assert outcome.error is not None
        assert "merge failed" in outcome.error

    def test_merge_pr_never_passes_delete_branch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """--delete-branch is the measured hazard: its local-branch
        deletion fails inside a component worktree and gh reports the
        whole (successful) merge as failed. It must never come back."""
        from ralph_py import pr as pr_module

        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            seen.append(list(cmd))

            class _R:
                returncode = 1  # force both invocation shapes to run
                stderr = "nope"
                stdout = ""

            return _R()

        monkeypatch.setattr(pr_module.subprocess, "run", fake_run)
        pr_module.merge_pr(7, tmp_path)

        assert len(seen) == 2  # --auto attempt, then direct
        for cmd in seen:
            assert "--delete-branch" not in cmd

    def test_remote_branch_deleted_after_confirmed_merge(
        self, tmp_path: Path, stub_gh: Path,
    ) -> None:
        """Explicit remote cleanup replaces gh's --delete-branch: after
        a confirmed merge the pushed branch is removed from origin so a
        recreated same-name branch can push fast-forward next run."""
        root = _make_repo(tmp_path, ["alpha"])
        manifest = _two_component_manifest()
        comp = self._single_component(manifest, root)

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=2.0,
        )
        assert outcome.merged is True

        remote_refs = _git(
            "ls-remote", "--heads", "origin", cwd=root,
        )
        assert comp.branch_name not in remote_refs

    def test_wait_timeout_outcome(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "OPEN")
        root = _make_repo(tmp_path, ["alpha"])
        manifest = _two_component_manifest()
        comp = self._single_component(manifest, root)

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=1.0,
        )

        assert outcome.pushed is True
        assert outcome.merged is False
        assert outcome.merge_pending is True
        assert outcome.pr_number == 7
        assert outcome.error is not None and "not merged within" in outcome.error

    def test_push_failure_outcome(
        self, tmp_path: Path, stub_gh: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        _git("remote", "set-url", "origin", str(tmp_path / "missing.git"),
             cwd=root)
        manifest = _two_component_manifest()
        comp = self._single_component(manifest, root)

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=1.0,
        )

        assert outcome.pushed is False
        assert outcome.merged is False
        assert outcome.merge_pending is False
        assert outcome.error is not None

    def test_existing_merged_pr_short_circuits(
        self, tmp_path: Path, stub_gh: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        manifest = _two_component_manifest()
        comp = manifest.get_component("alpha")
        assert comp is not None
        comp.pr_number = 7
        comp.pr_url = STUB_PR_URL

        outcome = push_create_and_merge_pr(
            comp, manifest, root, PlainUI(no_color=True), merge_timeout=1.0,
        )

        assert outcome.merged is True
        assert outcome.pr_number == 7

    def test_wait_for_merge_distinguishes_closed_from_timeout(
        self, tmp_path: Path, stub_gh: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "CLOSED")
        assert wait_for_merge(7, root, timeout=1.0) == "closed"
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "OPEN")
        assert wait_for_merge(7, root, timeout=1.0) == "pending"
        monkeypatch.setenv("GH_STUB_VIEW_STATE", "MERGED")
        assert wait_for_merge(7, root, timeout=1.0) == "merged"


class TestFetchNeverPull:
    """H-1: base freshness comes from fetch; the checkout is never moved."""

    def test_fetch_updates_tracking_ref_without_touching_checkout(
        self, tmp_path: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        new_sha = _advance_origin(tmp_path)
        main_before = _git("rev-parse", "main", cwd=root)
        head_before = _git("rev-parse", "HEAD", cwd=root)
        assert main_before != new_sha

        assert git.fetch_base_branch("main", root) is None

        assert _git("rev-parse", "refs/remotes/origin/main", cwd=root) == new_sha
        assert _git("rev-parse", "main", cwd=root) == main_before
        assert _git("rev-parse", "HEAD", cwd=root) == head_before
        assert _git("branch", "--show-current", cwd=root) == "operator-branch"
        notes = root / "OPERATOR_NOTES.txt"
        assert notes.read_text() == "uncommitted operator state"

    def test_fetch_reports_error_when_no_remote(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, ["alpha"], with_origin=False)
        error = git.fetch_base_branch("main", root)
        assert error is not None


class TestBaseRefResolution:
    """Worktrees and diffs use origin/<base> when a remote exists and
    fall back to the local base ref otherwise."""

    def test_resolves_to_origin_when_tracking_ref_exists(
        self, tmp_path: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        assert git.resolve_base_ref("main", root) == "origin/main"

    def test_falls_back_to_local_ref_without_remote(
        self, tmp_path: Path,
    ) -> None:
        root = _make_repo(tmp_path, ["alpha"], with_origin=False)
        assert git.resolve_base_ref("main", root) == "main"

    def test_origin_prefixed_ref_passes_through(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, ["alpha"])
        assert git.resolve_base_ref("origin/main", root) == "origin/main"

    def test_diff_against_origin_base_removes_phantom_diffs(
        self, tmp_path: Path,
    ) -> None:
        """Squash merges rewrite SHAs: a branch cut from origin/<base>
        diffed against a stale LOCAL base shows the already-merged files
        as phantom changes. Resolving to origin/<base> removes them."""
        root = _make_repo(tmp_path, ["alpha"])
        # A "squash-merged PR" lands on origin only; local main is stale.
        _advance_origin(tmp_path, filename="file1.txt")
        assert git.fetch_base_branch("main", root) is None

        wt = tmp_path / "wt-feat2"
        _git("worktree", "add", str(wt), "-b", "feat2", "origin/main",
             cwd=root)
        (wt / "file2.txt").write_text("component change")
        _git("add", "-A", cwd=wt)
        _git("commit", "-m", "feat2 change", cwd=wt)

        names = git.get_diff_names("main", cwd=wt)
        assert names == ["file2.txt"]  # no phantom file1.txt
        content = git.get_diff_content("main", cwd=wt)
        assert "file2.txt" in content and "file1.txt" not in content

    def test_factory_worktrees_fall_back_without_remote(
        self, tmp_path: Path,
    ) -> None:
        """Local-only repos (and the test suite) keep working: worktrees
        cut from the local base ref when there is no origin."""
        root = _make_repo(tmp_path, ["alpha", "beta"], with_origin=False)
        manifest = _two_component_manifest()
        config = _factory_config(create_prs=False)

        result, calls = _run(manifest, root, config)

        assert result.completed == ["alpha", "beta"]
        assert calls == ["alpha", "beta"]
        assert result.exit_code == 0
        assert _git("branch", "--show-current", cwd=root) == "operator-branch"
