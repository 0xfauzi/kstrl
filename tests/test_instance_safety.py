"""R0.5: instance and state safety (H-7, H-8, H-15).

- Run-level flock on ``.ralph/factory.lock``: a second invocation on the
  same root refuses to start while the first holds the lock (real
  two-process contention), with ``--force-lock`` as the explicit
  override.
- Manifest save path == manifest load path: a custom ``--manifest``
  round-trips to the custom file, and ``ralph run`` persists to its own
  ``run-manifest.json`` instead of clobbering a factory's resumable
  ``manifest.json``.
- ``single_pr=True`` forces ``max_parallel=1`` with a printed notice
  (all components share one branch; parallel worktrees hard-fail).
- Stale branches from previous runs are deleted when fully merged and
  refused (naming the branch) otherwise - never silently reused.
- Stale worktrees from crashed runs (run-scoped and pre-R0.5 flat
  layout) are pruned at start when the run lock is genuinely held.

Real git repos and real fake-agent subprocesses; no LLM involved.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, FactoryResult, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

COMPLETE_LINE = "echo '<promise>COMPLETE</promise>'"

# Child process that takes the run-level flock and holds it until killed,
# standing in for a live first factory invocation.
_LOCK_HOLDER_SCRIPT = """
import fcntl, sys, time
fp = open(sys.argv[1], "a+")
try:
    fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("FAIL", flush=True)
    sys.exit(1)
print("locked", flush=True)
time.sleep(60)
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30,
    )


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        text=True, timeout=30,
    ).stdout.strip()


def _init_repo(root: Path, comp_ids: tuple[str, ...] = ("comp-a",)) -> None:
    """Real git repo shaped like a ralph project (scripts/ralph/ is
    gitignored, so provisioning must copy prompt + PRD into worktrees)."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / ".gitignore").write_text("scripts/ralph/\n")
    (root / "README.md").write_text("seed\n")
    _git("add", ".gitignore", "README.md", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)

    ralph_dir = root / "scripts" / "ralph"
    (ralph_dir / "feature").mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text(
        "Read the PRD at $prd_path and implement one story.\n"
    )
    for comp_id in comp_ids:
        feature_dir = ralph_dir / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        prd: dict[str, object] = {
            "branchName": f"ralph/factory/{comp_id}",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }
        (feature_dir / "prd.json").write_text(json.dumps(prd))


def _component(comp_id: str, branch: str | None = None) -> Component:
    return Component(
        id=comp_id, title=comp_id.upper(), description="", dependencies=[],
        prd_path=f"scripts/ralph/feature/{comp_id}/prd.json",
        branch_name=branch or f"ralph/factory/{comp_id}",
    )


def _manifest(
    components: list[Component] | None = None, single_pr: bool = False,
) -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="t",
        base_branch="main", single_pr=single_pr,
        components=components if components is not None
        else [_component("comp-a")],
    )


def _factory_config(**overrides: Any) -> FactoryConfig:
    kwargs: dict[str, Any] = dict(
        use_worktrees=True, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_bad_patterns=False,
            subprocess_timeout=30.0,
        ),
    )
    kwargs.update(overrides)
    return FactoryConfig(**kwargs)


def _base_config(root: Path, agent_cmd: str = COMPLETE_LINE) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0, agent_cmd=agent_cmd,
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _run(
    root: Path,
    manifest: Manifest | None = None,
    config: FactoryConfig | None = None,
    manifest_path: Path | None = None,
    agent_cmd: str = COMPLETE_LINE,
) -> FactoryResult:
    return run_factory(
        manifest if manifest is not None else _manifest(),
        config if config is not None else _factory_config(),
        _base_config(root, agent_cmd),
        PlainUI(no_color=True),
        root,
        manifest_path=manifest_path,
    )


class _HeldLock:
    """Context manager: a real second process holding .ralph/factory.lock."""

    def __init__(self, root: Path) -> None:
        self.lock_path = root / ".ralph" / "factory.lock"

    def __enter__(self) -> _HeldLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(self.lock_path)],
            stdout=subprocess.PIPE, text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        assert line == "locked", f"lock-holder child failed: {line!r}"
        return self

    def __exit__(self, *exc: object) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.mark.skipif(
    sys.platform == "win32", reason="flock is POSIX-only (documented degrade)",
)
class TestRunLockContention:
    def test_second_invocation_refused_while_lock_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """While a real second process holds the flock, run_factory
        refuses to start: exit code 2, nothing scheduled, no state
        written."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        manifest = _manifest()
        with _HeldLock(root):
            result = _run(root, manifest=manifest)

        assert result.exit_code == 2
        assert result.completed == []
        # Nothing was scheduled or persisted.
        assert manifest.components[0].status == "pending"
        assert not (root / "scripts" / "ralph" / "manifest.json").exists()
        assert not (root / ".ralph" / "worktrees").exists()
        text = capsys.readouterr().err
        assert "factory.lock" in text
        assert "--force-lock" in text

    def test_force_lock_overrides_held_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        with _HeldLock(root):
            result = _run(root, config=_factory_config(force_lock=True))

        assert result.exit_code == 0
        assert result.completed == ["comp-a"]
        text = capsys.readouterr().err
        assert "--force-lock" in text

    def test_lock_released_after_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A completed run releases the flock so the next invocation can
        acquire it."""
        import fcntl

        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        result = _run(root)
        assert result.completed == ["comp-a"]

        with open(root / ".ralph" / "factory.lock", "a+") as fp:
            # Raises BlockingIOError if the run leaked its lock.
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


class TestManifestPathFidelity:
    def test_custom_manifest_path_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """State saves to the same custom path the manifest was loaded
        from, not the hardcoded scripts/ralph/manifest.json (H-15)."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        custom = tmp_path / "custom-manifest.json"
        _manifest().save(custom)
        manifest = Manifest.load(custom)

        result = _run(root, manifest=manifest, manifest_path=custom)

        assert result.completed == ["comp-a"]
        saved = json.loads(custom.read_text())
        assert saved["components"][0]["status"] == "completed"
        assert not (root / "scripts" / "ralph" / "manifest.json").exists(), (
            "custom-manifest run leaked state into the default path"
        )

    def test_default_manifest_path_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        result = _run(root)

        assert result.completed == ["comp-a"]
        default_path = root / "scripts" / "ralph" / "manifest.json"
        saved = json.loads(default_path.read_text())
        assert saved["components"][0]["status"] == "completed"

    def test_cli_run_uses_run_manifest_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`ralph run` wires its own run-manifest.json into run_factory
        so it cannot clobber a factory's resumable manifest.json."""
        project = tmp_path / "project"
        ralph_dir = project / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "prompt.md").write_text("test prompt")
        (ralph_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        captured: dict[str, Any] = {}

        def fake_run_factory(*args: Any, **kwargs: Any) -> FactoryResult:
            captured["manifest_path"] = kwargs.get("manifest_path")
            captured["factory_config"] = args[1]
            return FactoryResult()

        monkeypatch.setattr("kstrl.cli.run_factory", fake_run_factory)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "run", "0",
                "--agent-cmd", COMPLETE_LINE,
                "--sleep", "0",
                "--no-verify",
                "--force-lock",
            ],
            env={
                "PROMPT_FILE": str(ralph_dir / "prompt.md"),
                "PRD_FILE": str(ralph_dir / "prd.json"),
            },
        )

        assert result.exit_code == 0, result.output
        assert captured["manifest_path"] == (
            ralph_dir / "run-manifest.json"
        )
        assert captured["factory_config"].force_lock is True

    def test_cli_factory_passes_custom_manifest_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`ralph factory --manifest /x.json` hands that exact path to
        run_factory as the save path."""
        root = tmp_path / "repo"
        _init_repo(root)
        custom = tmp_path / "custom-manifest.json"
        _manifest().save(custom)

        captured: dict[str, Any] = {}

        def fake_run_factory(*args: Any, **kwargs: Any) -> FactoryResult:
            captured["manifest_path"] = kwargs.get("manifest_path")
            captured["factory_config"] = args[1]
            return FactoryResult()

        monkeypatch.setattr("kstrl.cli.run_factory", fake_run_factory)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "factory",
                "--manifest", str(custom),
                "--root", str(root),
                "--agent-cmd", COMPLETE_LINE,
                "--yes",
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["manifest_path"] == custom
        assert captured["factory_config"].force_lock is False


class TestSinglePrParallelism:
    def test_single_pr_forces_sequential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """single_pr with max_parallel>1 downgrades to sequential with a
        notice, and same-tier components complete instead of hard-failing
        on 'branch already checked out' (H-8)."""
        root = tmp_path / "repo"
        _init_repo(root, comp_ids=("comp-a", "comp-b"))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        shared_branch = "ralph/factory/shared"
        manifest = _manifest(
            components=[
                _component("comp-a", branch=shared_branch),
                _component("comp-b", branch=shared_branch),
            ],
            single_pr=True,
        )
        config = _factory_config(single_pr=True, max_parallel=4)

        result = _run(root, manifest=manifest, config=config)

        assert result.exit_code == 0
        assert sorted(result.completed) == ["comp-a", "comp-b"]
        text = capsys.readouterr().err
        assert "forcing max_parallel=1" in text


class TestStaleBranchPolicy:
    def test_unmerged_stale_branch_refuses_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A leftover component branch with commits not merged into base
        refuses the run with an error naming the branch (H-7): never
        silently reused."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        # Aborted-run leftover: branch with a commit main does not have.
        _git("branch", "ralph/factory/comp-a", cwd=root)
        _git("checkout", "-q", "ralph/factory/comp-a", cwd=root)
        (root / "stale.txt").write_text("from an aborted run\n")
        _git("add", "stale.txt", cwd=root)
        _git("commit", "-q", "-m", "stale work", cwd=root)
        _git("checkout", "-q", "main", cwd=root)
        stale_tip = _git_out("rev-parse", "ralph/factory/comp-a", cwd=root)

        manifest = _manifest()
        result = _run(root, manifest=manifest)

        assert result.exit_code == 2
        assert result.completed == []
        assert manifest.components[0].status == "pending"
        # The branch is untouched, not deleted and not built upon.
        assert _git_out(
            "rev-parse", "ralph/factory/comp-a", cwd=root,
        ) == stale_tip
        text = capsys.readouterr().err
        assert "ralph/factory/comp-a" in text
        assert "not merged" in text

    def test_merged_stale_branch_deleted_and_run_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A leftover branch fully merged into base is deleted at
        preflight and the component is provisioned fresh from base."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        # Leftover branch pointing at main's tip: fully merged.
        _git("branch", "ralph/factory/comp-a", "main", cwd=root)

        result = _run(root)

        assert result.exit_code == 0
        assert result.completed == ["comp-a"]
        text = capsys.readouterr().err
        assert "Deleted stale branch 'ralph/factory/comp-a'" in text


class TestStaleWorktreePrune:
    def test_stale_worktrees_pruned_at_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worktrees from crashed runs - run-scoped dirs from other run
        ids AND pre-R0.5 flat-layout dirs - are removed when the run lock
        is held; the completed run leaves no worktree dirs behind."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        worktree_root = root / ".ralph" / "worktrees"
        old_run_wt = worktree_root / "20250101-000000-000000-dead" / "comp-x"
        legacy_wt = worktree_root / "legacy-comp"
        _git(
            "worktree", "add", str(old_run_wt), "-b", "tmp/old-run", "main",
            cwd=root,
        )
        _git(
            "worktree", "add", str(legacy_wt), "-b", "tmp/legacy", "main",
            cwd=root,
        )

        result = _run(root)

        assert result.completed == ["comp-a"]
        assert not old_run_wt.exists()
        assert not legacy_wt.exists()
        # The completed run's own worktree dir is gone too; only lock
        # files may remain at the top level.
        leftovers = [
            p for p in worktree_root.iterdir() if not p.name.endswith(".lock")
        ] if worktree_root.exists() else []
        assert leftovers == []
        # git's worktree bookkeeping agrees (no stale registrations).
        listed = _git_out("worktree", "list", "--porcelain", cwd=root)
        assert "worktrees/" not in listed.replace(str(root), "")
