"""#621: the Phase 1 and Phase 3 commands on a project that is not Python.

End to end: real git repositories under ``tmp_path``, the real ``ks init``,
``ks config show`` and ``ks check`` through ``CliRunner``, and the real
``ks factory`` in its own process with a stub engineer that counts its
calls. A ``uv`` that records its arguments and exits 0 is first on PATH,
so the log says exactly which Python default ran, and a default that ran
on a tree with nothing to measure would have passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.helpers.executables import put_on_path, write_executable
from tests.helpers.procs import kill_group

#: Generous for a refusal measured well under a second; a hang fails loudly.
FUSE_SECONDS = 120.0

_OK = f"{sys.executable} -c 'print(1)'"
_CARGO_TOML = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
_GATES = {"test_suite", "typecheck", "linter"}


def _fake_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a recording ``uv`` first on PATH; return the log it appends to."""
    log = tmp_path / "uv.log"
    put_on_path(tmp_path, monkeypatch, "uv", f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    return log


def _uv_calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _repo(tmp_path: Path, files: dict[str, str], *, init: bool) -> Path:
    """A committed repository on ``main`` holding ``files``, `ks init`-ed or not."""
    root = tmp_path / "proj"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if init:
        result = CliRunner().invoke(cli, ["init", str(root), "--ui", "plain"])
        assert result.exit_code == 0, result.output
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    return root


def _uncomment_verify(root: Path) -> None:
    """Uncomment the three command lines `ks init` seeded in [verify] only."""
    toml = root / "kstrl.toml"
    head, rest = toml.read_text(encoding="utf-8").split("[verify]\n", 1)
    block, tail = rest.split("\n[", 1)
    for key in ("test_command", "typecheck_command", "lint_command"):
        block = block.replace(f"# {key} = ", f"{key} = ", 1)
    toml.write_text(f"{head}[verify]\n{block}\n[{tail}", encoding="utf-8")


def _config_show(root: Path) -> Result:
    return CliRunner().invoke(cli, ["config", "show", "--root", str(root)])


def _contract_line(output: str) -> str:
    section = output.split("[contract]\n", 1)[1].split("\n[", 1)[0]
    (line,) = [ln for ln in section.splitlines() if ln.strip().startswith("test_command = ")]
    return line.strip()


def _check_json(root: Path) -> dict[str, Any]:
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json"])
    document: dict[str, Any] = json.loads(result.stdout)
    return document


def test_contract_test_command_follows_verify_when_unset(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"Cargo.toml": _CARGO_TOML}, init=True)
    _uncomment_verify(root)

    result = _config_show(root)

    assert result.exit_code == 0, result.output
    assert _contract_line(result.output) == "test_command = 'cargo test'  (from [verify])"


def test_contract_test_command_set_explicitly_still_wins(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"Cargo.toml": _CARGO_TOML}, init=True)
    _uncomment_verify(root)
    toml = root / "kstrl.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "[contract]\n", '[contract]\ntest_command = "make itest"\n', 1
        ),
        encoding="utf-8",
    )

    result = _config_show(root)

    assert result.exit_code == 0, result.output
    assert _contract_line(result.output) == "test_command = 'make itest'  (toml)"


def test_an_explicitly_empty_typecheck_command_is_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _fake_uv(tmp_path, monkeypatch)
    root = _repo(
        tmp_path,
        {
            "package.json": '{"name": "web", "private": true}\n',
            "kstrl.toml": (
                f"[verify]\ntest_command = {json.dumps(_OK)}\n"
                f'typecheck_command = ""\nlint_command = {json.dumps(_OK)}\n'
            ),
        },
        init=False,
    )

    document = _check_json(root)

    names = {check["name"] for check in document["checks"]}
    assert {"test_suite", "linter"} <= names
    assert "typecheck" not in names
    assert document["not_measured"] == [
        {
            "check": "typecheck",
            "reason": "no_target",
            "detail": "[verify] typecheck_command is empty",
        }
    ]
    assert _uv_calls(log) == []


def test_an_unset_command_on_a_tree_with_no_python_manifest_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _fake_uv(tmp_path, monkeypatch)
    root = _repo(tmp_path, {"Cargo.toml": _CARGO_TOML, "src/main.rs": "fn main() {}\n"}, init=True)

    document = _check_json(root)

    rows = {check["name"]: check for check in document["checks"]}
    assert _GATES <= set(rows)
    for name in _GATES:
        assert rows[name]["passed"] is False
        assert rows[name]["message"].startswith("Not run: `uv run ")
        assert "has no pyproject.toml or setup.py" in rows[name]["message"]
    assert document["passed"] is False
    assert _uv_calls(log) == []


def test_python_repo_resolution_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: on a Python tree the three defaults run exactly as before."""
    log = _fake_uv(tmp_path, monkeypatch)
    root = _repo(
        tmp_path, {"pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n'}, init=True
    )

    document = _check_json(root)

    rows = {check["name"]: check for check in document["checks"]}
    assert _GATES <= set(rows)
    assert all(rows[name]["passed"] for name in _GATES)
    assert document["not_measured"] == []
    assert _uv_calls(log) == ["run pytest", "run mypy .", "run ruff check ."]
    assert _contract_line(_config_show(root).output).startswith("test_command = 'uv run pytest'  (")


def test_init_seeds_clippy_with_all_targets(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"Cargo.toml": _CARGO_TOML}, init=True)

    text = (root / "kstrl.toml").read_text(encoding="utf-8")

    assert '# typecheck_command = "cargo check --all-targets"\n' in text
    assert '# lint_command = "cargo clippy --all-targets -- -D warnings"\n' in text


def _factory_repo(tmp_path: Path) -> Path:
    """A Rust repository with one component whose PRD fails Phase 1 on its own."""
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["a"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": "storage",
                "title": "storage",
                "description": "",
                "dependencies": [],
                "prdPath": "scripts/kstrl/feature/storage/prd.json",
                "branchName": "kstrl/factory/storage",
            }
        ],
    }
    prd = {"branchName": "kstrl/factory/storage", "userStories": [story]}
    return _repo(
        tmp_path,
        {
            "Cargo.toml": _CARGO_TOML,
            "kstrl.toml": "",
            "scripts/kstrl/feature/storage/prd.json": json.dumps(prd),
            "scripts/kstrl/manifest.json": json.dumps(manifest),
        },
        init=False,
    )


def _factory(tmp_path: Path, root: Path, *, engineer_does: str = "") -> tuple[str, int, int]:
    """Run the real `ks factory`; return (output, exit code, engineer calls).

    ``engineer_does`` is shell the stub engineer runs in its own working
    directory (the component's worktree) before it reports COMPLETE."""
    calls = tmp_path / "agent.calls"
    stub = write_executable(
        tmp_path / "agent.sh",
        f"#!/bin/sh\necho call >> '{calls}'\ncat >/dev/null\n{engineer_does}\n"
        "echo '<promise>COMPLETE</promise>'\n",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KSTRL_", "FACTORY_")) and k not in ("AGENT_CMD", "MODEL")
    }
    env.update(AGENT_CMD=str(stub), KSTRL_AGENT_PROBE="0", KSTRL_NO_TUI="1")
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    args = [
        sys.executable,
        "-m",
        "kstrl",
        "factory",
        "--manifest",
        str(root / "scripts" / "kstrl" / "manifest.json"),
        "--root",
        str(root),
        "--no-tui",
        "--yes",
        "--ui",
        "plain",
        "--no-color",
        "--no-prs",
        "--max-retries",
        "0",
        "--max-parallel",
        "1",
        "--review-mode",
        "skip",
        "--contract-check",
        "skip",
    ]
    child = subprocess.Popen(
        args,
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, _ = child.communicate(timeout=FUSE_SECONDS)
    except subprocess.TimeoutExpired:
        kill_group(child.pid)
        child.communicate()
        pytest.fail(f"`ks factory` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    count = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
    return out, child.returncode, count


def test_ks_factory_does_not_run_python_defaults_on_a_non_python_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 on an unconfigured Rust tree fails the three gates without
    spawning `uv`, where it used to run pytest, mypy and ruff and pass lint."""
    log = _fake_uv(tmp_path, monkeypatch)
    root = _factory_repo(tmp_path)

    out, code, calls = _factory(tmp_path, root)

    assert calls == 1, out
    assert code != 0, out
    assert _uv_calls(log) == []
    journal = (root / ".kstrl" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    (verification,) = [
        event["data"]
        for event in map(json.loads, journal)
        if event["event"] == "verification_result"
    ]
    assert verification["passed"] is False
    not_run = [f for f in verification["failures"] if f.startswith("Not run: `uv run ")]
    assert len(not_run) == 3, verification["failures"]


def test_ks_factory_runs_python_defaults_in_a_worktree_the_engineer_made_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for where the question is asked: the root checkout has no
    pyproject.toml, the engineer writes one in its worktree, and Phase 1
    runs the three Python defaults there. A greenfield Python project
    starts exactly like this, so asking the root would refuse all of them."""
    log = _fake_uv(tmp_path, monkeypatch)
    root = _factory_repo(tmp_path)

    out, _code, calls = _factory(
        tmp_path,
        root,
        engineer_does="echo '[project]' > pyproject.toml",
    )

    assert calls == 1, out
    assert not (root / "pyproject.toml").exists()
    assert _uv_calls(log) == ["run pytest", "run mypy .", "run ruff check ."], out
