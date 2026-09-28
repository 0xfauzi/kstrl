"""The hooks in scripts/precommit/, run the way pre-commit runs them, in an agent's shell (#678).

Every agent shell in this factory exports FORCE_COLOR=3, and ruff colours its
text output under it even into a pipe. ``cyclomatic_ratchet.py`` parsed that
text with a regex, so under FORCE_COLOR the census came back empty and a real
regression exited 0. Each test here runs a hook script as a subprocess, with
FORCE_COLOR=3 exported, in a real git repository holding a planted
regression, and reads the exit code and the lines a committer would see.

The refusal cases replace the tool with a stub on PATH. A census the hook
cannot read must exit 2, never 0 and never 1: 1 is the regression answer, and
a refusal that reads as a regression hides which of the two happened.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.gitrepo import git_in, set_identity

HOOKS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "precommit"

#: Bounds one hook run. The cyclomatic ratchet runs ``uvx ruff@0.16.4``
#: twice, and a cold uv cache fetches ruff first.
HOOK_TIMEOUT_SECONDS = 180


def _branchy(branches: int) -> str:
    """A module whose one function ``f`` has ruff cyclomatic ``branches + 1``."""
    body = [f"    if x == {i}:\n        return {i}\n" for i in range(branches)]
    return "def f(x):\n" + "".join(body) + "    return -1\n"


def _repo(tmp_path: Path, committed: dict[str, str], staged: dict[str, str]) -> Path:
    """A repository with ``committed`` at HEAD and ``staged`` in the index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    set_identity(repo)
    for name, text in committed.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "base")
    for name, text in staged.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git_in(repo, "add", "-A")
    return repo


def _run_hook(
    script: str, cwd: Path, args: list[str], env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one hook script as pre-commit does, with FORCE_COLOR=3 exported.

    ``-S`` keeps site-packages off the path: pre-commit runs these scripts
    under a bare ``python3``, so an import outside the stdlib must fail here too.
    """
    env = {**os.environ, "FORCE_COLOR": "3", **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, "-S", str(HOOKS_DIR / script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=HOOK_TIMEOUT_SECONDS,
        check=False,
    )


#: One planted regression per hook script: (committed, staged, argv, the line
#: a committer must see). Keyed by script name so the census test below can
#: hold it against the directory.
REGRESSIONS: dict[str, tuple[dict[str, str], dict[str, str], list[str], str]] = {
    "cyclomatic_ratchet.py": (
        {"m.py": _branchy(10)},
        {"m.py": _branchy(11)},
        ["m.py"],
        "REGRESSED  m.py::f  cyclomatic 11 -> 12  (limit 10)",
    ),
    "file_length_ratchet.py": (
        {"big.py": "x = 0\n" * 800},
        {"big.py": "x = 0\n" * 801},
        ["big.py"],
        "CROSSED  big.py  800 -> 801 lines  (limit 800)",
    ),
    "check_no_mocks.py": (
        {"prod.py": "X = 1\n"},
        {"prod.py": "from unittest import mock\n"},
        ["prod.py"],
        "prod.py:1: imports mock from unittest",
    ),
    "check_agent_docs_sync.py": (
        {"AGENTS.md": "# One\n", "CLAUDE.md": "# One\n"},
        {"AGENTS.md": "# One\n## Two\n"},
        [],
        "section '## Two' is in AGENTS.md but not CLAUDE.md",
    ),
}


def test_every_hook_script_has_a_planted_regression() -> None:
    """A new script in scripts/precommit/ must add its row to REGRESSIONS."""
    assert sorted(p.name for p in HOOKS_DIR.glob("*.py")) == sorted(REGRESSIONS)


@pytest.mark.parametrize("script", sorted(REGRESSIONS))
def test_hook_fails_its_regression_under_force_color(tmp_path: Path, script: str) -> None:
    committed, staged, args, expected = REGRESSIONS[script]
    repo = _repo(tmp_path, committed, staged)

    result = _run_hook(script, repo, args)

    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stdout, result.stdout


def test_cyclomatic_ratchet_fails_a_new_file_whose_head_census_is_empty(tmp_path: Path) -> None:
    """Every staged path is new, so HEAD holds no function: that is not a refusal."""
    repo = _repo(tmp_path, {"notes.txt": "x\n"}, {"new.py": _branchy(11)})

    result = _run_hook("cyclomatic_ratchet.py", repo, ["new.py"])

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NEW        new.py::f  cyclomatic 12  (limit 10)" in result.stdout, result.stdout


def test_cyclomatic_ratchet_names_a_regression_by_its_path_in_the_repo(tmp_path: Path) -> None:
    """Two staged files share a basename and a function: each keeps its own path."""
    repo = _repo(
        tmp_path,
        {"a/m.py": _branchy(4), "b/m.py": _branchy(19)},
        {"a/m.py": _branchy(14)},
    )

    result = _run_hook("cyclomatic_ratchet.py", repo, ["a/m.py", "b/m.py"])

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REGRESSED  a/m.py::f  cyclomatic 5 -> 15  (limit 10)" in result.stdout, result.stdout


#: Stub ``uvx`` bodies, and the phrase the refusal must name for each.
RUFF_STUBS: dict[str, tuple[str, str]] = {
    "prints nothing": ("exit 0\n", "is not the JSON list this hook parses"),
    # json.loads raises RecursionError here, which is not a ValueError.
    "prints JSON nested too deep to parse": (
        "printf '%100000s' '' | tr ' ' '['\n",
        "is not the JSON list this hook parses",
    ),
    # json.loads raises a plain ValueError here, which is not a JSONDecodeError.
    "prints an integer too long to parse": (
        "printf '%5000s' '' | tr ' ' '1'\n",
        "is not the JSON list this hook parses",
    ),
    "prints an empty report": ("echo '[]'\n", "the census is empty"),
    "prints an unreadable C901 entry": (
        """echo '[{"code": "C901", "message": "garbled", "filename": "m.py"}]'\n""",
        "ruff entry 0 is a C901 report this hook cannot read",
    ),
}


@pytest.mark.parametrize("stub", sorted(RUFF_STUBS))
def test_cyclomatic_ratchet_refuses_a_census_it_cannot_read(tmp_path: Path, stub: str) -> None:
    body, phrase = RUFF_STUBS[stub]
    committed, staged, args, _ = REGRESSIONS["cyclomatic_ratchet.py"]
    repo = _repo(tmp_path, committed, staged)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    uvx.chmod(0o755)

    result = _run_hook(
        "cyclomatic_ratchet.py",
        repo,
        args,
        {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Cyclomatic complexity ratchet refused:" in result.stdout, result.stdout
    assert phrase in result.stdout, result.stdout


def test_hooks_refuse_when_their_tool_is_missing(tmp_path: Path) -> None:
    """No uvx for the ratchet, no git for the docs sync: exit 2, never a pass."""
    committed, staged, args, _ = REGRESSIONS["cyclomatic_ratchet.py"]
    repo = _repo(tmp_path, {**committed, "AGENTS.md": "# One\n"}, staged)
    git = shutil.which("git")
    assert git is not None
    git_only = tmp_path / "git-only"
    git_only.mkdir()
    (git_only / "git").symlink_to(git)
    # A uvx that is there but cannot be executed: PermissionError, not
    # FileNotFoundError, so the spawn's handler must be OSError.
    no_exec = tmp_path / "no-exec"
    no_exec.mkdir()
    (no_exec / "git").symlink_to(git)
    (no_exec / "uvx").write_text("#!/bin/sh\necho '[]'\n", encoding="utf-8")
    (no_exec / "uvx").chmod(0o644)
    empty = tmp_path / "empty"
    empty.mkdir()
    # git runs but fails: not a repository (CalledProcessError, not OSError).
    loose = tmp_path / "not-a-repo"
    loose.mkdir()
    (loose / "AGENTS.md").write_text("# One\n", encoding="utf-8")

    runs = {
        "ratchet, no uvx": _run_hook("cyclomatic_ratchet.py", repo, args, {"PATH": str(git_only)}),
        "ratchet, uvx not executable": _run_hook(
            "cyclomatic_ratchet.py", repo, args, {"PATH": str(no_exec)}
        ),
        "docs, no git": _run_hook("check_agent_docs_sync.py", repo, [], {"PATH": str(empty)}),
        "docs, not a repository": _run_hook(
            "check_agent_docs_sync.py", loose, [], {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
        ),
    }

    for label, result in runs.items():
        assert result.returncode == 2, (label, result.stdout + result.stderr)
    for label in ("ratchet, no uvx", "ratchet, uvx not executable"):
        assert "refused: could not run uvx ruff@0.16.4" in runs[label].stdout, runs[label].stdout
    for label in ("docs, no git", "docs, not a repository"):
        assert "refused: git ls-files could not list the files" in runs[label].stdout, runs[
            label
        ].stdout
