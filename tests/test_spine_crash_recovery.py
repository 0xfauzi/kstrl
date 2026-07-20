"""Spine tier II (R4.2): crash recovery after a SIGKILL mid-run.

A real factory run in a real second PROCESS is SIGKILLed while a
component is mid-flight - either during the engineer iteration (status
RUNNING on disk) or during Phase 1 verification (status VERIFYING); the
in-flight command signals via a marker file, then sleeps, so the kill
provably lands inside the phase under test. That leaves exactly the
state crash recovery must handle: a manifest persisted with an
intermediate status, a provisioned worktree under the run-scoped
``.ralph/worktrees/<run_id>/`` layout, and the component branch checked
out there.

The restart then proves the R0.5 recovery contract end to end:
- RUNNING/VERIFYING statuses are reset to PENDING;
- the crashed run's ``<run_id>/`` worktree is pruned (run ids differ, so
  the new run can never mistake it for its own);
- the crashed attempt's branch is handled per the stale-branch policy:
  auto-deleted when fully merged, loudly REFUSED (exit 2, operator
  decides) when it carries unmerged commits - never silently reused;
- the manifest ends consistent: the component re-runs to COMPLETED with
  no stale error, and no worktree survives the run.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kstrl.factory import run_factory
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from tests.spine_utils import (
    COMPLETE_LINE,
    base_config,
    component,
    factory_config,
    git,
    init_ralph_repo,
    make_manifest,
)

pytestmark = pytest.mark.spine

COMP = "comp-a"
BRANCH = f"ralph/factory/{COMP}"

# Real factory run in a child process. argv: root, manifest_path,
# verify test command, engineer agent command.
_DRIVER_SCRIPT = """
import sys
from pathlib import Path
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.manifest import Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

root = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
result = run_factory(
    Manifest.load(manifest_path),
    FactoryConfig(
        use_worktrees=True, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command=sys.argv[3], typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=300.0,
        ),
    ),
    KstrlConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0, agent_cmd=sys.argv[4],
        kstrl_branch="", kstrl_branch_explicit=True,
        ui_mode="plain", no_color=True,
    ),
    PlainUI(no_color=True),
    root,
    manifest_path=manifest_path,
)
sys.exit(result.exit_code)
"""


def _crash_factory(
    root: Path,
    manifest_path: Path,
    marker: Path,
    agent_cmd: str,
    verify_cmd: str,
) -> None:
    """Run the driver until ``marker`` appears, then SIGKILL it.

    The phase under test owns the marker: touch-then-sleep as the agent
    command crashes mid-RUNNING, as the verify command mid-VERIFYING.
    """
    proc = subprocess.Popen(
        [
            sys.executable, "-c", _DRIVER_SCRIPT,
            str(root), str(manifest_path), verify_cmd, agent_cmd,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert time.monotonic() < deadline, (
                "factory run never reached the phase under test"
            )
            assert proc.poll() is None, (
                f"factory run died early: {proc.communicate()[0]}"
            )
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)


def _assert_crashed_state(
    root: Path, manifest_path: Path, expected_status: str,
) -> Path:
    """The kill really landed inside the intended phase: the manifest
    persisted the intermediate status and the run-scoped worktree
    survived. Returns the stale worktree path."""
    crashed = Manifest.load(manifest_path)
    assert crashed.components[0].status == expected_status
    stale_worktrees = sorted((root / ".ralph" / "worktrees").glob(f"*/{COMP}"))
    assert len(stale_worktrees) == 1, (
        f"expected exactly one crashed worktree, found {stale_worktrees}"
    )
    return stale_worktrees[0]


def _no_worktree_dirs_left(root: Path) -> bool:
    worktree_root = root / ".ralph" / "worktrees"
    if not worktree_root.exists():
        return True
    return not any(p.is_dir() for p in worktree_root.iterdir())


class TestCrashRecovery:
    @pytest.mark.parametrize(
        "crashed_status",
        [ComponentStatus.RUNNING.value, ComponentStatus.VERIFYING.value],
    )
    def test_restart_resets_intermediate_status_prunes_worktree_completes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        crashed_status: str,
    ) -> None:
        """Kill mid-engineer (RUNNING) or mid-verify (VERIFYING): the
        crashed attempt committed nothing, so its branch equals base and
        the restart auto-deletes it, prunes the stale run-dir worktree,
        resets the intermediate status to PENDING, and re-runs to
        COMPLETED."""
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_ralph_repo(root, (COMP,))
        manifest_path = tmp_path / "manifest.json"
        make_manifest([component(COMP)]).save(manifest_path)
        marker = tmp_path / "phase-started"
        hang = f"touch '{marker}' && sleep 45"
        if crashed_status == ComponentStatus.RUNNING.value:
            # The ENGINEER signals and hangs: verify is never reached.
            agent_cmd, verify_cmd = hang, "true"
        else:
            # The engineer completes; Phase 1 VERIFY signals and hangs.
            agent_cmd, verify_cmd = COMPLETE_LINE, hang

        _crash_factory(root, manifest_path, marker, agent_cmd, verify_cmd)
        stale_worktree = _assert_crashed_state(
            root, manifest_path, crashed_status,
        )

        out = io.StringIO()
        restarted = Manifest.load(manifest_path)
        result = run_factory(
            restarted, factory_config(), base_config(root),
            PlainUI(no_color=True, file=out), root,
            manifest_path=manifest_path,
        )
        ui_output = out.getvalue()

        assert (
            f"Resetting '{COMP}' from {crashed_status} to PENDING"
            in ui_output
        )
        assert "Pruned 1 stale worktree(s) from previous runs" in ui_output
        assert f"Deleted stale branch '{BRANCH}'" in ui_output

        assert result.exit_code == 0
        assert result.completed == [COMP]
        assert not stale_worktree.exists()
        assert _no_worktree_dirs_left(root)
        listing = git("worktree", "list", "--porcelain", cwd=root)
        assert [
            line for line in listing.splitlines()
            if line.startswith("worktree ")
        ] == [f"worktree {root}"]

        # The persisted manifest is consistent after recovery.
        final = Manifest.load(manifest_path)
        assert final.components[0].status == ComponentStatus.COMPLETED.value
        assert final.components[0].error == ""

    def test_restart_refuses_crashed_branch_with_commits_then_operator_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Crashed attempt COMMITTED work before dying: the restart still
        resets state and prunes the stale worktree, but refuses to run
        (exit 2) rather than silently reuse or destroy the branch - loud
        beats lossy (R0.5). Deleting the branch, as the refusal
        instructs, lets the next run complete."""
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_ralph_repo(root, (COMP,))
        manifest_path = tmp_path / "manifest.json"
        make_manifest([component(COMP)]).save(manifest_path)
        committing_agent = (
            "echo progress > progress.txt && git add progress.txt && "
            "git commit -q -m 'crashed attempt progress' && "
            + COMPLETE_LINE
        )

        marker = tmp_path / "verify-started"
        _crash_factory(
            root, manifest_path, marker, committing_agent,
            f"touch '{marker}' && sleep 45",
        )
        stale_worktree = _assert_crashed_state(
            root, manifest_path, ComponentStatus.VERIFYING.value,
        )

        out = io.StringIO()
        refused = run_factory(
            Manifest.load(manifest_path), factory_config(),
            base_config(root), PlainUI(no_color=True, file=out), root,
            manifest_path=manifest_path,
        )
        ui_output = out.getvalue()

        assert refused.exit_code == 2
        assert refused.completed == []
        assert "Refusing to run: stale component branches found" in ui_output
        assert f"branch '{BRANCH}'" in ui_output
        # Stale-worktree handling ran even though the run was refused.
        assert "Pruned 1 stale worktree(s) from previous runs" in ui_output
        assert not stale_worktree.exists()
        # The crashed attempt's commits were preserved, not destroyed.
        assert git("branch", "--list", BRANCH, cwd=root).strip()
        assert "progress.txt" in git(
            "ls-tree", "--name-only", BRANCH, cwd=root,
        ).splitlines()

        # Operator path from the refusal message: delete the branch and
        # re-run; recovery then completes and the manifest is consistent.
        git("branch", "-D", BRANCH, cwd=root)
        rerun = run_factory(
            Manifest.load(manifest_path), factory_config(),
            base_config(root), PlainUI(no_color=True, file=io.StringIO()),
            root, manifest_path=manifest_path,
        )
        assert rerun.exit_code == 0
        assert rerun.completed == [COMP]
        final = Manifest.load(manifest_path)
        assert final.components[0].status == ComponentStatus.COMPLETED.value
        assert final.components[0].error == ""
        assert _no_worktree_dirs_left(root)
