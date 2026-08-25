"""Tests for ``ks sense`` (R10.1): the mechanical sensors run standalone.

Each test builds a real git repository under ``tmp_path`` whose
``[verify]`` commands are fast no-op Python one-liners, then drives the
command through ``CliRunner`` and reads the ``--json`` document back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from click.testing import CliRunner, Result

from kstrl.cli import cli
from tests.conftest import snapshot_kstrl_dir
from tests.spine_utils import git

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"
_LINT_FAIL_COMMAND = (
    f"{sys.executable} -c "
    "'import sys; print(\"x.py:1:1: E501 line too long\"); sys.exit(1)'"
)

# CheckResult.name values, read from kstrl/verify.py, not guessed.
_ALWAYS_ON_CHECKS = {"test_suite", "typecheck", "linter", "diff_scope", "bad_patterns"}


def _kstrl_toml(lint_command: str = _OK_COMMAND) -> str:
    # json.dumps yields a valid TOML basic string for these commands
    # (the failing lint command carries embedded double quotes).
    return (
        "[verify]\n"
        f"test_command = {json.dumps(_OK_COMMAND)}\n"
        f"typecheck_command = {json.dumps(_OK_COMMAND)}\n"
        f"lint_command = {json.dumps(lint_command)}\n"
    )


def _make_repo(tmp_path: Path, lint_command: str = _OK_COMMAND) -> Path:
    """One-commit git repo on ``main`` with a module, a test and kstrl.toml."""
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "sense@test", cwd=root)
    git("config", "user.name", "Sense Test", cwd=root)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "proj"\nversion = "0.0.1"\n'
    )
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(
        "from src.a import a\n\n\ndef test_a() -> None:\n    assert a() == 1\n"
    )
    (root / "kstrl.toml").write_text(_kstrl_toml(lint_command))
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, ["sense", *args])


def _sense_json(root: Path, *extra: str) -> tuple[Result, dict[str, Any]]:
    result = _invoke("--root", str(root), "--json", *extra)
    document: dict[str, Any] = json.loads(result.stdout)
    return result, document


def _check(document: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [c for c in document["checks"] if c["name"] == name]
    assert len(matches) == 1, f"expected one {name!r} check, got {matches!r}"
    return matches[0]


def test_sense_passes_on_clean_tree(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    assert document["schema_version"] == 1
    assert document["path"] == str(root)
    assert document["passed"] is True
    names = {c["name"] for c in document["checks"]}
    assert _ALWAYS_ON_CHECKS <= names
    for check in document["checks"]:
        assert set(check) == {
            "name", "passed", "message", "details", "duration_seconds", "findings",
        }
        assert check["passed"] is True
        assert isinstance(check["duration_seconds"], float)


def test_sense_reports_failure_and_exits_1(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, lint_command=_LINT_FAIL_COMMAND)

    result, document = _sense_json(root)

    assert result.exit_code == 1
    assert document["passed"] is False
    linter = _check(document, "linter")
    assert linter["passed"] is False
    assert linter["message"]
    # The other sensors still ran and still pass: no short-circuit.
    assert _check(document, "test_suite")["passed"] is True
    assert _check(document, "typecheck")["passed"] is True


def test_sense_skips_prd_checks_without_prd(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    names = {c["name"] for c in document["checks"]}
    assert "prd_stories" not in names
    assert "fixtures" not in names


def test_sense_runs_prd_checks_with_prd(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    prd = tmp_path / "prd.json"
    prd.write_text(json.dumps({
        "branchName": "test",
        "userStories": [
            {
                "id": "US-001", "title": "Test", "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": False, "notes": "",
            }
        ],
    }))

    result, document = _sense_json(root, "--prd", str(prd))

    assert result.exit_code == 1
    stories = _check(document, "prd_stories")
    assert stories["passed"] is False
    assert "US-001" in "".join(stories["details"])


def test_sense_no_scope_constraints_without_allowed_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _sense_json(root)

    assert result.exit_code == 0, result.output
    scope = _check(document, "diff_scope")
    assert scope["passed"] is True
    assert "No scope constraints" in scope["message"]


def test_sense_enforces_allowed_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 2\n")
    git("commit", "-q", "-am", "change a", cwd=root)

    result, document = _sense_json(root, "--allowed-path", "docs/**")

    assert result.exit_code == 1
    # No origin in this repo: detection falls back to main, which is
    # the branch the feature commit diverged from.
    assert document["base_branch"] == "main"
    scope = _check(document, "diff_scope")
    assert scope["passed"] is False
    assert "src/a.py" in "".join(scope["details"])


def test_sense_exit_2_on_missing_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    missing = str(tmp_path / "nonexistent")

    result = _invoke("--root", str(root), "--path", missing)
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    assert result.stdout == ""

    result = _invoke("--root", str(root), "--path", missing, "--json")
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert "error" in document
    assert missing in document["error"]


def test_sense_exit_2_on_malformed_kstrl_toml(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text("[verify\nthis is not toml\n")

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    assert "error" in json.loads(result.stdout)


def test_sense_writes_nothing(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    kstrl_dir = root / ".kstrl"
    assert not kstrl_dir.exists()
    before = snapshot_kstrl_dir(kstrl_dir)
    tracked_before = git("status", "--porcelain", cwd=root)

    result, _document = _sense_json(root)

    assert result.exit_code == 0, result.output
    assert snapshot_kstrl_dir(kstrl_dir) == before
    assert not kstrl_dir.exists()
    # The no-op verify commands leave the checkout untouched too.
    assert git("status", "--porcelain", cwd=root) == tracked_before


def test_sense_help_lists_every_option() -> None:
    result = CliRunner().invoke(cli, ["sense", "--help"])

    assert result.exit_code == 0
    for option in (
        "--root", "--path", "--base", "--prd", "--allowed-path",
        "--json", "--ui", "--no-color",
    ):
        assert option in result.output
