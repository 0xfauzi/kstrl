"""A resumed run recreates the branches its interrupted predecessor provably made (#460).

The defect: a factory run interrupted mid-component leaves the component
RUNNING in the manifest, its worktree registered under
``.kstrl/worktrees/<run_id>/<component>`` and its branch carrying the
attempt's commits. Re-running the same command reset the component to
PENDING and pruned the worktree (recognising the run as its own), then
refused on the branch as if a stranger had made it, and the operator had
to `git branch -D` it by hand.

The fix: before the reset, the resume reads which branches git still has
checked out in a worktree at the interrupted run's own path for a
component that run left RUNNING or VERIFYING. Those, and only those, are
deleted (the component starts again from the base branch, the same as its
worktree does) and the deletion is written to the run's events. Every
branch it cannot attribute that way is refused exactly as before.

The interrupt is SIGKILL, not SIGINT. The issue's operator pressed Ctrl-C,
and what the resume reads is the state that left: the component RUNNING
and its worktree registered. Measured on this tree (lane 465
measurements.md), one SIGINT to the run's process group did not stop a
`ks factory --no-tui --ui plain` run within 120 s and left exactly that
state after the SIGKILL that followed, so a SIGINT test would end in a
SIGKILL anyway and wait two minutes to get there. SIGKILL reaches the
same state without the wait. Real CLI in a subprocess, real git, a shell
engineer that commits and then blocks on a marker.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from kstrl.manifest import Manifest
from tests.helpers import gitrepo

A = "comp-a"
B = "comp-b"
A_BRANCH = f"kstrl/factory/{A}"
B_BRANCH = f"kstrl/factory/{B}"
SHARED_BRANCH = "kstrl/factory/shared"

FLAGS = (
    "--yes",
    "--ui",
    "plain",
    "--no-color",
    "--no-tui",
    "--max-parallel",
    "1",
    "--max-retries",
    "0",
    "--no-prs",
    "--review-mode",
    "skip",
    "--contract-check",
    "skip",
    "--test-command",
    "true",
    "--typecheck-command",
    "true",
    "--lint-command",
    "true",
)

COMPLETE = "echo '<promise>COMPLETE</promise>'"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8", timeout=30
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """Two independent components, `comp-a` first in manifest order."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    components = []
    for cid in (A, B):
        prd = root / "scripts" / "kstrl" / "feature" / cid / "prd.json"
        prd.parent.mkdir(parents=True)
        story = {
            "id": "US-001",
            "title": "t",
            "acceptanceCriteria": ["AC1"],
            "priority": 1,
            "passes": True,
            "notes": "",
        }
        prd.write_text(
            json.dumps({"branchName": f"kstrl/factory/{cid}", "userStories": [story]}),
            encoding="utf-8",
        )
        components.append(
            {
                "id": cid,
                "title": cid,
                "description": "",
                "dependencies": [],
                "prdPath": f"scripts/kstrl/feature/{cid}/prd.json",
                "branchName": f"kstrl/factory/{cid}",
            }
        )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(
            {
                "version": "1",
                "specFile": "spec.md",
                "projectName": "p",
                "baseBranch": "main",
                "singlePr": False,
                "components": components,
            }
        ),
        encoding="utf-8",
    )
    return root


def _repo_shared_branch(tmp_path: Path) -> Path:
    """Two dependent components on one shared branch (single_pr): `comp-b`
    depends on `comp-a`, and both carry `branchName` `SHARED_BRANCH` in
    their PRD and in the manifest.
    """
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    components = []
    for cid, deps in ((A, []), (B, [A])):
        prd = root / "scripts" / "kstrl" / "feature" / cid / "prd.json"
        prd.parent.mkdir(parents=True)
        story = {
            "id": "US-001",
            "title": "t",
            "acceptanceCriteria": ["AC1"],
            "priority": 1,
            "passes": True,
            "notes": "",
        }
        prd.write_text(
            json.dumps({"branchName": SHARED_BRANCH, "userStories": [story]}),
            encoding="utf-8",
        )
        components.append(
            {
                "id": cid,
                "title": cid,
                "description": "",
                "dependencies": deps,
                "prdPath": f"scripts/kstrl/feature/{cid}/prd.json",
                "branchName": SHARED_BRANCH,
            }
        )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(
            {
                "version": "1",
                "specFile": "spec.md",
                "projectName": "p",
                "baseBranch": "main",
                "singlePr": True,
                "components": components,
            }
        ),
        encoding="utf-8",
    )
    return root


def _env(agent: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    env["AGENT_CMD"] = agent
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    return env


def _argv(root: Path) -> list[str]:
    manifest = root / "scripts" / "kstrl" / "manifest.json"
    return [
        sys.executable,
        "-m",
        "kstrl",
        "factory",
        "--manifest",
        str(manifest),
        "--root",
        str(root),
        *FLAGS,
    ]


def _interrupt_after_commit(tmp_path: Path, root: Path) -> str:
    """Run the factory until comp-a's engineer has committed, then kill the group.

    Returns the interrupted run's id.
    """
    marker = tmp_path / "committed"
    agent = (
        "echo work > work.txt && git add -A && git commit -q -m attempt && "
        f"touch '{marker}' && sleep 120 && {COMPLETE}"
    )
    proc = subprocess.Popen(
        _argv(root),
        cwd=root,
        env=_env(agent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert time.monotonic() < deadline, "the engineer never committed"
            assert proc.poll() is None, proc.communicate()[0]
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
    manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
    comp = manifest.get_component(A)
    assert comp is not None and comp.status == "running", "precondition: comp-a left RUNNING"
    assert _git(root, "log", "-1", "--format=%s", A_BRANCH) == "attempt"
    return manifest.run_id


def _interrupt_comp_b_after_commit(tmp_path: Path, root: Path) -> str:
    """Run the shared (single_pr) branch factory until comp-a has
    COMPLETED and comp-b's engineer has committed on top of it, then kill
    the group.

    comp-a's engineer commits and completes normally; comp-b's engineer
    only starts once comp-a is COMPLETED (it depends on it), commits its
    own attempt onto the shared branch, then blocks on a marker.
    Returns the interrupted run's id.
    """
    marker = tmp_path / "committed-b"
    agent = (
        'c=$(basename "$(pwd)"); echo w > "work-$c.txt"; git add -A; '
        'git commit -q -m "$c"; '
        f"if [ \"$c\" = {B} ]; then touch '{marker}'; sleep 120; fi; "
        f"{COMPLETE}"
    )
    proc = subprocess.Popen(
        _argv(root) + ["--single-pr"],
        cwd=root,
        env=_env(agent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert time.monotonic() < deadline, "comp-b's engineer never committed"
            assert proc.poll() is None, proc.communicate()[0]
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
    manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
    comp_a = manifest.get_component(A)
    comp_b = manifest.get_component(B)
    assert comp_a is not None and comp_a.status == "completed", "precondition: comp-a COMPLETED"
    assert comp_b is not None and comp_b.status == "running", "precondition: comp-b left RUNNING"
    assert _git(root, "log", "-1", "--format=%s", SHARED_BRANCH) == B
    return manifest.run_id


def _resume(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _argv(root),
        cwd=root,
        env=_env(COMPLETE),
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=300,
    )


def _resume_single_pr(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _argv(root) + ["--single-pr"],
        cwd=root,
        env=_env(COMPLETE),
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=300,
    )


def _events_text(root: Path, run_id: str) -> str:
    return (root / ".kstrl" / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")


class TestResumeReclaimsTheInterruptedRunsBranch:
    def test_the_resume_deletes_its_own_branch_records_it_and_completes(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        interrupted = _interrupt_after_commit(tmp_path, root)
        tip = _git(root, "rev-parse", A_BRANCH)

        resumed = _resume(root)
        out = resumed.stdout + resumed.stderr

        assert resumed.returncode == 0, out
        assert "Refusing to run" not in out, out
        manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
        assert [c.status for c in manifest.components] == ["completed", "completed"], out
        # The deletion is in the resumed run's own events, naming the run
        # that left the branch and the commit it pointed at.
        events = _events_text(root, manifest.run_id)
        assert f"Deleted stale branch '{A_BRANCH}'" in events, out
        assert interrupted in events and tip in events, out

    def test_a_branch_the_interrupted_run_did_not_make_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        _interrupt_after_commit(tmp_path, root)
        # comp-b never started. A branch under its name with a commit on it
        # came from somewhere else.
        _git(root, "branch", B_BRANCH, "main")
        other = tmp_path / "other"
        _git(root, "worktree", "add", "-q", str(other), B_BRANCH)
        (other / "foreign.txt").write_text("x\n", encoding="utf-8")
        _git(other, "add", "foreign.txt")
        _git(other, "commit", "-q", "-m", "foreign")
        _git(root, "worktree", "remove", "--force", str(other))

        resumed = _resume(root)
        out = resumed.stdout + resumed.stderr

        assert resumed.returncode == 2, out
        refusals = [line for line in out.splitlines() if "already exists" in line]
        assert any(B_BRANCH in line for line in refusals), out
        assert not any(A_BRANCH in line for line in refusals), out
        assert _git(root, "log", "-1", "--format=%s", B_BRANCH) == "foreign"

    def test_a_branch_whose_worktree_registration_is_gone_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        _interrupt_after_commit(tmp_path, root)
        # The evidence that ties the branch to the interrupted run is its
        # worktree registration. Without it the branch cannot be attributed.
        worktree = next((root / ".kstrl" / "worktrees").glob(f"*/{A}"))
        _git(root, "worktree", "remove", "--force", str(worktree))

        resumed = _resume(root)
        out = resumed.stdout + resumed.stderr

        assert resumed.returncode == 2, out
        refusals = [line for line in out.splitlines() if "already exists" in line]
        assert any(A_BRANCH in line for line in refusals), out
        assert _git(root, "log", "-1", "--format=%s", A_BRANCH) == "attempt"

    def test_a_single_pr_shared_branch_is_still_refused(self, tmp_path: Path) -> None:
        """single_pr shares one branch across components. Without the
        `manifest.single_pr` exclusion in `_interrupted_run_branches`, the
        resume would see comp-b's worktree registered on that branch,
        attribute the WHOLE branch to comp-b's interrupted attempt alone,
        and delete it - taking comp-a's already-COMPLETED commit down
        with it. The branch must still be refused as unmerged, not
        silently recreated from base.
        """
        root = _repo_shared_branch(tmp_path)
        _interrupt_comp_b_after_commit(tmp_path, root)

        resumed = _resume_single_pr(root)
        out = resumed.stdout + resumed.stderr

        assert resumed.returncode == 2, out
        refusals = [line for line in out.splitlines() if "already exists" in line]
        assert any(SHARED_BRANCH in line for line in refusals), out
        assert A in _git(root, "log", "--format=%s", SHARED_BRANCH), out
