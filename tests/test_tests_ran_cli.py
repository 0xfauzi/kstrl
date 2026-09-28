"""``ks check`` names a changed test file the test command did not run (#620).

Which tests the test gate runs is decided by files the engineer can edit:
``pytest.ini`` and ``pyproject.toml`` addopts, the ``package.json`` test
script. One edit that leaves a failing test file out used to turn every
row green. Each test here builds a real git repository, runs the real
``ks check --json`` through ``CliRunner`` with a real test runner, and
reads the ``test_suite`` row and the ``not_measured`` sidecar back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.spine_utils import git

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"
_PYTEST_COMMAND = f"{sys.executable} -m pytest -p no:cacheprovider"

_PYTEST_INI = "[pytest]\npythonpath = .\n"
_PRICING = "def percent(x: int) -> int:\n    return x * 10 // 100\n"
_PRICING_TEST = (
    "from pricing import percent\n\n\ndef test_percent() -> None:\n    assert percent(100) == 10\n"
)
_BULK_OK = "def bulk_percent(xs: list[int]) -> list[int]:\n    return [x // 10 for x in xs]\n"
_BULK_BUG = "def bulk_percent(xs: list[int]) -> list[int]:\n    return [x // 9 for x in xs]\n"
_BULK_TEST = (
    "from bulk import bulk_percent\n\n\n"
    "def test_bulk() -> None:\n    assert bulk_percent([100]) == [10]\n"
)
_IGNORE_BULK = "[pytest]\npythonpath = .\naddopts = --ignore=tests/test_bulk.py\n"


def _kstrl_toml(test_command: str) -> str:
    return (
        "[verify]\n"
        f"test_command = {json.dumps(test_command)}\n"
        f"typecheck_command = {json.dumps(_OK_COMMAND)}\n"
        f"lint_command = {json.dumps(_OK_COMMAND)}\n"
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, base: dict[str, str], test_command: str) -> Path:
    """A repo whose ``main`` holds ``base``, checked out on ``feature``."""
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    _write(root, {**base, "kstrl.toml": _kstrl_toml(test_command)})
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    git("checkout", "-q", "-b", "feature", cwd=root)
    return root


def _pytest_repo(tmp_path: Path) -> Path:
    base = {
        "pyproject.toml": '[project]\nname = "proj"\nversion = "0.0.1"\n',
        "pytest.ini": _PYTEST_INI,
        "pricing.py": _PRICING,
        "tests/test_pricing.py": _PRICING_TEST,
    }
    return _repo(tmp_path, base, _PYTEST_COMMAND)


def _commit(root: Path, files: dict[str, str]) -> None:
    _write(root, files)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "change", cwd=root)


def _check_json(root: Path) -> tuple[Result, dict[str, Any]]:
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json"])
    document: dict[str, Any] = json.loads(result.stdout)
    return result, document


def _suite(document: dict[str, Any]) -> dict[str, Any]:
    rows = [c for c in document["checks"] if c["name"] == "test_suite"]
    assert len(rows) == 1, rows
    row: dict[str, Any] = rows[0]
    return row


def _not_run(document: dict[str, Any]) -> list[str]:
    """The files the test_suite row says did not run, by finding."""
    return [
        f["location"]
        for f in _suite(document)["findings"]
        if f["category"] == "adequacy_test_not_run"
    ]


def test_excluding_a_failing_python_test_file_through_addopts_is_reported(
    tmp_path: Path,
) -> None:
    root = _pytest_repo(tmp_path)
    _commit(
        root,
        {"bulk.py": _BULK_BUG, "tests/test_bulk.py": _BULK_TEST, "pytest.ini": _IGNORE_BULK},
    )

    result, document = _check_json(root)

    # Advisory: the repo's rule for a new gate. The row still passes and
    # the command still exits 0, but it is no longer silent.
    assert result.exit_code == 0, result.output
    suite = _suite(document)
    assert suite["passed"] is True
    assert _not_run(document) == ["tests/test_bulk.py"]
    assert {f["severity"] for f in suite["findings"]} == {"advisory"}
    assert "tests/test_bulk.py" in suite["message"]
    assert document["not_measured"] == []


def test_excluding_a_changed_test_file_through_pyproject_is_reported(tmp_path: Path) -> None:
    """The pyproject spelling of the same exclusion, on a test file that
    already existed and was CHANGED rather than added."""
    root = _repo(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "proj"\nversion = "0.0.1"\n\n'
                '[tool.pytest.ini_options]\npythonpath = ["."]\n'
            ),
            "pricing.py": _PRICING,
            "tests/test_pricing.py": _PRICING_TEST,
            "bulk.py": _BULK_OK,
            "tests/test_bulk.py": _BULK_TEST,
        },
        _PYTEST_COMMAND,
    )
    _commit(
        root,
        {
            "bulk.py": _BULK_BUG,
            "tests/test_bulk.py": _BULK_TEST
            + "\n\ndef test_bulk_empty() -> None:\n    assert bulk_percent([]) == []\n",
            "pyproject.toml": (
                '[project]\nname = "proj"\nversion = "0.0.1"\n\n'
                '[tool.pytest.ini_options]\npythonpath = ["."]\n'
                'addopts = "--ignore=tests/test_bulk.py"\n'
            ),
        },
    )

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert _suite(document)["passed"] is True
    assert _not_run(document) == ["tests/test_bulk.py"]


def test_swapping_an_excluded_failing_test_for_a_trivial_one_is_reported(
    tmp_path: Path,
) -> None:
    """The trivial file has the excluded file's NAME in another directory,
    and as many test files ran as this diff changed, so a comparison by
    count or by file name sees nothing wrong. Compared by path, the
    failing file is named."""
    root = _pytest_repo(tmp_path)
    _commit(
        root,
        {
            "bulk.py": _BULK_BUG,
            "tests/test_bulk.py": _BULK_TEST,
            "trivial/test_bulk.py": "def test_trivial() -> None:\n    assert 1 == 1\n",
            "pytest.ini": _IGNORE_BULK,
        },
    )

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert _not_run(document) == ["tests/test_bulk.py"]


def test_a_failing_gate_keeps_its_message_and_still_names_the_file_that_did_not_run(
    tmp_path: Path,
) -> None:
    """A failing row's message feeds the baseline's failure signature, so
    the note is not added to it; the finding is recorded all the same."""
    root = _pytest_repo(tmp_path)
    _commit(
        root,
        {
            "pricing.py": "def percent(x: int) -> int:\n    return x\n",
            "bulk.py": _BULK_BUG,
            "tests/test_bulk.py": _BULK_TEST,
            "pytest.ini": _IGNORE_BULK,
        },
    )

    result, document = _check_json(root)

    assert result.exit_code == 1, result.output
    suite = _suite(document)
    assert suite["passed"] is False
    assert _not_run(document) == ["tests/test_bulk.py"]
    assert "did not run" not in suite["message"]


def test_an_unrelated_pytest_ini_edit_is_not_reported(tmp_path: Path) -> None:
    root = _pytest_repo(tmp_path)
    _commit(
        root,
        {
            "bulk.py": _BULK_OK,
            "tests/test_bulk.py": _BULK_TEST,
            "pytest.ini": _PYTEST_INI + "markers =\n    slow: a slow test\n",
        },
    )

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    suite = _suite(document)
    assert suite["passed"] is True
    assert suite["findings"] == []
    assert suite["message"] == "Tests passed"
    assert document["not_measured"] == []


def test_adding_a_test_that_runs_is_not_reported(tmp_path: Path) -> None:
    root = _pytest_repo(tmp_path)
    _commit(root, {"bulk.py": _BULK_OK, "tests/test_bulk.py": _BULK_TEST})

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert _suite(document)["findings"] == []
    assert document["not_measured"] == []


def test_a_changed_test_file_no_readable_runner_ran_is_not_measured(tmp_path: Path) -> None:
    """A test command kstrl cannot read a test inventory from is NOT
    MEASURED, never a pass that vouches for the changed test file."""
    root = _repo(tmp_path, {"pricing.py": _PRICING}, _OK_COMMAND)
    _commit(root, {"bulk.py": _BULK_BUG, "tests/test_bulk.py": _BULK_TEST})

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert _suite(document)["findings"] == []
    gaps = [g for g in document["not_measured"] if g["check"] == "tests_ran"]
    assert len(gaps) == 1, document["not_measured"]
    assert gaps[0]["reason"] == "tool_missing"
    assert "tests/test_bulk.py" in gaps[0]["detail"]


@pytest.mark.skipif(shutil.which("npx") is None, reason="needs Node (npx) to run vitest")
def test_narrowing_the_npm_test_script_is_reported(tmp_path: Path) -> None:
    package = {
        "name": "web",
        "version": "0.0.1",
        "private": True,
        "type": "module",
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "3.2.4"},
    }
    root = _repo(
        tmp_path,
        {
            "package.json": json.dumps(package, indent=2),
            ".gitignore": "node_modules/\n",
            "src/pricing.ts": "export function percent(x: number): number {\n"
            "  return Math.floor((x * 10) / 100);\n}\n",
            "src/pricing.test.ts": 'import { expect, test } from "vitest";\n'
            'import { percent } from "./pricing";\n'
            'test("percent", () => {\n  expect(percent(100)).toBe(10);\n});\n',
        },
        "npm test",
    )
    # --offline: the default suite never reaches the network (R4.3), so
    # vitest comes from the local npm cache or the test does not run.
    install = subprocess.run(
        ["npm", "install", "--offline", "--no-audit", "--no-fund"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if install.returncode != 0:
        pytest.skip(f"vitest 3.2.4 is not in the local npm cache: {install.stderr[-300:]}")
    package["scripts"] = {"test": "vitest run --passWithNoTests src/pricing.test.ts"}
    _commit(
        root,
        {
            "package.json": json.dumps(package, indent=2),
            "src/bulk.ts": "export function bulkPercent(xs: number[]): number[] {\n"
            "  return xs.map((x) => Math.floor((x * 11) / 100));\n}\n",
            "src/bulk.test.ts": 'import { expect, test } from "vitest";\n'
            'import { bulkPercent } from "./bulk";\n'
            'test("bulk", () => {\n  expect(bulkPercent([100])).toEqual([10]);\n});\n',
        },
    )

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert _suite(document)["passed"] is True
    assert _not_run(document) == ["src/bulk.test.ts"]
