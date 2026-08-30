"""Direct tests for ``run_init`` - the scaffold `ks init` writes.

Nothing exercised ``run_init`` itself before this file: the wizard tests
cover ``plan_scaffold`` and the agent-settings write, and the TUI screen
test patches ``run_init`` out. Both issues fixed here (#201 .gitignore,
#256 next steps) are properties of what init WRITES and PRINTS, so they
are tested against the real function with a real UI.
"""

from __future__ import annotations

import inspect
import io
import re
from pathlib import Path

import pytest

from kstrl.cli import cli
from kstrl.init_cmd import (
    _LANGUAGE_IGNORES,
    GITIGNORE_BLOCK_MARKER,
    NEXT_STEPS,
    _detect_project_context,
    run_init,
)
from kstrl.init_wizard import plan_scaffold
from kstrl.ui.plain import PlainUI
from tests.spine_utils import git


def run_init_capturing(root: Path) -> tuple[int, str]:
    """Run init against ``root``, returning (exit code, printed output)."""
    buffer = io.StringIO()
    code = run_init(root, PlainUI(no_color=True, file=buffer))
    return code, buffer.getvalue()


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """A git repo with one commit and a uv-style Python project in it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    git("add", "pyproject.toml", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    return repo


class TestGitignoreScaffold:
    def test_python_project_gets_build_artifacts_and_kstrl_state(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        content = (tmp_path / ".gitignore").read_text()
        for entry in (
            "__pycache__/",
            "*.py[cod]",
            ".venv/",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            "dist/",
            ".kstrl/",
        ):
            assert entry in content, entry

    def test_lockfile_is_never_ignored(self, tmp_path: Path) -> None:
        """#201: uv.lock belongs in version control, not in .gitignore."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        run_init_capturing(tmp_path)

        assert "uv.lock" not in (tmp_path / ".gitignore").read_text()

    def test_block_follows_the_detected_language(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "demo"}')

        run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert "__pycache__/" not in content

    def test_unknown_language_still_ignores_kstrl_runtime_state(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert ".kstrl/" in content
        assert "__pycache__/" not in content

    def test_rerun_does_not_duplicate_the_block(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        run_init_capturing(tmp_path)
        after_first = (tmp_path / ".gitignore").read_text()
        _, output = run_init_capturing(tmp_path)

        assert (tmp_path / ".gitignore").read_text() == after_first
        assert after_first.count(GITIGNORE_BLOCK_MARKER) == 1
        assert "already has the kstrl block" in output

    def test_existing_gitignore_is_appended_to_not_rewritten(self, tmp_path: Path) -> None:
        existing = "# mine\nsecrets.env\ndist/\n"
        (tmp_path / ".gitignore").write_text(existing)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        _, output = run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert content.startswith(existing)
        assert GITIGNORE_BLOCK_MARKER in content
        assert ".kstrl/" in content
        assert "Appended the kstrl block" in output

    def test_append_separates_from_a_file_with_no_trailing_newline(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env")

        run_init_capturing(tmp_path)

        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert lines[0] == "secrets.env"
        assert GITIGNORE_BLOCK_MARKER in lines

    def test_empty_gitignore_gains_the_block_without_leading_blanks(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("")

        run_init_capturing(tmp_path)

        assert (tmp_path / ".gitignore").read_text().startswith(GITIGNORE_BLOCK_MARKER)

    def test_user_edits_below_the_block_survive_a_rerun(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        path = tmp_path / ".gitignore"
        path.write_text(path.read_text() + "\n# added later\nmy-scratch/\n")
        before = path.read_text()

        run_init_capturing(tmp_path)

        assert path.read_text() == before


class TestLockfileTracking:
    def test_untracked_lockfile_is_staged_and_reported(self, python_repo: Path) -> None:
        (python_repo / "uv.lock").write_text("version = 1\n")
        commits_before = git("rev-list", "--count", "HEAD", cwd=python_repo)

        _, output = run_init_capturing(python_repo)

        untracked = git("ls-files", "--others", "--exclude-standard", cwd=python_repo)
        assert git("ls-files", "--", "uv.lock", cwd=python_repo) == "uv.lock"
        assert "uv.lock" not in untracked.split()
        assert "Staged uv.lock" in output
        assert "no commit was created" in output
        assert git("rev-list", "--count", "HEAD", cwd=python_repo) == commits_before

    def test_tracked_lockfile_is_left_alone(self, python_repo: Path) -> None:
        (python_repo / "uv.lock").write_text("version = 1\n")
        git("add", "uv.lock", cwd=python_repo)
        git("commit", "-qm", "lock", cwd=python_repo)

        _, output = run_init_capturing(python_repo)

        assert "uv.lock is tracked" in output
        assert "Staged uv.lock" not in output

    def test_missing_lockfile_is_a_warning_with_the_fix(self, python_repo: Path) -> None:
        _, output = run_init_capturing(python_repo)

        assert "No uv.lock yet" in output
        assert "uv lock" in output

    def test_non_git_directory_skips_the_lockfile_step(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "uv.lock").write_text("version = 1\n")

        _, output = run_init_capturing(tmp_path)

        assert "uv.lock" not in output

    def test_non_python_repo_skips_the_lockfile_step(self, python_repo: Path) -> None:
        (python_repo / "pyproject.toml").unlink()

        _, output = run_init_capturing(python_repo)

        assert "uv.lock" not in output


class TestNextSteps:
    def test_leads_with_the_spec_workflow(self, tmp_path: Path) -> None:
        """#256: the two commands implementing the README headline."""
        _, output = run_init_capturing(tmp_path)

        assert "ks decompose --spec" in output
        assert "ks factory --spec" in output
        spec_at = output.index("ks decompose --spec")
        assert spec_at < output.index("ks run [iterations]")

    def test_single_component_path_is_labelled_as_such(self, tmp_path: Path) -> None:
        _, output = run_init_capturing(tmp_path)

        assert "ks run [iterations]" in output
        assert "no PR" in output

    def test_names_the_free_measurement(self, tmp_path: Path) -> None:
        _, output = run_init_capturing(tmp_path)

        assert "ks sense" in output
        assert "ks understand [iterations]" in output
        assert "ks feature [iterations]" in output


class TestScaffoldContract:
    def test_plan_scaffold_lists_exactly_what_run_init_writes(self, tmp_path: Path) -> None:
        """The wizard preview and the write cannot drift apart silently."""
        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        written = {p for p in tmp_path.rglob("*") if p.is_file()}
        assert written == {entry.path for entry in plan_scaffold(tmp_path)}

    def test_plan_stops_calling_gitignore_an_append_once_init_ran(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env\n")

        run_init_capturing(tmp_path)

        planned = {e.path.name: e for e in plan_scaffold(tmp_path)}
        assert planned[".gitignore"].action == "keep"

    def test_every_detected_language_has_an_ignore_block(self) -> None:
        """A language the detector can return but the ignore table cannot
        is #201 recurring in the way that is hardest to see: the scaffold
        writes a block with no build artifacts in it."""
        source = inspect.getsource(_detect_project_context)
        assigned = set(re.findall(r'ctx\["language"\] = "([^"]+)"', source))

        assert assigned, "the language-assignment shape changed; fix this test"
        assert assigned - {"unknown"} <= set(_LANGUAGE_IGNORES)

    def test_every_command_named_in_next_steps_is_real(self) -> None:
        """The block is the first thing a new user reads; a renamed
        command or flag must not leave it printing something that errors."""
        checked = 0
        for line in NEXT_STEPS.splitlines():
            match = re.search(r"\bks ([a-z]+)(.*)", line)
            if not match:
                continue
            name, rest = match.group(1), match.group(2)
            command = cli.commands.get(name)
            assert command is not None, f"`ks {name}` is not a command"
            known = {opt for param in command.params for opt in param.opts}
            assert set(re.findall(r"--[a-z-]+", rest)) <= known, line
            checked += 1

        assert checked >= 5


class TestExitCodes:
    def test_missing_directory_returns_2(self, tmp_path: Path) -> None:
        code, output = run_init_capturing(tmp_path / "nope")

        assert code == 2
        assert "Directory not found" in output

    def test_unparseable_prd_returns_1(self, tmp_path: Path) -> None:
        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prd.json").write_text("{not json")

        code, output = run_init_capturing(tmp_path)

        assert code == 1
        assert "Invalid JSON" in output
