"""R0.3: the contract phase cannot corrupt the user's repo; failures are loud.

Real-git tests (no LLM):

- A conflicted tier merge happens in a detached temp worktree, is aborted
  there, and leaves the user's checkout byte-identical; the next tier
  check runs cleanly (previously the conflict landed in the user's
  checkout and every subsequent tier failed - CRIT-6).
- A failing contract with no retries left yields a nonzero exit code,
  a run-summary entry, and contract_result evolution-journal events.
- A contract breaker reset actually re-enters scheduling (real fake-agent
  subprocess): the component re-runs, the second contract passes, and the
  run completes with exit code 0.
- Squash-merged (create_prs) mode reports the failure with the failing
  test output and NO breaker attribution.
- A temp worktree that survives removal raises ContractCleanupError
  (fail loudly), which the factory converts into a nonzero exit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ralph_py import contract as contract_mod
from ralph_py.config import RalphConfig
from ralph_py.contract import (
    ContractCleanupError,
    ContractConfig,
    ContractMode,
    run_contract_testing,
    run_tier_check,
)
from ralph_py.factory import FactoryConfig, run_factory
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.timeout import TimeoutConfig
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import VerifyConfig

# Fails (with output) whenever bad_marker.txt exists in the tested tree.
MARKER_TEST_CMD = (
    "if [ -f bad_marker.txt ]; then echo INTEGRATION BROKEN; exit 1; fi"
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        text=True, timeout=30,
    )
    return result.stdout


def _init_repo(root: Path) -> None:
    """Real git repo on main with the ralph scaffolding committed."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "conflict.txt").write_text("base\n")
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt\n")
    feature_dir = ralph_dir / "feature" / "a"
    feature_dir.mkdir(parents=True)
    (feature_dir / "prd.json").write_text(json.dumps({
        "branchName": "ralph/factory/a",
        "userStories": [{
            "id": "US-001", "title": "Test",
            "acceptanceCriteria": ["AC1"],
            "priority": 1, "passes": True, "notes": "",
        }],
    }))
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


def _commit_on_branch(
    root: Path, branch: str, files: dict[str, str], base: str = "main",
) -> None:
    """Create ``branch`` from ``base`` with ``files`` committed, without
    moving the user's checkout (worktree-based, like the factory)."""
    wt = root.parent / f"setup-{branch.replace('/', '-')}"
    _git("worktree", "add", str(wt), "-b", branch, base, cwd=root)
    for rel, content in files.items():
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", f"setup {branch}", cwd=wt)
    _git("worktree", "remove", "--force", str(wt), cwd=root)


def _snapshot_working_tree(root: Path) -> dict[str, bytes]:
    """All file bytes in the working tree, excluding .git and .ralph."""
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in (".git", ".ralph"):
            continue
        if path.is_file():
            snapshot[str(rel)] = path.read_bytes()
    return snapshot


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _component(
    comp_id: str,
    branch: str,
    deps: list[str] | None = None,
    status: str = ComponentStatus.COMPLETED.value,
) -> Component:
    return Component(
        id=comp_id,
        title=comp_id.upper(),
        description="",
        dependencies=deps or [],
        prd_path="scripts/ralph/feature/a/prd.json",
        branch_name=branch,
        status=status,
    )


def _worktree_count(root: Path) -> int:
    porcelain = _git("worktree", "list", "--porcelain", cwd=root)
    return sum(1 for line in porcelain.splitlines() if line.startswith("worktree "))


def _tracked_changes(root: Path) -> list[str]:
    """git status lines for tracked files only. run_factory writes
    untracked persistence state (scripts/ralph/manifest.json, .ralph/)
    by design; contract corruption would show up as tracked modifications
    or staged merge state."""
    return [
        line
        for line in _git("status", "--porcelain", cwd=root).splitlines()
        if not line.startswith("??")
    ]


def _read_journal_events(
    root: Path, event_type: str
) -> list[dict[str, object]]:
    """Read evolution-journal entries of one event type. Since R2.1
    run_factory loads EvolutionConfig via ``load(root_dir)``, which
    anchors the default journal path to the factory root (not the
    process CWD)."""
    journal = root / ".ralph" / "evolution.jsonl"
    if not journal.exists():
        return []
    entries = [
        json.loads(line)
        for line in journal.read_text().splitlines() if line.strip()
    ]
    return [e for e in entries if e.get("event_type") == event_type]


class TestConflictedTierLeavesCheckoutUntouched:
    def test_conflict_recovers_and_checkout_is_byte_identical(
        self, tmp_path: Path,
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        _commit_on_branch(root, "ralph/a", {"conflict.txt": "from a\n"})
        _commit_on_branch(root, "ralph/b", {"conflict.txt": "from b\n"})
        manifest = _make_manifest([
            _component("a", "ralph/a"),
            _component("b", "ralph/b"),
        ])
        config = ContractConfig(
            mode=ContractMode.TIER.value, test_command="true", timeout=60,
        )
        ui = PlainUI(no_color=True)

        before = _snapshot_working_tree(root)
        head_before = _git("rev-parse", "HEAD", cwd=root).strip()

        result = run_tier_check(manifest, ["a", "b"], [], root, config, ui, 0)

        assert result.passed is False
        assert "Merge conflict" in result.test_output
        # Merge-order attribution (deferred-merge mode): the later merge
        # is blamed - documented limitation.
        assert result.breaker == "b"

        # The user's checkout is byte-identical and NOT mid-merge.
        assert _snapshot_working_tree(root) == before
        assert _git("rev-parse", "HEAD", cwd=root).strip() == head_before
        assert _git("status", "--porcelain", cwd=root) == ""
        assert not (root / ".git" / "MERGE_HEAD").exists()

        # The temp worktree is gone: nothing under .ralph/contract and
        # git tracks only the main worktree.
        contract_dir = root / ".ralph" / "contract"
        assert not contract_dir.exists() or not any(contract_dir.iterdir())
        assert _worktree_count(root) == 1

        # Recovery is clean: the next check runs fine (previously the
        # conflicted index made every subsequent tier fail).
        result2 = run_tier_check(manifest, ["a"], [], root, config, ui, 0)
        assert result2.passed is True

    def test_failing_tests_bisect_leaves_checkout_untouched(
        self, tmp_path: Path,
    ) -> None:
        """Test-failure path (merge ok, tests fail, bisection runs):
        the user's checkout still never changes."""
        root = tmp_path / "repo"
        _init_repo(root)
        _commit_on_branch(root, "ralph/a", {"bad_marker.txt": "boom\n"})
        manifest = _make_manifest([_component("a", "ralph/a")])
        config = ContractConfig(
            mode=ContractMode.TIER.value,
            test_command=MARKER_TEST_CMD,
            timeout=60,
        )
        before = _snapshot_working_tree(root)

        result = run_tier_check(
            manifest, ["a"], [], root, config, PlainUI(no_color=True), 0,
        )

        assert result.passed is False
        assert result.breaker == "a"
        assert "INTEGRATION BROKEN" in result.test_output
        assert _snapshot_working_tree(root) == before
        assert _git("status", "--porcelain", cwd=root) == ""
        assert _worktree_count(root) == 1


class TestContractFailureExitsNonzero:
    def test_failing_tier_sets_nonzero_exit_and_run_summary(
        self, tmp_path: Path,
    ) -> None:
        """Deferred-merge mode, retries exhausted: the run must exit
        nonzero with the failure recorded (previously it exited 0 with
        broken integrated code)."""
        root = tmp_path / "repo"
        _init_repo(root)
        _commit_on_branch(root, "ralph/factory/a", {"bad_marker.txt": "boom\n"})
        manifest = _make_manifest([_component("a", "ralph/factory/a")])
        factory_config = FactoryConfig(
            use_worktrees=True, create_prs=False, max_parallel=1,
            max_retries=0, retry_delay=0, review_mode="skip",
            contract_config=ContractConfig(
                mode=ContractMode.TIER.value,
                test_command=MARKER_TEST_CMD,
                timeout=60,
            ),
        )
        base = RalphConfig(
            prompt_file=root / "scripts" / "ralph" / "prompt.md",
            prd_file=root / "scripts" / "ralph" / "prd.json",
            sleep_seconds=0,
            agent_cmd="echo unused",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

        result = run_factory(
            manifest, factory_config, base, PlainUI(no_color=True), root,
        )

        assert result.exit_code == 1
        assert result.contract_failures, "failure must land in the run summary"
        assert "a" in result.failed
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.status == ComponentStatus.FAILED.value
        assert "retries exhausted" in comp.error

        # contract_result journal event recorded for the failure.
        events = _read_journal_events(root, "contract_result")
        assert events, "expected a contract_result journal event"
        assert events[-1]["passed"] is False
        assert events[-1]["breaker"] == "a"

        # The user's checkout survived: no tracked changes, no merge state.
        assert _tracked_changes(root) == []
        assert not (root / ".git" / "MERGE_HEAD").exists()
        assert not (root / "bad_marker.txt").exists()


class TestBreakerRetryReentersScheduling:
    def test_breaker_reruns_and_passing_contract_completes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end with a real fake-agent subprocess: run 1 commits a
        marker that breaks the contract tests, the breaker is reset, the
        scheduling loop re-runs it (run 2 removes the marker), and the
        second contract pass completes the run with exit code 0."""
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        _init_repo(root)
        log_path = tmp_path / "progress.jsonl"

        # The fake engineer: first run plants bad_marker.txt, the retry
        # (which sees the marker on the resumed branch) removes it.
        agent_cmd = (
            "if [ -f bad_marker.txt ]; then "
            "git rm -q bad_marker.txt && git commit -qm fix; "
            "else "
            "echo boom > bad_marker.txt && git add bad_marker.txt "
            "&& git commit -qm break; "
            "fi; "
            "echo '<promise>COMPLETE</promise>'"
        )

        manifest = _make_manifest([
            _component(
                "a", "ralph/factory/a",
                status=ComponentStatus.PENDING.value,
            ),
        ])
        factory_config = FactoryConfig(
            use_worktrees=True, create_prs=False, max_parallel=1,
            max_retries=1, retry_delay=0, review_mode="skip",
            progress_log_path=log_path,
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False,
            ),
            contract_config=ContractConfig(
                mode=ContractMode.TIER.value,
                test_command=MARKER_TEST_CMD,
                timeout=120,
            ),
            timeout_config=TimeoutConfig(
                agent_iteration=60, component_total=120,
            ),
        )
        base = RalphConfig(
            prompt_file=root / "scripts" / "ralph" / "prompt.md",
            prd_file=root / "scripts" / "ralph" / "prd.json",
            sleep_seconds=0,
            agent_cmd=agent_cmd,
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

        result = run_factory(
            manifest, factory_config, base, PlainUI(no_color=True), root,
        )

        assert result.exit_code == 0
        assert result.completed == ["a"]
        assert result.failed == []
        assert result.contract_failures == []
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.status == ComponentStatus.COMPLETED.value
        assert comp.retries == 1

        # Progress log shows the failed contract pass, then the passing one.
        events = [
            json.loads(line) for line in log_path.read_text().splitlines()
        ]
        contract_events = [e for e in events if e["event"] == "contract_result"]
        assert [e["data"]["passed"] for e in contract_events] == [False, True]
        # Journal mirrors both outcomes (recorded for pass AND fail).
        journal_events = _read_journal_events(root, "contract_result")
        assert [e["passed"] for e in journal_events] == [False, True]

        # The user's checkout never saw the marker or any merge state.
        assert _tracked_changes(root) == []
        assert not (root / ".git" / "MERGE_HEAD").exists()
        assert not (root / "bad_marker.txt").exists()
        assert _worktree_count(root) == 1


class TestMergedModeNoBlame:
    def test_squash_merged_mode_reports_failure_without_blame(
        self, tmp_path: Path,
    ) -> None:
        """create_prs mode: components already merged to base. The check
        tests the integrated base, reports the failing output, and must
        NOT attribute blame (previously bisection blamed the first
        component unconditionally)."""
        root = tmp_path / "repo"
        _init_repo(root)
        # The broken integrated state lives on base, as it would after
        # squash merges.
        (root / "bad_marker.txt").write_text("boom\n")
        _git("add", "-A", cwd=root)
        _git("commit", "-q", "-m", "squash-merged components", cwd=root)
        _commit_on_branch(root, "ralph/a", {"a.txt": "a\n"})
        _commit_on_branch(root, "ralph/b", {"b.txt": "b\n"})
        manifest = _make_manifest([
            _component("a", "ralph/a"),
            _component("b", "ralph/b", deps=["a"]),
        ])
        config = ContractConfig(
            mode=ContractMode.TIER.value,
            test_command=MARKER_TEST_CMD,
            timeout=60,
        )
        before = _snapshot_working_tree(root)

        results = run_contract_testing(
            manifest, root, config, PlainUI(no_color=True),
            components_merged=True,
        )

        assert len(results) == 1
        result = results[0]
        assert result.passed is False
        assert result.breaker is None, "merged mode must not attribute blame"
        assert "INTEGRATION BROKEN" in result.test_output
        assert result.components_tested == ["a", "b"]
        assert _snapshot_working_tree(root) == before
        assert _worktree_count(root) == 1

    def test_squash_merged_mode_passes_on_healthy_base(
        self, tmp_path: Path,
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        _commit_on_branch(root, "ralph/a", {"a.txt": "a\n"})
        manifest = _make_manifest([_component("a", "ralph/a")])
        config = ContractConfig(
            mode=ContractMode.TIER.value,
            test_command=MARKER_TEST_CMD,
            timeout=60,
        )

        results = run_contract_testing(
            manifest, root, config, PlainUI(no_color=True),
            components_merged=True,
        )

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].breaker is None


class TestCleanupFailsLoudly:
    def test_surviving_temp_worktree_raises_cleanup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        worktree_path, error = contract_mod._create_temp_worktree(
            "main", root, "cleanup-test",
        )
        assert worktree_path is not None, error

        real_run = contract_mod.run_scrubbed

        def failing_remove(cmd: list[str], **kwargs: object) -> object:
            if cmd[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(
                    cmd, returncode=1, stdout="", stderr="planted failure",
                )
            return real_run(cmd, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(contract_mod, "run_scrubbed", failing_remove)
        with pytest.raises(ContractCleanupError, match="survived removal"):
            contract_mod._remove_temp_worktree(worktree_path, root)
        monkeypatch.undo()

        # Real removal still works afterwards.
        contract_mod._remove_temp_worktree(worktree_path, root)
        assert not worktree_path.exists()

    def test_factory_converts_cleanup_error_to_nonzero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import patch

        root = tmp_path / "repo"
        _init_repo(root)
        manifest = _make_manifest([_component("a", "ralph/factory/a")])
        factory_config = FactoryConfig(
            use_worktrees=True, create_prs=False, max_parallel=1,
            max_retries=0, retry_delay=0, review_mode="skip",
            contract_config=ContractConfig(
                mode=ContractMode.TIER.value, test_command="true",
            ),
        )
        base = RalphConfig(
            prompt_file=root / "scripts" / "ralph" / "prompt.md",
            prd_file=root / "scripts" / "ralph" / "prd.json",
            sleep_seconds=0,
            agent_cmd="echo unused",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

        with patch(
            "ralph_py.factory.run_contract_testing",
            side_effect=ContractCleanupError("worktree survived removal"),
        ):
            result = run_factory(
                manifest, factory_config, base, PlainUI(no_color=True), root,
            )

        assert result.exit_code == 1
        assert any(
            "cleanup failed" in line for line in result.contract_failures
        )
