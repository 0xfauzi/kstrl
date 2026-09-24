"""A repository with no build manifest is refused before anything spends (#434).

kstrl will not write a root build manifest: `decompose._ALLOWED_PATHS_EXCLUDE`
refuses one in every component's allowedPaths. On a repository that has
none, the architect can only halt and ask who writes it, and the operator
paid $2.66 and 411 s to find that out in the build #434 records. These
tests drive the real entry points as subprocesses (`python -m kstrl`), so
the preflight seam, the exit code and the process boundary are all in the
path, and they count architect calls with a recording agent command rather
than trusting an exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.init_cmd import _detect_project_context, gitignore_block
from kstrl.launch import DecomposeLaunch
from kstrl.tui.session import LaunchError, start_run_session
from tests.helpers.gitrepo import git_in, set_identity

#: The refusal headline the decompose and factory preflight print.
REFUSAL = "Refusing to run: this repository has no build manifest kstrl can use"

#: One manifest per ecosystem the issue names, with contents
#: `_detect_project_context` reads without error.
MANIFESTS: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
    "package.json": '{"name": "demo"}\n',
    "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
}


def greenfield(tmp_path: Path, *, extra: dict[str, str] | None = None) -> Path:
    """The #434 repository: one commit holding a spec and a stray .py file.

    `legacy/old_notes.py` is there because the repository in the issue had
    one: a Python source file is not a build manifest, and the refusal
    must not read it as one.
    """
    root = tmp_path / "greenfield"
    root.mkdir()
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    (root / "spec.md").write_text("# Spec\n\nBuild a Python CLI.\n", encoding="utf-8")
    (root / "legacy").mkdir()
    (root / "legacy" / "old_notes.py").write_text("x = 1\n", encoding="utf-8")
    for name, body in (extra or {}).items():
        (root / name).write_text(body, encoding="utf-8")
    # #459: the ignores `ks init` writes for whatever language the extras
    # make this, so a manifest test is not refused over its build output.
    language = _detect_project_context(root)["language"]
    (root / ".gitignore").write_text(gitignore_block(language), encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "initial")
    return root


def run_ks(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """`python -m kstrl <args>`, stdout and stderr merged.

    Run from ``root.parent``, which holds no manifest, and never from
    ``root``: every command names ``root`` explicitly, so a check that
    read the working directory instead of ``--root`` refuses the
    repositories that have a manifest and fails the controls below.

    A `gh` that exits 1 goes first on PATH, the stub tests/test_doctor.py
    uses, so the doctor runs never reach the network: `github_cli` is a
    warning either way and no assertion here depends on it.
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


def recording_agent(root: Path) -> tuple[str, Path]:
    """An agent command that appends one line per call, and its log.

    It prints no JSON, so a decompose that reaches it fails its three
    attempts: what matters here is only whether it was called.
    """
    calls = root.parent / "agent-calls.log"
    return f"echo called >> '{calls}'; cat > /dev/null; echo not-json", calls


def spec_command(root: Path, command: str, agent: str) -> subprocess.CompletedProcess[str]:
    return run_ks(
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


@pytest.mark.parametrize("command", ["decompose", "factory"])
def test_a_repository_with_no_build_manifest_is_refused_before_any_agent_call(
    tmp_path: Path, command: str
) -> None:
    root = greenfield(tmp_path)
    agent, calls = recording_agent(root)

    proc = spec_command(root, command, agent)

    assert proc.returncode == 2, proc.stdout
    assert REFUSAL in proc.stdout
    assert "kstrl will not create the build manifest" in proc.stdout
    assert "uv init --package ." in proc.stdout
    assert not calls.exists(), calls.read_text(encoding="utf-8")
    # Refused before a run directory exists, like every other pre-spend
    # refusal: nothing under .kstrl/runs for a run that never started.
    assert not list((root / ".kstrl" / "runs").glob("*"))


@pytest.mark.parametrize("manifest", sorted(MANIFESTS))
def test_a_repository_with_its_own_manifest_reaches_the_architect(
    tmp_path: Path, manifest: str
) -> None:
    root = greenfield(tmp_path, extra={manifest: MANIFESTS[manifest]})
    agent, calls = recording_agent(root)

    proc = spec_command(root, "decompose", agent)

    assert REFUSAL not in proc.stdout
    assert calls.exists(), proc.stdout
    assert calls.read_text(encoding="utf-8").count("called") >= 1


def test_an_unrecognised_toolchain_reaches_the_architect_once_verify_names_it(
    tmp_path: Path,
) -> None:
    """A Gemfile is a build manifest kstrl does not recognise. With no
    [verify] command it is refused like an empty repository; with one it
    proceeds, because the operator has told kstrl how the project builds."""
    root = greenfield(tmp_path, extra={"Gemfile": 'source "https://rubygems.org"\n'})
    agent, calls = recording_agent(root)

    refused = spec_command(root, "decompose", agent)
    assert refused.returncode == 2, refused.stdout
    assert REFUSAL in refused.stdout
    assert not calls.exists()

    (root / "kstrl.toml").write_text(
        '[verify]\ntest_command = "bundle exec rspec"\n', encoding="utf-8"
    )
    proceeded = spec_command(root, "decompose", agent)
    assert REFUSAL not in proceeded.stdout
    assert calls.exists(), proceeded.stdout


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_an_empty_verify_command_does_not_satisfy_the_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Phase 1 resolves an empty command to the uv default, which needs the
    pyproject.toml this repository lacks, so it names no toolchain."""
    root = greenfield(tmp_path)
    monkeypatch.setenv("KSTRL_VERIFY_TEST_CMD", value)
    agent, calls = recording_agent(root)
    proc = spec_command(root, "decompose", agent)
    assert proc.returncode == 2, proc.stdout
    assert REFUSAL in proc.stdout
    assert not calls.exists()


@pytest.mark.parametrize(
    "body", ["[]\n", '{"dependencies": null}\n'], ids=["top-level-list", "null-dependencies"]
)
def test_a_package_json_of_an_unexpected_shape_is_a_manifest_and_nothing_crashes(
    tmp_path: Path, body: str
) -> None:
    """Valid JSON that is not the object `ks init` expected. Before #434
    only init read package.json; the preflight and the doctor check now
    read it through the same function, so a shape it did not expect
    must be a manifest to all three, not a traceback in all three."""
    root = greenfield(tmp_path, extra={"package.json": body})
    agent, calls = recording_agent(root)

    decomposed = spec_command(root, "decompose", agent)
    assert "Traceback" not in decomposed.stdout, decomposed.stdout
    assert REFUSAL not in decomposed.stdout
    assert calls.exists(), decomposed.stdout

    doctor = run_ks(root, "doctor", "--root", str(root))
    assert "Traceback" not in doctor.stdout, doctor.stdout
    assert "[ok] build_manifest:" in doctor.stdout

    init = run_ks(root, "init", str(root), "--ui", "plain", "--no-color")
    assert init.returncode == 0, init.stdout
    assert "Fix first" not in init.stdout


def test_doctor_puts_the_missing_manifest_first_in_fix_first(tmp_path: Path) -> None:
    root = greenfield(tmp_path)

    proc = run_ks(root, "doctor", "--root", str(root))

    assert proc.returncode == 2, proc.stdout
    assert "ks doctor: not-ready" in proc.stdout
    assert "[fail] build_manifest:" in proc.stdout
    lines = proc.stdout.splitlines()
    first_fix = lines[lines.index("Fix first:") + 1]
    assert first_fix.startswith("  1. kstrl will not create the build manifest"), first_fix
    assert "uv init --package ." in first_fix
    assert "uv add --dev pytest mypy ruff" in first_fix


def test_doctor_does_not_guess_when_kstrl_toml_does_not_load(tmp_path: Path) -> None:
    """No manifest and an unreadable [verify]: whether the operator named
    their own commands is unknown, so the row says it was not evaluated
    rather than asserting either answer."""
    root = greenfield(tmp_path)
    (root / "kstrl.toml").write_bytes(b"[verify\n")

    proc = run_ks(root, "doctor", "--root", str(root))

    assert proc.returncode == 2, proc.stdout
    assert (
        "[fail] build_manifest: not evaluated: kstrl.toml did not load (see kstrl_config)"
        in proc.stdout
    )


def test_init_says_kstrl_will_not_create_the_manifest_and_prints_the_commands(
    tmp_path: Path,
) -> None:
    root = greenfield(tmp_path)

    proc = run_ks(root, "init", str(root), "--ui", "plain", "--no-color")

    assert proc.returncode == 0, proc.stdout
    assert "Fix first" in proc.stdout
    assert "no build manifest at the repository root that kstrl recognises" in proc.stdout
    assert "kstrl will not create the build manifest" in proc.stdout
    assert "uv init --package ." in proc.stdout
    assert proc.stdout.index("Fix first") < proc.stdout.index("Next steps")


def test_init_says_nothing_about_a_manifest_that_exists(tmp_path: Path) -> None:
    root = greenfield(tmp_path, extra={"pyproject.toml": MANIFESTS["pyproject.toml"]})

    proc = run_ks(root, "init", str(root), "--ui", "plain", "--no-color")

    assert proc.returncode == 0, proc.stdout
    assert "Fix first" not in proc.stdout
    assert "kstrl will not create" not in proc.stdout


def test_the_home_shell_decompose_launch_refuses_before_building_an_agent(
    tmp_path: Path,
) -> None:
    root = greenfield(tmp_path)
    (root / "kstrl.toml").write_text('[agent]\ncommand = "fake-agent"\n', encoding="utf-8")

    with patch("kstrl.agents.get_agent") as get_agent, pytest.raises(LaunchError) as raised:
        start_run_session(DecomposeLaunch(spec_path=root / "spec.md", project_name="demo"), root)

    assert "kstrl will not create the build manifest" in str(raised.value)
    assert get_agent.call_count == 0
    assert not list((root / ".kstrl" / "runs").glob("*"))


def test_every_manifest_kstrl_will_not_write_is_one_the_refusal_recognises(
    tmp_path: Path,
) -> None:
    """The two vocabularies, tied. `ROOT_BUILD_MANIFESTS` is what no
    component may write; `_detect_project_context` is what the refusal
    reads. A manifest in the first that the second does not recognise
    would refuse a repository that already has the file kstrl forbids
    anyone to create, so every one of them must clear the refusal.

    Imported here rather than at module level so that, before the fix,
    only this test fails on the import and every end-to-end test above
    fails on its own assertion.
    """
    from kstrl.decompose import _ALLOWED_PATHS_EXCLUDE, ROOT_BUILD_MANIFESTS
    from kstrl.init_cmd import BUILD_MANIFEST_MISSING, build_manifest_blocker

    assert ROOT_BUILD_MANIFESTS, "the exclusion list lost its build manifests"
    assert ROOT_BUILD_MANIFESTS <= _ALLOWED_PATHS_EXCLUDE
    empty = tmp_path / "empty"
    empty.mkdir()
    # The control: without it, a predicate that never refuses passes the loop.
    assert build_manifest_blocker(empty) == BUILD_MANIFEST_MISSING
    for name in sorted(ROOT_BUILD_MANIFESTS):
        repo = tmp_path / name.replace(".", "_")
        repo.mkdir()
        (repo / name).write_text(MANIFESTS.get(name, ""), encoding="utf-8")
        assert build_manifest_blocker(repo) is None, name


@pytest.mark.parametrize("key", ["test_command", "typecheck_command", "lint_command"])
def test_each_verify_command_lets_an_unrecognised_toolchain_reach_the_architect(
    tmp_path: Path, key: str
) -> None:
    root = greenfield(tmp_path, extra={"Gemfile": 'source "https://rubygems.org"\n'})
    (root / "kstrl.toml").write_text(f'[verify]\n{key} = "bundle exec x"\n', encoding="utf-8")
    agent, calls = recording_agent(root)
    proc = spec_command(root, "decompose", agent)
    assert REFUSAL not in proc.stdout
    assert calls.exists(), proc.stdout


@pytest.mark.parametrize(
    ("key", "command"),
    [
        ("test_command", "uv run pytest -q tests"),
        ("lint_command", "uv run ruff check src"),
    ],
)
def test_a_verify_command_that_runs_through_uv_does_not_satisfy_the_escape(
    tmp_path: Path, key: str, command: str
) -> None:
    """A greenfield Python repository (#434 B1). Setting a [verify]
    command to a `uv run ...` invocation, even one that is not any of
    the three literal defaults, does not describe a toolchain kstrl
    does not recognise: `uv run` itself needs the pyproject.toml this
    repository does not have, so the command cannot run any more than
    the refusal it is supposed to avoid. It must not satisfy the
    escape. Parametrized over more than one [verify] key and over
    commands distinct from `verify.DEFAULT_TEST_COMMAND` /
    `DEFAULT_TYPECHECK_COMMAND` / `DEFAULT_LINT_COMMAND`, so a check
    that compares against those three literal strings instead of the
    `uv run` prefix cannot pass this test."""
    root = greenfield(tmp_path)
    (root / "kstrl.toml").write_text(f'[verify]\n{key} = "{command}"\n', encoding="utf-8")
    agent, calls = recording_agent(root)

    proc = spec_command(root, "decompose", agent)

    assert proc.returncode == 2, proc.stdout
    assert REFUSAL in proc.stdout
    assert not calls.exists(), proc.stdout


def test_doctor_ok_detail_names_which_of_the_two_conditions_applied(tmp_path: Path) -> None:
    """B1: `check_build_manifest`'s `[ok]` line must say which of the
    two conditions in `build_manifest_ok_reason` let the repository
    through, not one sentence that reads the same either way."""
    with_manifest = greenfield(tmp_path, extra={"pyproject.toml": MANIFESTS["pyproject.toml"]})
    proc = run_ks(with_manifest, "doctor", "--root", str(with_manifest))
    assert (
        "[ok] build_manifest: a build manifest kstrl recognises is at the "
        "repository root" in proc.stdout
    ), proc.stdout

    second_tmp = tmp_path / "b"
    second_tmp.mkdir()
    root = greenfield(second_tmp, extra={"Gemfile": 'source "https://rubygems.org"\n'})
    (root / "kstrl.toml").write_text(
        '[verify]\ntest_command = "bundle exec rspec"\n', encoding="utf-8"
    )
    proc = run_ks(root, "doctor", "--root", str(root))
    assert (
        "[ok] build_manifest: no build manifest kstrl recognises is at the "
        "repository root, but [verify] names a command" in proc.stdout
    ), proc.stdout


def test_the_home_shell_decompose_launch_honours_the_verify_escape(tmp_path: Path) -> None:
    root = greenfield(tmp_path, extra={"Gemfile": 'source "https://rubygems.org"\n'})
    (root / "kstrl.toml").write_text(
        '[agent]\ncommand = "fake-agent"\n[verify]\ntest_command = "bundle exec rspec"\n',
        encoding="utf-8",
    )
    with patch(
        "kstrl.agents.get_agent", side_effect=RuntimeError("reached get_agent")
    ) as get_agent:
        with pytest.raises(BaseException) as raised:
            start_run_session(
                DecomposeLaunch(spec_path=root / "spec.md", project_name="demo"), root
            )
    assert "kstrl will not create the build manifest" not in str(raised.value)
    assert get_agent.call_count == 1


def test_a_defect_inside_the_build_manifest_check_is_a_traceback_not_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a kstrl.toml that does not load (OSError, ValueError) is 'not evaluated'. A defect
    inside the check must propagate, so a widened catch cannot hide it."""

    def defect(root: Path, *, read_verify: bool = True) -> str | None:
        raise TypeError("planted defect")

    monkeypatch.setattr("kstrl.doctor.build_manifest_blocker", defect)
    result = CliRunner().invoke(cli, ["doctor", "--root", str(tmp_path)])
    assert isinstance(result.exception, TypeError), result.output
