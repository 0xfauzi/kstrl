"""A lockfile is judged through the manifest it pins (#544).

Measured in paid run P-A: the seed repository commits a pyproject.toml and
no uv.lock, the engineer's first ``uv run`` writes uv.lock, and the scope
guard flags it, which cost one engineer retry in 3 of 3 slices. A lockfile
is now in scope when its manifest is, or when the component created it
beside a manifest the base had and the diff leaves unchanged. A change to a
lockfile the base already had, with its manifest out of scope, is still a
violation.

Every test drives both layers that answer the scope question on a real
repository: the in-loop guard (``guards.enforce_allowed_paths`` with the
baseline the factory takes) and Phase 1 (``verify.check_diff_scope``).
The two headline tests let a real ``uv run`` write the lockfile.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kstrl import git
from kstrl.config import KstrlConfig
from kstrl.guards import enforce_allowed_paths
from kstrl.init_cmd import gitignore_block
from kstrl.policy import LOCKFILE_MANIFESTS
from kstrl.ui.plain import PlainUI
from kstrl.verify import check_diff_scope
from tests.helpers.gitrepo import git_in, set_identity

ALLOWED = ["src/"]

PYPROJECT = (
    '[project]\nname = "probe"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n'
)

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


def _seed(root: Path, files: dict[str, str]) -> None:
    """A repo on ``main`` holding ``files`` plus the Python .gitignore
    ``ks init`` writes, committed, with a component branch checked out."""
    root.mkdir(parents=True, exist_ok=True)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    (root / ".gitignore").write_text(gitignore_block("Python"), encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("X = 1\n", encoding="utf-8")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "seed")
    git_in(root, "checkout", "-q", "-b", "kstrl/factory/comp")


def _commit_all(root: Path) -> None:
    """How the engineer commits: everything git does not ignore."""
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "story")


def _uv_run(root: Path) -> None:
    """The engineer's first ``uv run``. Offline: the project has no
    dependencies, so there is nothing to download.

    The inherited variables that point uv at a project or environment are
    removed, so this ``uv run`` can only ever sync the ``.venv`` in ``root``
    and never the environment the test suite itself runs in."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT")
    }
    subprocess.run(
        ["uv", "run", "--offline", "python", "-c", "pass"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )


def _in_loop(
    root: Path, baseline: git.WorkspaceBaseline, allowed: list[str] = ALLOWED
) -> tuple[bool, list[str]]:
    config = KstrlConfig(
        max_iterations=1,
        prompt_file=root / "prompt.md",
        prd_file=root / "prd.json",
        sleep_seconds=0,
        interactive=False,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        allowed_paths=allowed,
    )
    return enforce_allowed_paths(
        config, PlainUI(no_color=True, file=io.StringIO()), root, baseline=baseline
    )


def _phase_1_violations(root: Path, allowed: list[str] = ALLOWED) -> list[str]:
    """The files Phase 1 names outside scope, [] when it passes."""
    row = check_diff_scope(root, "main", allowed)
    if row.passed:
        assert row.measured
        return []
    listed = next(d for d in row.details if d.startswith("Files outside allowed scope"))
    return [line.removeprefix("  - ") for line in listed.splitlines()[1:]]


@needs_uv
def test_the_lockfile_the_first_uv_run_writes_passes_the_in_loop_guard(tmp_path: Path) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    _uv_run(tmp_path)
    (tmp_path / "src" / "app.py").write_text("X = 2\n", encoding="utf-8")

    assert (tmp_path / "uv.lock").is_file()
    assert _in_loop(tmp_path, baseline) == (True, [])


@needs_uv
def test_the_lockfile_the_first_uv_run_writes_passes_phase_1_once_committed(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    _uv_run(tmp_path)
    (tmp_path / "src" / "app.py").write_text("X = 2\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert "uv.lock" in git.get_diff_names("main", tmp_path)
    row = check_diff_scope(tmp_path, "main", ALLOWED)
    assert row.passed, row.details
    assert row.measured
    assert row.message == "2 files, all within scope"
    assert _in_loop(tmp_path, baseline) == (True, [])


def test_a_lockfile_the_base_had_is_still_caught_when_changed_out_of_scope(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT, "uv.lock": "version = 1\n"})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    (tmp_path / "uv.lock").write_text("version = 1\n# repinned\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert _in_loop(tmp_path, baseline) == (False, ["uv.lock"])
    assert _phase_1_violations(tmp_path) == ["uv.lock"]


def test_a_lockfile_the_base_had_is_still_caught_when_deleted_out_of_scope(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT, "uv.lock": "version = 1\n"})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    git_in(tmp_path, "rm", "-q", "uv.lock")
    git_in(tmp_path, "commit", "-q", "-m", "story")

    assert _in_loop(tmp_path, baseline) == (False, ["uv.lock"])
    assert _phase_1_violations(tmp_path) == ["uv.lock"]


def test_a_lockfile_is_in_scope_when_its_manifest_is(tmp_path: Path) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT, "uv.lock": "version = 1\n"})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")
    allowed = ["src/", "pyproject.toml"]

    (tmp_path / "pyproject.toml").write_text(PYPROJECT + "# a dependency\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n# relocked\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert _in_loop(tmp_path, baseline, allowed) == (True, [])
    assert _phase_1_violations(tmp_path, allowed) == []


def test_a_lockfile_created_beside_no_manifest_is_still_caught(tmp_path: Path) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert _in_loop(tmp_path, baseline) == (False, ["docs/uv.lock"])
    assert _phase_1_violations(tmp_path) == ["docs/uv.lock"]


def test_a_lockfile_created_while_its_manifest_changed_out_of_scope_is_still_caught(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, {"pyproject.toml": PYPROJECT})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    (tmp_path / "pyproject.toml").write_text(PYPROJECT + "# a dependency\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert _in_loop(tmp_path, baseline) == (False, ["pyproject.toml", "uv.lock"])
    assert _phase_1_violations(tmp_path) == ["pyproject.toml", "uv.lock"]


def test_lockfile_scope_is_judged_from_the_merge_base_not_the_base_tip(
    tmp_path: Path,
) -> None:
    """The base branch gains its own uv.lock after this component forked,
    the way a sibling's merged pull request adds one. The component's diff
    is measured from the merge base, where no uv.lock existed, so the
    lockfile it created is still one it was entitled to create (#435)."""
    _seed(tmp_path, {"pyproject.toml": PYPROJECT})
    git_in(tmp_path, "checkout", "-q", "main")
    (tmp_path / "uv.lock").write_text("version = 1\n# sibling\n", encoding="utf-8")
    _commit_all(tmp_path)
    git_in(tmp_path, "checkout", "-q", "kstrl/factory/comp")
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    (tmp_path / "uv.lock").write_text("version = 1\n# component\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert _in_loop(tmp_path, baseline) == (True, [])
    assert _phase_1_violations(tmp_path) == []


@pytest.mark.parametrize(("lockfile", "manifest"), sorted(LOCKFILE_MANIFESTS.items()))
def test_every_named_lockfile_is_judged_through_its_own_manifest(
    tmp_path: Path, lockfile: str, manifest: str
) -> None:
    """Created beside its unchanged manifest: in scope. The same lockfile
    created beside a DIFFERENT toolchain's manifest: still a violation, so
    a wrong row in the table cannot pass."""
    other = "Gemfile" if manifest != "Gemfile" else "go.mod"
    _seed(tmp_path, {f"pkg/{manifest}": "manifest\n", f"other/{other}": "manifest\n"})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")

    (tmp_path / "pkg" / lockfile).write_text("lock\n", encoding="utf-8")
    (tmp_path / "other" / lockfile).write_text("lock\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert _in_loop(tmp_path, baseline) == (False, [f"other/{lockfile}"])
    assert _phase_1_violations(tmp_path) == [f"other/{lockfile}"]


def test_an_unreadable_base_tree_fails_both_layers_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockfile rule CLEARS violations, so when it cannot read the base
    it must not clear, and the failure must name the cause rather than
    report the lockfile as an ordinary scope violation."""
    _seed(tmp_path, {"pyproject.toml": PYPROJECT})
    baseline = git.capture_workspace_baseline(tmp_path, base_ref="main")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit_all(tmp_path)

    def _unreadable(sha: str, cwd: Path | None, timeout: float = 0) -> frozenset[str]:
        raise git.GitDiffError(f"git ls-tree {sha} exited 128: planted")

    monkeypatch.setattr(git, "tracked_files_at", _unreadable)

    assert _in_loop(tmp_path, baseline) == (False, [])
    row = check_diff_scope(tmp_path, "main", ALLOWED)
    assert not row.passed
    assert not row.measured
    assert "could not read the diff" in row.message
    assert any("planted" in d for d in row.details)
    assert [f.category for f in row.findings] == ["infrastructure_error"]
