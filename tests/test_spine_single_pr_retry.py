"""A single_pr retry never removes another component's commits (#566).

In single_pr mode every component commits onto one shared branch. A
component whose attempt is killed by the component timeout is retried on a
branch reset past the killed attempt's commits (R0.1). Measured before the
fix: the reset deleted the shared branch and cut it again from the base, so
the retry dropped every earlier component's commits while the run reported
each component completed and exited 0. The per-component modes go through
the same reset and kept every sibling's commits before the fix; a test here
holds them to that.

Real git and real ``bash -lc`` engineers through the real ``run_factory``,
built with the #543 helpers in ``tests/test_spine_dependency_base.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl import factory
from kstrl.timeout import TimeoutConfig
from tests.spine_utils import component, git, init_kstrl_repo, make_manifest
from tests.test_spine_dependency_base import (
    SHARED_BRANCH,
    _project,
    _run,
    _statuses,
)

pytestmark = pytest.mark.spine

# c's first attempt commits c.txt, then outlives the component wall clock.
# The marker file sits outside the worktree, so the retry does not sleep.
_FIRST_C_ATTEMPT_COMMITS_THEN_TIMES_OUT = (
    'if [ "$comp" = c ] && [ ! -f "$ENGINEER_LOG.c-slept" ]; then '
    'touch "$ENGINEER_LOG.c-slept"; echo killed > c.txt; git add -A; '
    "git commit -q -m c-killed; sleep 30; fi"
)


@pytest.mark.parametrize(
    "deps",
    [{"a": [], "c": []}, {"a": [], "c": ["a"]}],
    ids=["independent", "dependent"],
)
def test_a_timed_out_single_pr_retry_keeps_earlier_components_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deps: dict[str, list[str]],
) -> None:
    """a commits onto the shared branch, c's first attempt commits and is
    killed, c's retry commits. The branch holds a's commit and the retry's,
    and not the killed attempt's."""
    root, manifest, _ = _project(tmp_path, deps, single_pr=True)

    _run(
        tmp_path,
        root,
        manifest,
        monkeypatch,
        extra=_FIRST_C_ATTEMPT_COMMITS_THEN_TIMES_OUT,
        create_prs=False,
        max_retries=1,
        timeout_config=TimeoutConfig(component_total=5.0),
    )

    assert (tmp_path / "engineer.log.c-slept").exists()
    assert _statuses(tmp_path) == {"a": "completed", "c": "completed"}
    subjects = git("log", "--format=%s", SHARED_BRANCH, cwd=root).splitlines()
    assert subjects == ["c", "a", "init"], subjects


@pytest.mark.parametrize(
    ("deps", "c_subjects"),
    [({"a": [], "c": []}, ["c", "init"]), ({"a": [], "c": ["a"]}, ["c", "a", "init"])],
    ids=["independent", "dependent"],
)
def test_a_timed_out_per_component_retry_keeps_the_siblings_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deps: dict[str, list[str]],
    c_subjects: list[str],
) -> None:
    """The same run with one branch per component, which the fresh reset
    also goes through: a's branch keeps a's commit, and c's branch drops
    the killed attempt's commit."""
    root, manifest, _ = _project(tmp_path, deps)

    _run(
        tmp_path,
        root,
        manifest,
        monkeypatch,
        extra=_FIRST_C_ATTEMPT_COMMITS_THEN_TIMES_OUT,
        create_prs=False,
        max_retries=1,
        timeout_config=TimeoutConfig(component_total=5.0),
    )

    assert (tmp_path / "engineer.log.c-slept").exists()
    assert _statuses(tmp_path) == {"a": "completed", "c": "completed"}
    on_a = git("log", "--format=%s", "kstrl/factory/a", cwd=root).splitlines()
    on_c = git("log", "--format=%s", "kstrl/factory/c", cwd=root).splitlines()
    assert on_a == ["a", "init"], on_a
    assert on_c == c_subjects, on_c


def test_a_failed_fresh_setup_leaves_the_shared_branch_where_it_was(tmp_path: Path) -> None:
    """The reset and the checkout are one git command, so a fresh setup that
    fails leaves the shared branch where it was. An unresolvable recut
    point makes it fail; deleting the branch first and cutting it again
    in a second command would lose the branch and a's commit with it."""
    root = tmp_path / "repo"
    init_kstrl_repo(root, ("a", "c"))
    run_id = "run-566"
    wt_a = factory._setup_worktree("a", SHARED_BRANCH, "main", root, run_id)
    (wt_a / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=wt_a)
    git("commit", "-q", "-m", "a", cwd=wt_a)
    factory._cleanup_worktree("a", root, run_id)
    wt_c = factory._setup_worktree("c", SHARED_BRANCH, "main", root, run_id)
    (wt_c / "c.txt").write_text("killed\n")
    git("add", "c.txt", cwd=wt_c)
    git("commit", "-q", "-m", "c-killed", cwd=wt_c)
    killed_tip = git("rev-parse", "HEAD", cwd=wt_c)
    factory._cleanup_worktree("c", root, run_id)

    with pytest.raises(RuntimeError, match="Failed to create worktree for 'c'"):
        factory._setup_worktree(
            "c",
            SHARED_BRANCH,
            "main",
            root,
            run_id,
            fresh_from_base=True,
            recut_at="0" * 40,
        )

    assert git("rev-parse", SHARED_BRANCH, cwd=root) == killed_tip
    assert "a.txt" in git("ls-tree", "--name-only", SHARED_BRANCH, cwd=root).split()


def test_the_recut_point_is_the_recorded_start_in_single_pr_mode_only() -> None:
    """single_pr resets to the recorded start commit and refuses without
    one; the other modes reset to the base branch (None)."""
    comp = component("c")
    comp.branch_name = SHARED_BRANCH
    manifest = make_manifest([comp])

    assert factory._recut_point(manifest, comp, {"c": "abc123"}) is None

    manifest.single_pr = True
    assert factory._recut_point(manifest, comp, {"c": "abc123"}) == "abc123"
    with pytest.raises(RuntimeError, match="no recorded start commit for 'c'"):
        factory._recut_point(manifest, comp, {})
