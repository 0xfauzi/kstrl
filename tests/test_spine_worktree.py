"""Spine tier I (R4.2): worktree lifecycle against real git repos.

No mocks at the boundary under test: ``_setup_worktree`` and
``_cleanup_worktree`` run real git subprocesses against real repos, and
the lock-exclusion tests use real second PROCESSES (flock exclusion is
invisible to threads within one process's open file description).

Covered:
- create from the requested base branch (and from origin/<base> when a
  remote exists, per R0.2 freshness semantics);
- cleanup removes the worktree directory and its git registration;
- recreate-after-crash: dirty dir + stale index.lock, branch-commit
  resume, and ``fresh_from_base`` discard (R0.1 retry semantics);
- the per-component flock and the run-level factory flock actually
  exclude across processes, and the run lock dies with its holder.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kstrl.factory import _cleanup_worktree, _setup_worktree, run_factory
from kstrl.ui.plain import PlainUI
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    git,
    init_kstrl_repo,
    make_manifest,
)

pytestmark = pytest.mark.spine

RUN_ID = "spine-run-1"
COMP = "comp-a"
BRANCH = "kstrl/factory/comp-a"

# Child that takes an exclusive flock on the given path and holds it
# until killed, standing in for a live contending kstrl process.
_LOCK_HOLDER_SCRIPT = """
import fcntl, sys, time
fp = open(sys.argv[1], "a+")
fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(120)
"""

# Child that runs the real _setup_worktree. Touches the ready file after
# imports finish, immediately before the call, so the parent can time the
# hold window against the call itself rather than interpreter startup.
_SETUP_CHILD_SCRIPT = """
import sys
from pathlib import Path
from kstrl.factory import _setup_worktree
root = Path(sys.argv[1])
Path(sys.argv[2]).write_text("ready")
wt = _setup_worktree("comp-a", "kstrl/factory/comp-a", "main", root, sys.argv[3])
print(wt, flush=True)
"""


def _init_plain_repo(root: Path) -> tuple[str, str]:
    """Real repo with main plus a develop branch one commit ahead.

    Returns (main_sha, develop_sha).
    """
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "spine@test", cwd=root)
    git("config", "user.name", "Spine Test", cwd=root)
    (root / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    main_sha = git("rev-parse", "main", cwd=root)
    git("checkout", "-q", "-b", "develop", cwd=root)
    (root / "dev.txt").write_text("develop-only\n")
    git("add", "dev.txt", cwd=root)
    git("commit", "-q", "-m", "develop commit", cwd=root)
    develop_sha = git("rev-parse", "develop", cwd=root)
    git("checkout", "-q", "main", cwd=root)
    return main_sha, develop_sha


def _worktree_registered(root: Path, worktree_path: Path) -> bool:
    listing = git("worktree", "list", "--porcelain", cwd=root)
    return str(worktree_path) in listing


class TestWorktreeLifecycle:
    def test_setup_creates_worktree_from_requested_base(
        self, tmp_path: Path,
    ) -> None:
        """The worktree is cut from the requested base branch (develop),
        not the default branch, at the run-scoped path (R0.5 layout)."""
        root = tmp_path / "repo"
        main_sha, develop_sha = _init_plain_repo(root)

        wt = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)

        assert wt == root / ".kstrl" / "worktrees" / RUN_ID / COMP
        assert wt.is_dir()
        assert _worktree_registered(root, wt)
        assert git("branch", "--show-current", cwd=wt) == BRANCH
        head = git("rev-parse", "HEAD", cwd=wt)
        assert head == develop_sha
        assert head != main_sha
        assert (wt / "dev.txt").exists()

    def test_setup_cuts_from_origin_base_not_stale_local(
        self, tmp_path: Path,
    ) -> None:
        """With a remote, the worktree is cut from origin/<base> even when
        the local base ref is stale (R0.2: dependents must build on the
        squash-merged remote history)."""
        root = tmp_path / "repo"
        _, local_develop_sha = _init_plain_repo(root)
        origin = tmp_path / "origin.git"
        git("init", "-q", "--bare", str(origin), cwd=tmp_path)
        git("remote", "add", "origin", str(origin), cwd=root)
        git("push", "-q", "-u", "origin", "main", "develop", cwd=root)

        # Advance origin/develop from a second clone; local develop is
        # now one commit behind the remote.
        clone = tmp_path / "clone"
        git("clone", "-q", "-b", "develop", str(origin), str(clone),
            cwd=tmp_path)
        git("config", "user.email", "other@test", cwd=clone)
        git("config", "user.name", "Other", cwd=clone)
        (clone / "remote.txt").write_text("landed remotely\n")
        git("add", "remote.txt", cwd=clone)
        git("commit", "-q", "-m", "remote-only commit", cwd=clone)
        git("push", "-q", "origin", "develop", cwd=clone)
        remote_develop_sha = git("rev-parse", "HEAD", cwd=clone)

        wt = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)

        head = git("rev-parse", "HEAD", cwd=wt)
        assert head == remote_develop_sha
        assert head != local_develop_sha
        assert (wt / "remote.txt").exists()

    def test_cleanup_removes_worktree_and_registration(
        self, tmp_path: Path,
    ) -> None:
        """Cleanup removes the directory and the git worktree entry; the
        component branch survives (it is only deleted at merge time)."""
        root = tmp_path / "repo"
        _init_plain_repo(root)
        wt = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)

        _cleanup_worktree(COMP, root, RUN_ID)

        assert not wt.exists()
        assert not _worktree_registered(root, wt)
        assert git("branch", "--list", BRANCH, cwd=root).strip()

    def test_recreate_after_crash_resumes_branch_commits(
        self, tmp_path: Path,
    ) -> None:
        """A killed attempt leaves a dirty worktree and a stale
        index.lock under .git/worktrees/<comp>/. Recreating for the same
        run must clear both and resume the branch WITH its commits
        (non-timeout retry semantics), dropping uncommitted dirt."""
        root = tmp_path / "repo"
        _init_plain_repo(root)
        wt = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)

        # Simulate the crashed attempt: one committed step of progress,
        # one uncommitted file, and git killed mid-operation.
        (wt / "progress.txt").write_text("committed progress\n")
        git("add", "progress.txt", cwd=wt)
        git("commit", "-q", "-m", "crashed attempt progress", cwd=wt)
        crashed_sha = git("rev-parse", "HEAD", cwd=wt)
        (wt / "uncommitted.txt").write_text("dirty\n")
        stale_lock = root / ".git" / "worktrees" / COMP / "index.lock"
        stale_lock.parent.mkdir(parents=True, exist_ok=True)
        stale_lock.write_text("")

        wt2 = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)

        assert wt2 == wt
        assert not stale_lock.exists()
        assert git("rev-parse", "HEAD", cwd=wt2) == crashed_sha
        assert (wt2 / "progress.txt").exists()
        assert not (wt2 / "uncommitted.txt").exists()
        assert git("status", "--porcelain", cwd=wt2) == ""

    def test_recreate_fresh_from_base_discards_crashed_commits(
        self, tmp_path: Path,
    ) -> None:
        """fresh_from_base=True (timeout retry, R0.1) deletes the branch
        so the worktree is recut from the base, discarding the killed
        attempt's possibly-poisoned commits."""
        root = tmp_path / "repo"
        _, develop_sha = _init_plain_repo(root)
        wt = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)
        (wt / "poisoned.txt").write_text("from the killed attempt\n")
        git("add", "poisoned.txt", cwd=wt)
        git("commit", "-q", "-m", "poisoned progress", cwd=wt)
        crashed_sha = git("rev-parse", "HEAD", cwd=wt)

        wt2 = _setup_worktree(
            COMP, BRANCH, "develop", root, RUN_ID, fresh_from_base=True,
        )

        head = git("rev-parse", "HEAD", cwd=wt2)
        assert head == develop_sha
        assert head != crashed_sha
        assert not (wt2 / "poisoned.txt").exists()

    def test_recreate_when_crashed_worktree_dir_was_deleted(
        self, tmp_path: Path,
    ) -> None:
        """A worktree directory deleted after a crash (tmp cleaner,
        operator rm -rf) leaves a registered-but-missing entry under
        .git/worktrees/<comp>/. Setup must clear the registration and
        recreate the worktree on the same branch (reuse semantics, like
        any other same-run retry)."""
        root = tmp_path / "repo"
        _init_plain_repo(root)
        wt = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)
        shutil.rmtree(wt)  # dir gone, .git/worktrees/comp-a/ remains

        wt2 = _setup_worktree(COMP, BRANCH, "develop", root, RUN_ID)

        assert wt2.is_dir()
        assert git("branch", "--show-current", cwd=wt2) == BRANCH


@pytest.mark.skipif(
    sys.platform == "win32", reason="flock is POSIX-only (documented degrade)",
)
class TestComponentLockTwoProcessExclusion:
    def test_component_lock_blocks_second_process_until_released(
        self, tmp_path: Path,
    ) -> None:
        """While a real second process holds the per-component flock,
        _setup_worktree in another process blocks; it completes only
        after the holder dies. Unlocked setup takes ~0.03s (measured), so
        a 1s hold with the child still running is exclusion, not noise."""
        root = tmp_path / "repo"
        _init_plain_repo(root)
        lock_path = root / ".kstrl" / "worktrees" / f"{COMP}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        ready = tmp_path / "child-ready"
        worktree_path = root / ".kstrl" / "worktrees" / RUN_ID / COMP

        holder = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(lock_path)],
            stdout=subprocess.PIPE, text=True,
        )
        child: subprocess.Popen[str] | None = None
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "locked"

            child = subprocess.Popen(
                [sys.executable, "-c", _SETUP_CHILD_SCRIPT,
                 str(root), str(ready), RUN_ID],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            deadline = time.monotonic() + 30
            while not ready.exists():
                assert time.monotonic() < deadline, (
                    "setup child never reached _setup_worktree"
                )
                assert child.poll() is None, (
                    f"setup child died early: {child.communicate()}"
                )
                time.sleep(0.01)

            time.sleep(1.0)  # hold window: ~30x unlocked setup time
            assert child.poll() is None, (
                "second process finished _setup_worktree while the "
                "component lock was held: flock does not exclude"
            )
            assert not worktree_path.exists()

            # Release the lock: the blocked child must now complete.
            holder.kill()
            holder.wait(timeout=10)
            out, err = child.communicate(timeout=30)
            assert child.returncode == 0, f"setup child failed: {err}"
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=10)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=10)

        assert worktree_path.is_dir()
        assert str(worktree_path) in out
        assert git("branch", "--show-current", cwd=worktree_path) == BRANCH


@pytest.mark.skipif(
    sys.platform == "win32", reason="flock is POSIX-only (documented degrade)",
)
class TestRunLockTwoProcessExclusion:
    def test_run_lock_refuses_second_invocation_until_holder_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """While a real process holds .kstrl/factory.lock, run_factory
        refuses to start (exit 2, nothing scheduled). Once the holder
        dies the same invocation succeeds: the flock dies with its
        process, so a stale lock FILE can never wedge the factory."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        lock_path = root / ".kstrl" / "factory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        holder = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(lock_path)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "locked"

            manifest = make_manifest([component(COMP)])
            refused = run_factory(
                manifest, factory_config(), base_config(root),
                PlainUI(no_color=True), root,
            )

            assert refused.exit_code == 2
            assert refused.completed == []
            assert manifest.components[0].status == "pending"
            assert not (root / ".kstrl" / "worktrees").exists()
        finally:
            holder.kill()
            holder.wait(timeout=10)

        rerun = run_factory(
            make_manifest([component(COMP)]), factory_config(),
            base_config(root), PlainUI(no_color=True), root,
        )
        assert rerun.exit_code == 0
        assert rerun.completed == [COMP]
