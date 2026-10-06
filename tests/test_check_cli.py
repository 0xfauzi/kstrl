"""Tests for ``ks check`` (R10.1): the mechanical checks run standalone.

Each test builds a real git repository under ``tmp_path`` whose
``[verify]`` commands are fast no-op Python one-liners, then drives the
command through ``CliRunner`` and reads the ``--json`` document back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.stack import stack_toml
from tests.conftest import snapshot_kstrl_dir
from tests.helpers import gitrepo
from tests.helpers.stack_confirmation import confirm_stack, in_process_stack
from tests.spine_utils import git

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"
_LINT_FAIL_COMMAND = (
    f"{sys.executable} -c 'import sys; print(\"x.py:1:1: E501 line too long\"); sys.exit(1)'"
)

# CheckResult.name values, read from kstrl/verify.py, not guessed.
_ALWAYS_ON_CHECKS = {"stack:tests", "stack:typecheck", "stack:lint", "diff_scope", "bad_patterns"}


def _kstrl_toml(lint_command: str = _OK_COMMAND) -> str:
    # json.dumps yields a valid TOML basic string for these commands
    # (the failing lint command carries embedded double quotes).
    return stack_toml(
        in_process_stack({"tests": _OK_COMMAND, "typecheck": _OK_COMMAND, "lint": lint_command})
    )


def _make_repo(tmp_path: Path, lint_command: str = _OK_COMMAND) -> Path:
    """One-commit git repo on ``main`` with a module, a test and kstrl.toml."""
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    (root / "pyproject.toml").write_text('[project]\nname = "proj"\nversion = "0.0.1"\n')
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(
        "from src.a import a\n\n\ndef test_a() -> None:\n    assert a() == 1\n"
    )
    (root / "kstrl.toml").write_text(_kstrl_toml(lint_command))
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    confirm_stack(root)
    return root


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, ["check", *args])


def _check_json(root: Path, *extra: str) -> tuple[Result, dict[str, Any]]:
    result = _invoke("--root", str(root), "--json", *extra)
    document: dict[str, Any] = json.loads(result.stdout)
    return result, document


def _check(document: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [c for c in document["checks"] if c["name"] == name]
    assert len(matches) == 1, f"expected one {name!r} check, got {matches!r}"
    return matches[0]


def test_check_passes_on_clean_tree(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    # 3, not 2 (#335); 2, not 1, was #306. The literal is pinned
    # deliberately: this document is a published surface, and the bump
    # is the only thing that tells a reader an ABSENT check row no
    # longer means "turned off in kstrl.toml" - it can now also mean
    # "asked for, measured nothing", which `not_measured` below
    # disambiguates. #335 extended that to the dead-code gate and added
    # a new row name, `dead_code_ruff`, to `checks`. 5 is #696 slice 8,
    # which retired six check names and added `retired` to the baseline.
    assert document["schema_version"] == 5
    assert document["path"] == str(root)
    assert document["passed"] is True
    # Present and empty on a tree where every enabled check measured
    # something: a reader can always index it, and an empty list is a
    # positive statement rather than a missing key.
    assert document["not_measured"] == []
    names = {c["name"] for c in document["checks"]}
    assert _ALWAYS_ON_CHECKS <= names
    for check in document["checks"]:
        assert set(check) == {
            "name",
            "passed",
            "message",
            "details",
            "duration_seconds",
            "findings",
        }
        assert check["passed"] is True
        assert isinstance(check["duration_seconds"], float)


def test_check_reports_failure_and_exits_1(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, lint_command=_LINT_FAIL_COMMAND)

    result, document = _check_json(root)

    assert result.exit_code == 1
    assert document["passed"] is False
    linter = _check(document, "stack:lint")
    assert linter["passed"] is False
    assert linter["message"]
    # The other checks still ran and still pass: no short-circuit.
    assert _check(document, "stack:tests")["passed"] is True
    assert _check(document, "stack:typecheck")["passed"] is True


def test_check_skips_prd_checks_without_prd(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    names = {c["name"] for c in document["checks"]}
    assert "prd_stories" not in names
    assert "fixtures" not in names


def test_check_runs_prd_checks_with_prd(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    prd = tmp_path / "prd.json"
    prd.write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Test",
                        "acceptanceCriteria": ["AC"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            }
        )
    )

    result, document = _check_json(root, "--prd", str(prd))

    assert result.exit_code == 1
    stories = _check(document, "prd_stories")
    assert stories["passed"] is False
    assert "US-001" in "".join(stories["details"])


def test_check_no_scope_constraints_without_allowed_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    scope = _check(document, "diff_scope")
    assert scope["passed"] is True
    assert "No scope constraints" in scope["message"]


def test_check_enforces_allowed_path(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 2\n")
    git("commit", "-q", "-am", "change a", cwd=root)

    result, document = _check_json(root, "--allowed-path", "docs/**")

    assert result.exit_code == 1
    # No origin in this repo, so detection reaches the candidate rung
    # and finds the local `main` the feature commit diverged from.
    assert document["base_branch"] == "main"
    scope = _check(document, "diff_scope")
    assert scope["passed"] is False
    assert "src/a.py" in "".join(scope["details"])


def test_check_exit_2_on_missing_path(tmp_path: Path) -> None:
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
    assert document["schema_version"] == 5
    assert "error" in document
    assert missing in document["error"]


def test_check_exit_2_on_malformed_kstrl_toml(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text("[verify\nthis is not toml\n")

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    assert "error" in json.loads(result.stdout)


def test_check_writes_nothing(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    kstrl_dir = root / ".kstrl"
    assert not kstrl_dir.exists()
    before = snapshot_kstrl_dir(kstrl_dir)
    tracked_before = git("status", "--porcelain", cwd=root)

    result, _document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert snapshot_kstrl_dir(kstrl_dir) == before
    assert not kstrl_dir.exists()
    # The no-op verify commands leave the checkout untouched too.
    assert git("status", "--porcelain", cwd=root) == tracked_before


def test_check_help_lists_every_option() -> None:
    result = CliRunner().invoke(cli, ["check", "--help"])

    assert result.exit_code == 0
    for option in (
        "--root",
        "--path",
        "--base",
        "--prd",
        "--allowed-path",
        # R10.6 (#227): the baseline group.
        "--write-baseline",
        "--compare-baseline",
        "--force",
        "--fail-on-regression",
        "--format",
        "--json",
        "--ui",
        "--no-color",
    ):
        assert option in result.output
    # The sentinel that gives --write-baseline/--compare-baseline an optional
    # value is a NUL byte, which cannot appear in an argv element. It must not
    # leak into the help text, and the metavar must read as optional.
    assert "\x00" not in result.output
    assert "--write-baseline [PATH]" in result.output


# --- Read-only contract (R10.1 review, P1) ------------------------------
#
# `ks check` measures the operator's LIVE checkout, not a worktree kstrl
# owns. Before the fix, `[verify] dead_code_cleanup = true` made it run
# `ruff --fix`, `git add -A` and `git commit`: HEAD moved and an
# unrelated untracked file was swept into a commit nobody asked for.
# #696 slice 8 removed that phase; the contract is still asserted.


def _feature_repo(tmp_path: Path) -> Path:
    """Repo on a feature branch, with an untracked bystander.

    `unrelated.txt` is what a `git add -A` would sweep in.
    """
    root = _make_repo(tmp_path)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "b.py").write_text("import os\n\n\ndef b() -> int:\n    return 2\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "add b", cwd=root)
    (root / "unrelated.txt").write_text("bystander\n")
    return root


def test_check_never_edits_stages_or_commits(tmp_path: Path) -> None:
    root = _feature_repo(tmp_path)
    head_before = git("rev-parse", "HEAD", cwd=root)
    log_before = git("log", "--oneline", cwd=root)
    status_before = git("status", "--porcelain", cwd=root)
    b_before = (root / "src" / "b.py").read_text()

    result, document = _check_json(root)

    assert result.exit_code in (0, 1), result.output
    assert document["checks"]
    assert git("rev-parse", "HEAD", cwd=root) == head_before
    assert git("log", "--oneline", cwd=root) == log_before
    # The bystander is still untracked, and still the only change.
    assert git("status", "--porcelain", cwd=root) == status_before
    assert "unrelated.txt" in status_before
    assert (root / "src" / "b.py").read_text() == b_before


def test_check_leaves_no_bytecode_or_lint_cache(tmp_path: Path) -> None:
    root = _feature_repo(tmp_path)

    result, _document = _check_json(root)

    assert result.exit_code in (0, 1), result.output
    assert list(root.rglob("__pycache__")) == []
    assert not (root / ".ruff_cache").exists()


# --- Git preflight (R10.1 review, P1) -----------------------------------
#
# The diff-consuming checks read git through the LENIENT helpers, which
# map a bad ref or a missing repository onto an EMPTY file list. Before
# the fix, an unreachable base made diff_scope report "0 files, all
# within scope" and bad_patterns "scanned 0 Python files", and the
# command exited 0 having measured nothing.


def _diverged_repo(tmp_path: Path, base: str = "main") -> Path:
    """Repo whose feature branch carries a change away from ``base``."""
    root = _make_repo(tmp_path)
    git("branch", "-m", "main", base, cwd=root)
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "src" / "a.py").write_text("def a() -> int:\n    return 2\n")
    git("commit", "-q", "-am", "change a", cwd=root)
    return root


def test_check_exit_2_when_explicit_base_is_unreachable(tmp_path: Path) -> None:
    root = _diverged_repo(tmp_path)

    result = _invoke("--root", str(root), "--json", "--base", "no-such-branch")

    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    document = json.loads(result.stdout)
    assert "no-such-branch" in document["error"]
    assert "from --base" in document["error"]
    # No verdict was invented for a diff git could not produce.
    assert "passed" not in document
    assert "checks" not in document


def test_check_detects_the_base_branch_that_exists(tmp_path: Path) -> None:
    """No origin, and the base is `trunk` rather than `main` (#259).

    This repo used to be the exit-2 fixture below: detection returned
    the literal `main`, the diff failed, and the test pinned that as
    correct. The ladder now asks the repo, so the diff is real and the
    measurement is against the branch that exists.
    """
    root = _diverged_repo(tmp_path, base="trunk")

    result, document = _check_json(root, "--allowed-path", "docs/**")

    assert document["base_branch"] == "trunk"
    # A real diff was read against trunk: the out-of-scope file is
    # named, rather than a clean tree nobody managed to measure.
    scope = _check(document, "diff_scope")
    assert scope["passed"] is False
    assert "src/a.py" in "".join(scope["details"])
    assert result.exit_code == 1


def test_check_exit_2_when_detected_base_is_missing(tmp_path: Path) -> None:
    """No origin and no branch the ladder knows: detection falls back to
    a branch that does not exist, and the fallback must not read as a
    clean diff.

    `release-2.0` is deliberately outside the candidate set, which is
    the only way the fallback is still reachable now that the ladder
    checks the repo (#259). The branch HEAD is on is NOT a rung, so
    standing on `feature` cannot rescue this into a silent empty diff.
    """
    root = _diverged_repo(tmp_path, base="release-2.0")

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    document = json.loads(result.stdout)
    assert "'main'" in document["error"]
    assert "--base" in document["error"]


def test_check_exit_2_outside_a_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "kstrl.toml").write_text(_kstrl_toml())
    confirm_stack(root)

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2
    assert "cannot measure the diff" in json.loads(result.stdout)["error"]


def test_check_runs_without_git_when_no_check_reads_the_diff(
    tmp_path: Path,
) -> None:
    """The preflight guards the diff-based checks, not the command: turn
    them all off and a plain directory is measurable again."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "kstrl.toml").write_text(
        _kstrl_toml() + "[verify]\ncheck_diff_scope = false\ncheck_bad_patterns = false\n"
    )
    confirm_stack(root)

    result, document = _check_json(root)

    assert result.exit_code == 0, result.output
    assert document["passed"] is True
    names = {c["name"] for c in document["checks"]}
    assert names == {"stack:tests", "stack:typecheck", "stack:lint"}
    # #696 decision 6: Phase 1 parsing which test files ran is a loss this
    # slice accepts (tests/test_tests_ran_cli.py, whose only subject was
    # that detector, was deleted). Nothing is left not-measured here.
    assert document["not_measured"] == []


#: The keys and sections #696 slice 8 retired, each with a value that
#: used to be valid: a check that read one language's files or ran one
#: language's tools.
RETIRED_SETTINGS = {
    "mutation_testing": "[verify]\nmutation_testing = true\n",
    "dead_code_cleanup": "[verify]\ndead_code_cleanup = true\n",
    "[adequacy]": '[adequacy]\nenabled = true\nlayer0 = "block"\n',
}


@pytest.mark.parametrize("name", sorted(RETIRED_SETTINGS))
def test_check_refuses_a_retired_setting_by_name(tmp_path: Path, name: str) -> None:
    """A retired key is refused before anything runs, naming the key and
    what replaced it, rather than read and ignored (#696 slice 8)."""
    root = _make_repo(tmp_path)
    (root / "kstrl.toml").write_text(_kstrl_toml() + RETIRED_SETTINGS[name])

    result = _invoke("--root", str(root), "--json")

    assert result.exit_code == 2, result.output
    error = json.loads(result.stdout)["error"]
    assert name in error
    # "which was retired" is config_preflight's wording for a RETIRED_KEYS
    # row. A bare "retired" would also match tmp_path, which pytest names
    # after this test, and the generic unknown-key refusal would pass.
    assert "which was retired" in error
