"""Fixtures on a tree that is not Python (#632, slice 1).

End to end: the real ``ks check --prd P --json`` in its own process,
against a committed git repository, with fixtures enabled through the
environment. No LLM is called and no toolchain beyond ``sh`` is needed.

Two things were wrong. A ``function`` fixture imports a Python module with
kstrl's own interpreter, so on a tree with no pyproject.toml or setup.py it
failed every attempt with ``ModuleNotFoundError`` and nothing refused it
first. And a ``cli`` fixture could not be given input: its child inherited
kstrl's own stdin, so the same PRD passed under ``/dev/null`` and timed out
under an open pipe. Every run here gives ``ks check`` a pipe whose write end
stays open for the whole run, which is the case that used to time out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import gitrepo
from tests.helpers.executables import write_executable
from tests.helpers.procs import kill_group

#: The whole run takes about a second; a hang fails loudly instead.
FUSE_SECONDS = 120.0

#: Short, so the case that used to hang on the inherited pipe costs 3s, not 30s.
FIXTURE_TIMEOUT_SECONDS = "3"

REFUSAL = "PRD names fixtures this tree cannot run (failing closed)"

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"

#: A program in no particular language: it sums the integers on stdin.
SUM = '#!/bin/sh\nn=0\nwhile read x; do n=$((n + x)); done\necho "sum=$n"\n'

CARGO_TREE = {"Cargo.toml": '[package]\nname = "pricing"\nversion = "0.1.0"\n'}
#: Reads its stdin to EOF: the function runner must hand it an empty one.
PRICING = (
    "import sys\n\n\ndef total(quantity):\n    return quantity * 300 + len(sys.stdin.read())\n"
)
PYTHON_TREE = {
    "pyproject.toml": '[project]\nname = "pricing"\nversion = "0.1.0"\n',
    "pricing.py": PRICING,
}
#: #621's predicate also accepts a setup.py alone; a check for pyproject.toml
#: only would refuse this tree.
SETUP_PY_TREE = {"setup.py": "from setuptools import setup\n\nsetup()\n", "pricing.py": PRICING}

FILE_FIXTURE = {
    "description": "the program is there",
    "fixture_type": "file",
    "input_data": {"path": "sum"},
    "expected": {"exists": True},
}

FUNCTION_FIXTURE = {
    "description": "total(3) is 900",
    "fixture_type": "function",
    "input_data": {"module": "pricing", "function": "total", "args": [3]},
    "expected": {"returns": 900},
}


def _cli_fixture(stdin: object, expected: str) -> dict[str, Any]:
    """``./sum`` with ``stdin`` as its input; ``None`` leaves the key out."""
    input_data: dict[str, Any] = {"command": "./sum"}
    if stdin is not None:
        input_data["stdin"] = stdin
    return {
        "description": "sum of stdin",
        "fixture_type": "cli",
        "input_data": input_data,
        "expected": {"stdout_contains": [expected]},
    }


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A committed repository holding ``files``, ``./sum`` and a no-op kstrl.toml."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "kstrl.toml").write_text(
        "[verify]\n"
        f"test_command = {json.dumps(_OK_COMMAND)}\n"
        f"typecheck_command = {json.dumps(_OK_COMMAND)}\n"
        f"lint_command = {json.dumps(_OK_COMMAND)}\n",
        encoding="utf-8",
    )
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    write_executable(root / "sum", SUM)
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    return root


def _fixtures_row(root: Path, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Run ``ks check --prd`` with stdin on an open pipe; return its fixtures row."""
    prd = root.parent / "prd.json"
    story = {
        "id": "US-1",
        "title": "t",
        "acceptanceCriteria": ["c"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    prd.write_text(
        json.dumps({"branchName": "main", "userStories": [story], "fixtures": fixtures}),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("KSTRL_")}
    env["KSTRL_FIXTURES_ENABLED"] = "1"
    env["KSTRL_FIXTURES_TIMEOUT"] = FIXTURE_TIMEOUT_SECONDS
    read_end, write_end = os.pipe()
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "kstrl", "check", "--root", str(root)]
            + ["--base", "main", "--prd", str(prd), "--json"],
            # Outside the repository, so a check that asks the process's own
            # directory instead of the tree it was given cannot pass by accident.
            cwd=root.parent,
            env=env,
            stdin=read_end,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            out, err = child.communicate(timeout=FUSE_SECONDS)
        except subprocess.TimeoutExpired:
            kill_group(child.pid)
            child.communicate()
            pytest.fail(f"`ks check` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    finally:
        os.close(read_end)
        os.close(write_end)
    assert child.returncode in (0, 1), err
    rows = [c for c in json.loads(out)["checks"] if c["name"] == "fixtures"]
    assert len(rows) == 1, out
    row: dict[str, Any] = rows[0]
    return row


def test_a_function_fixture_on_a_tree_with_no_python_is_refused_at_validation(
    tmp_path: Path,
) -> None:
    """Refused by index before anything runs; the same PRD runs on both Python trees.

    The ``function`` fixture is second, so the line must carry ITS index. On
    the Python tree the function reads its stdin to EOF, so it passes only if
    the runner gave it an empty stdin rather than ``ks check``'s open pipe.
    """
    fixtures = [FILE_FIXTURE, FUNCTION_FIXTURE]

    refused = _fixtures_row(_repo(tmp_path / "cargo", CARGO_TREE), fixtures)

    assert refused["passed"] is False
    assert refused["message"] == REFUSAL
    assert len(refused["details"]) == 1, refused["details"]
    assert refused["details"][0].startswith("fixtures[1]: "), refused["details"]
    assert "a cli fixture runs any program" in refused["details"][0]
    assert not any("ModuleNotFoundError" in d for d in refused["details"])

    for name, tree in (("pyproject", PYTHON_TREE), ("setup-py", SETUP_PY_TREE)):
        ran = _fixtures_row(_repo(tmp_path / name, tree), fixtures)

        assert ran["passed"] is True, (name, ran)
        assert ran["message"] == "2/2 fixtures passed", (name, ran)


@pytest.mark.parametrize(
    ("stdin", "passed", "detail"),
    [
        ("1\n2\n3\n", True, "[PASS] sum of stdin: CLI fixture passed"),
        ("1\n2\n", False, "(actual: 'sum=3\\n')"),
        (None, False, "(actual: 'sum=0\\n')"),
        (3, False, "fixtures[0].input_data.stdin: must be a string"),
    ],
    ids=["all-input", "short-input", "no-stdin-key-reads-eof", "stdin-not-a-string"],
)
def test_a_cli_fixture_with_stdin_passes_and_fails_on_the_right_output(
    tmp_path: Path, stdin: object, passed: bool, detail: str
) -> None:
    row = _fixtures_row(_repo(tmp_path, CARGO_TREE), [_cli_fixture(stdin, "sum=6")])

    assert row["passed"] is passed, row
    assert len(row["details"]) == 1, row["details"]
    assert detail in row["details"][0], row["details"]
    assert "timed out" not in row["details"][0]
