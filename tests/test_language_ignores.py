"""The build output a language's verify commands write is ignored before anything spends (#459).

On a greenfield repository `ks init` runs before the build manifest exists,
so it detects no language and writes no language ignores. The #434
bootstrap then creates a Python project, and every `uv run pytest` writes
`__pycache__/` bytecode the in-loop scope guard counts as an out-of-scope
edit: the second external build lost both tier-0 components' first
attempts to it. These tests drive the real entry points as subprocesses
(`python -m kstrl`) and assert on exit codes, report rows, the agent-call
log and the files git lists.

The two uv commands of the bootstrap are simulated by writing the files
they were measured to write (uv 0.11.29 on a repository holding only a
spec: `uv init --package .` writes pyproject.toml, .python-version,
README.md and src/<name>/__init__.py and leaves .gitignore alone;
`uv add --dev pytest mypy ruff` adds the dev group and writes uv.lock),
because running uv here would need the network.
"""

from __future__ import annotations

import importlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl import git
from kstrl.init_cmd import (
    _LANGUAGE_IGNORES,
    BUILD_MANIFEST_FIX,
    _detect_project_context,
    gitignore_block,
)
from kstrl.init_wizard import plan_scaffold
from kstrl.launch import DecomposeLaunch
from kstrl.tui.session import LaunchError, start_run_session
from tests.helpers.gitrepo import git_in, set_identity

#: The refusal headline the decompose and factory preflight print.
REFUSAL = "Refusing to run: git does not ignore what this project's verify commands write"

#: One manifest per key of _LANGUAGE_IGNORES, with contents
#: `_detect_project_context` reads as that language. The census test
#: below fails when a key is added without a row here.
MANIFEST_FOR: dict[str, tuple[str, str]] = {
    "Python": ("pyproject.toml", '[project]\nname = "demo"\nversion = "0.1.0"\n'),
    "Rust": ("Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n'),
    "TypeScript": ("package.json", '{"name": "demo", "devDependencies": {"typescript": "5"}}\n'),
    "JavaScript": ("package.json", '{"name": "demo"}\n'),
    "Go": ("go.mod", "module example.com/demo\n\ngo 1.22\n"),
    "Java": ("pom.xml", "<project/>\n"),
    "Kotlin": ("build.gradle.kts", "plugins {}\n"),
}


def isolated_repo(root: Path) -> Path:
    """A git repository whose ignore rules are its own .gitignore only.

    ``core.excludesFile`` points at an empty file, so a developer's or a
    runner's global excludes (which may well list ``__pycache__/``)
    cannot make a missing entry read as ignored.
    """
    root.mkdir(parents=True)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    empty = root.parent / f"{root.name}-excludes"
    empty.write_text("", encoding="utf-8")
    git_in(root, "config", "core.excludesFile", str(empty))
    return root


def greenfield(tmp_path: Path) -> Path:
    """The #459 repository: one commit holding a spec."""
    root = isolated_repo(tmp_path / "snippetvault")
    (root / "spec.md").write_text("# Spec\n\nBuild a snippet vault.\n", encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "spec")
    return root


def run_ks(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """`python -m kstrl <args>` from ``root.parent``, stdout and stderr merged.

    A `gh` that exits 1 goes first on PATH, as in
    tests/test_build_manifest_preflight.py, so doctor never reaches the
    network.
    """
    bin_dir = root.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    env = {
        **os.environ,
        "KSTRL_NO_TUI": "1",
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return subprocess.run(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
    )


def ks_init(root: Path) -> subprocess.CompletedProcess[str]:
    return run_ks(root, "init", str(root), "--ui", "plain", "--no-color")


def spec_command(root: Path, command: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run decompose or factory --spec with an agent that logs each call."""
    calls = root.parent / f"{command}-agent-calls.log"
    agent = f"echo called >> '{calls}'; cat > /dev/null; echo not-json"
    proc = run_ks(
        root,
        command,
        "--spec",
        str(root / "spec.md"),
        "--project-name",
        "demo",
        "--root",
        str(root),
        "--agent-cmd",
        agent,
        "--ui",
        "plain",
        "--no-color",
    )
    return proc, calls


def simulate_uv_bootstrap(root: Path) -> None:
    """The files `uv init --package .` and `uv add --dev pytest mypy ruff` write."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "snippetvault"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        "dependencies = []\n\n[dependency-groups]\n"
        'dev = ["mypy>=2.3.1", "pytest>=9.1.1", "ruff>=0.16.8"]\n',
        encoding="utf-8",
    )
    (root / ".python-version").write_text("3.12\n", encoding="utf-8")
    (root / "README.md").write_text("", encoding="utf-8")
    package = root / "src" / "snippetvault"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")


def bootstrapped_without_reinit(tmp_path: Path) -> Path:
    """Greenfield, `ks init`, then the bootstrap as #434 printed it before #459."""
    root = greenfield(tmp_path)
    assert ks_init(root).returncode == 0
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "ks init")
    simulate_uv_bootstrap(root)
    git_in(root, "add", "pyproject.toml", "uv.lock", ".python-version", "README.md", "src")
    git_in(root, "commit", "-q", "-m", "Add the build manifest")
    return root


def gitignore_row(root: Path) -> tuple[str, str]:
    """`ks doctor`'s `gitignore` row for ``root``, and the whole report."""
    report = run_ks(root, "doctor", "--root", str(root))
    rows = [line for line in report.stdout.splitlines() if "] gitignore: " in line]
    assert len(rows) == 1, report.stdout
    return rows[0], report.stdout


def write_bytecode(root: Path) -> None:
    """What `uv run pytest` leaves behind, at the paths the #459 build reported."""
    for rel in (
        "src/snippetvault/__pycache__/__init__.cpython-312.pyc",
        "tests/__pycache__/test_snippets.cpython-312-pytest-9.1.1.pyc",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00")


def test_doctor_decompose_and_factory_refuse_a_python_repo_whose_bytecode_is_not_ignored(
    tmp_path: Path,
) -> None:
    root = bootstrapped_without_reinit(tmp_path)

    report = run_ks(root, "doctor", "--root", str(root))
    assert report.returncode == 2, report.stdout
    assert "ks doctor: not-ready" in report.stdout
    fail_line = next(
        line for line in report.stdout.splitlines() if line.startswith("  [fail] gitignore:")
    )
    assert "__pycache__/" in fail_line
    assert "*.py[cod]" in fail_line
    assert "Run `ks init` again" in report.stdout

    for command in ("decompose", "factory"):
        proc, calls = spec_command(root, command)
        assert proc.returncode == 2, proc.stdout
        assert REFUSAL in proc.stdout
        assert "__pycache__/" in proc.stdout
        assert not calls.exists(), calls.read_text(encoding="utf-8")


def test_rerunning_init_appends_the_missing_ignores_once_and_clears_every_refusal(
    tmp_path: Path,
) -> None:
    root = bootstrapped_without_reinit(tmp_path)

    first = ks_init(root)
    assert first.returncode == 0, first.stdout
    assert "Appended the Python ignores" in first.stdout
    text = (root / ".gitignore").read_text(encoding="utf-8")
    lines = text.splitlines()
    for entry in _LANGUAGE_IGNORES["Python"]:
        assert entry in lines, entry
    # Only the missing entries are appended, not a second kstrl block.
    assert lines.count(".kstrl/") == 1, text

    second = ks_init(root)
    assert second.returncode == 0, second.stdout
    assert (root / ".gitignore").read_text(encoding="utf-8") == text
    assert "already has the kstrl block" in second.stdout

    git_in(root, "add", ".gitignore")
    git_in(root, "commit", "-q", "-m", "Ignore the build output")
    write_bytecode(root)
    # The scope guard's own listing of what counts against a component.
    assert git.get_untracked_files(root) == set()

    report = run_ks(root, "doctor", "--root", str(root))
    assert "[ok] gitignore:" in report.stdout, report.stdout
    proc, calls = spec_command(root, "decompose")
    assert REFUSAL not in proc.stdout
    assert calls.exists(), proc.stdout


def test_the_printed_bootstrap_sequence_commits_the_python_ignores(tmp_path: Path) -> None:
    """Run the #434 sequence as `ks init` prints it, with the two uv
    commands simulated and every other command run verbatim."""
    root = greenfield(tmp_path)
    assert ks_init(root).returncode == 0

    commands = re.findall(r"`([^`]+)`", BUILD_MANIFEST_FIX)
    assert "ks init" in commands, commands
    uv_init = commands.index("uv init --package .")
    uv_add = commands.index("uv add --dev pytest mypy ruff")
    reinit = commands.index("ks init")
    git_add = next(i for i, c in enumerate(commands) if c.startswith("git add "))
    git_commit = next(i for i, c in enumerate(commands) if c.startswith("git commit "))
    assert uv_init < uv_add < reinit < git_add < git_commit, commands

    simulate_uv_bootstrap(root)
    assert ks_init(root).returncode == 0
    git_in(root, *shlex.split(commands[git_add])[1:])
    git_in(root, *shlex.split(commands[git_commit])[1:])

    committed = subprocess.run(
        ["git", "show", "HEAD:.gitignore"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert committed.returncode == 0, committed.stderr
    for entry in _LANGUAGE_IGNORES["Python"]:
        assert entry in committed.stdout.splitlines(), entry


def test_the_home_shell_decompose_launch_refuses_over_missing_ignores(tmp_path: Path) -> None:
    root = bootstrapped_without_reinit(tmp_path)
    (root / "kstrl.toml").write_text('[agent]\ncommand = "fake-agent"\n', encoding="utf-8")

    # The launch's agent preflight is the first thing to import kstrl.cli,
    # which binds get_agent at module level. Imported here first, so the
    # patch below cannot leak into kstrl.cli for every later test.
    importlib.import_module("kstrl.cli")
    # A get_agent that raises, so a launch that is NOT refused stops here
    # instead of starting a decompose thread.
    with (
        patch("kstrl.agents.get_agent", side_effect=RuntimeError("reached get_agent")) as get_agent,
        pytest.raises(LaunchError) as raised,
    ):
        start_run_session(DecomposeLaunch(spec_path=root / "spec.md", project_name="demo"), root)

    assert "git does not ignore" in str(raised.value)
    assert "__pycache__/" in str(raised.value)
    assert get_agent.call_count == 0


def test_the_census_covers_every_language_the_ignore_table_names() -> None:
    """A language added to _LANGUAGE_IGNORES without a manifest here is
    a language the check below never runs against."""
    assert set(MANIFEST_FOR) == set(_LANGUAGE_IGNORES)


@pytest.mark.parametrize("language", sorted(_LANGUAGE_IGNORES))
def test_the_doctor_check_names_every_entry_of_every_language(
    tmp_path: Path, language: str
) -> None:
    name, body = MANIFEST_FOR[language]
    root = isolated_repo(tmp_path / "repo")
    (root / name).write_text(body, encoding="utf-8")
    assert _detect_project_context(root)["language"] == language
    gitignore = root / ".gitignore"

    gitignore.write_text(".kstrl/\n", encoding="utf-8")
    row, report = gitignore_row(root)
    assert row.startswith("  [fail] gitignore: "), report
    for entry in _LANGUAGE_IGNORES[language]:
        assert entry in row, entry
    assert "Run `ks init` again" in report

    gitignore.write_text(gitignore_block(language), encoding="utf-8")
    row, report = gitignore_row(root)
    assert row.startswith("  [ok] gitignore: "), report

    from kstrl.init_cmd import missing_language_ignores

    block = gitignore_block(language).splitlines()
    for entry in _LANGUAGE_IGNORES[language]:
        gitignore.write_text(
            "".join(f"{line}\n" for line in block if line != entry), encoding="utf-8"
        )
        assert missing_language_ignores(root, language) == (entry,), entry


def test_git_decides_what_is_ignored_and_a_rerun_of_init_clears_a_negation(
    tmp_path: Path,
) -> None:
    """The scope guard asks git, so the check must too: a rule in
    .git/info/exclude counts, and a later `!` rule in .gitignore undoes
    one. A check that read the .gitignore text would get both wrong."""
    root = bootstrapped_without_reinit(tmp_path)
    exclude = root / ".git" / "info" / "exclude"
    python_lines = "".join(f"{entry}\n" for entry in _LANGUAGE_IGNORES["Python"])

    exclude.write_text(python_lines, encoding="utf-8")
    row, report = gitignore_row(root)
    assert row.startswith("  [ok] gitignore: "), report
    before = (root / ".gitignore").read_text(encoding="utf-8")
    rerun = ks_init(root)
    assert "already has the kstrl block" in rerun.stdout, rerun.stdout
    assert (root / ".gitignore").read_text(encoding="utf-8") == before

    exclude.write_text("", encoding="utf-8")
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(python_lines + "!__pycache__/\n")
    row, report = gitignore_row(root)
    assert row.startswith("  [fail] gitignore: "), report
    assert "__pycache__/" in row
    assert "*.py[cod]" not in row

    assert "Appended the Python ignores" in ks_init(root).stdout
    row, report = gitignore_row(root)
    assert row.startswith("  [ok] gitignore: "), report


def test_a_root_anchored_rule_does_not_count_as_ignoring_nested_bytecode(
    tmp_path: Path,
) -> None:
    """The #459 files were nested (src/<pkg>/__pycache__/). A rule that
    only ignores the root directory leaves them counted."""
    from kstrl.init_cmd import missing_language_ignores

    root = isolated_repo(tmp_path / "repo")
    (root / "pyproject.toml").write_text(MANIFEST_FOR["Python"][1], encoding="utf-8")
    block = gitignore_block("Python").replace("__pycache__/\n", "/__pycache__/\n")
    (root / ".gitignore").write_text(block, encoding="utf-8")

    assert missing_language_ignores(root, "Python") == ("__pycache__/",)


def test_git_that_cannot_answer_is_not_read_as_nothing_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kstrl.init_cmd import missing_language_ignores

    # Git stops its repository search here, so "not a repository" holds
    # even if the temporary directory sits inside a checkout.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text(MANIFEST_FOR["Python"][1], encoding="utf-8")

    assert missing_language_ignores(plain, "Python") is None


def test_the_wizard_preview_matches_what_a_rerun_of_init_writes(tmp_path: Path) -> None:
    """The TUI preview and the write share one decision (#201): once the
    language is known and its ignores are missing, the preview says
    append, and after the append it says keep."""
    root = greenfield(tmp_path)
    assert ks_init(root).returncode == 0
    simulate_uv_bootstrap(root)

    planned = {entry.path.name: entry.action for entry in plan_scaffold(root)}
    assert planned[".gitignore"] == "append"

    assert ks_init(root).returncode == 0
    planned = {entry.path.name: entry.action for entry in plan_scaffold(root)}
    assert planned[".gitignore"] == "keep"


def test_a_git_that_cannot_start_is_not_read_as_nothing_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No git on PATH raises OSError inside ignored_paths; that is None too."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    assert git.ignored_paths(["x"], tmp_path) is None
