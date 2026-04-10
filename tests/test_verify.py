"""Tests for verify module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ralph_py.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    check_bad_patterns,
    check_dead_code,
    check_diff_scope,
    check_mutation_score,
    check_prd_stories,
    check_test_suite,
    check_typecheck,
    run_mechanical_verification,
)


class TestCheckPrdStories:
    def test_all_passing(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(json.dumps({
            "branchName": "test",
            "userStories": [
                {
                    "id": "US-001", "title": "Test", "acceptanceCriteria": ["AC"],
                    "priority": 1, "passes": True, "notes": "",
                }
            ],
        }))
        result = check_prd_stories(prd)
        assert result.passed is True

    def test_story_not_passing(self, tmp_path: Path) -> None:
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
        result = check_prd_stories(prd)
        assert result.passed is False
        assert "US-001" in result.details[0]

    def test_invalid_prd(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("not json")
        result = check_prd_stories(prd)
        assert result.passed is False
        assert "Failed to load" in result.message

    def test_empty_stories(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(json.dumps({
            "branchName": "test",
            "userStories": [],
        }))
        result = check_prd_stories(prd)
        assert result.passed is True


class TestCheckTestSuite:
    def test_passing_command(self, tmp_path: Path) -> None:
        result = check_test_suite(tmp_path, command="true", timeout=5.0)
        assert result.passed is True

    def test_failing_command(self, tmp_path: Path) -> None:
        result = check_test_suite(tmp_path, command="false", timeout=5.0)
        assert result.passed is False

    def test_timeout(self, tmp_path: Path) -> None:
        result = check_test_suite(tmp_path, command="sleep 10", timeout=0.1)
        assert result.passed is False
        assert "timed out" in result.message


class TestCheckTypecheck:
    def test_passing(self, tmp_path: Path) -> None:
        result = check_typecheck(tmp_path, command="true", timeout=5.0)
        assert result.passed is True

    def test_failing(self, tmp_path: Path) -> None:
        result = check_typecheck(tmp_path, command="false", timeout=5.0)
        assert result.passed is False


class TestCheckDiffScope:
    def test_no_constraints(self, tmp_path: Path) -> None:
        result = check_diff_scope(tmp_path, "main", allowed_paths=None)
        assert result.passed is True

    def test_all_in_scope(self, tmp_path: Path) -> None:
        with patch("ralph_py.verify.git.get_diff_names", return_value=["src/main.py"]):
            result = check_diff_scope(tmp_path, "main", allowed_paths=["src/"])
        assert result.passed is True

    def test_out_of_scope(self, tmp_path: Path) -> None:
        with patch(
            "ralph_py.verify.git.get_diff_names",
            return_value=["src/main.py", "config/secret.py"],
        ):
            result = check_diff_scope(tmp_path, "main", allowed_paths=["src/"])
        assert result.passed is False
        assert "config/secret.py" in result.details


class TestCheckBadPatterns:
    def test_clean_files(self, tmp_path: Path) -> None:
        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        with patch(
            "ralph_py.verify.git.get_diff_names", return_value=["clean.py"]
        ):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is True

    def test_empty_py_file(self, tmp_path: Path) -> None:
        py_file = tmp_path / "empty.py"
        py_file.write_text("")
        with patch(
            "ralph_py.verify.git.get_diff_names", return_value=["empty.py"]
        ):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is False
        assert any("empty" in d for d in result.details)

    def test_syntax_error(self, tmp_path: Path) -> None:
        py_file = tmp_path / "bad.py"
        py_file.write_text("def f(\n")
        with patch(
            "ralph_py.verify.git.get_diff_names", return_value=["bad.py"]
        ):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is False
        assert any("syntax" in d.lower() for d in result.details)

    def test_secret_detected(self, tmp_path: Path) -> None:
        py_file = tmp_path / "leak.py"
        py_file.write_text('API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n')
        with patch(
            "ralph_py.verify.git.get_diff_names", return_value=["leak.py"]
        ):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is False
        assert any("secret" in d.lower() for d in result.details)

    def test_non_py_files_skipped(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "data.txt"
        txt_file.write_text("")
        with patch(
            "ralph_py.verify.git.get_diff_names", return_value=["data.txt"]
        ):
            result = check_bad_patterns(tmp_path, "main")
        assert result.passed is True


class TestRunMechanicalVerification:
    def test_all_pass(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(json.dumps({
            "branchName": "test",
            "userStories": [
                {
                    "id": "US-001", "title": "Test", "acceptanceCriteria": ["AC"],
                    "priority": 1, "passes": True, "notes": "",
                }
            ],
        }))
        config = VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        result = run_mechanical_verification(
            tmp_path, prd, "main", None, config,
        )
        assert result.passed is True
        assert len(result.checks) == 4  # prd + test + typecheck + lint

    def test_partial_failure(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text(json.dumps({
            "branchName": "test",
            "userStories": [
                {
                    "id": "US-001", "title": "Test", "acceptanceCriteria": ["AC"],
                    "priority": 1, "passes": True, "notes": "",
                }
            ],
        }))
        config = VerifyConfig(
            test_command="false",   # Tests fail
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        result = run_mechanical_verification(
            tmp_path, prd, "main", None, config,
        )
        assert result.passed is False
        # All checks should have run (no short-circuit)
        assert len(result.checks) == 4
        assert result.checks[0].passed is True   # PRD stories
        assert result.checks[1].passed is False   # Test suite
        assert result.checks[2].passed is True   # Typecheck

    def test_as_context_formatting(self) -> None:
        result = VerificationResult(
            passed=False,
            checks=[
                CheckResult("test_suite", False, "2 failures", ["FAIL: test_a"]),
                CheckResult("typecheck", True, "ok"),
            ],
        )
        ctx = result.as_context()
        assert "test_suite: FAIL" in ctx
        assert "typecheck" not in ctx  # passed checks excluded


class TestCheckMutationScore:
    def test_skips_when_mutmut_not_installed(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value=None):
            result = check_mutation_score(tmp_path, "main")
        assert result.passed is True
        assert "not installed" in result.message

    def test_skips_when_no_py_files_changed(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch("ralph_py.verify.git.get_diff_names", return_value=["readme.md"]),
        ):
            result = check_mutation_score(tmp_path, "main")
        assert result.passed is True
        assert "No non-test" in result.message

    def test_skips_test_files(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch(
                "ralph_py.verify.git.get_diff_names",
                return_value=["test_main.py", "tests/test_foo.py"],
            ),
        ):
            result = check_mutation_score(tmp_path, "main")
        assert result.passed is True

    def test_timeout_passes_gracefully(self, tmp_path: Path) -> None:
        import subprocess as sp
        with (
            patch("shutil.which", return_value="/usr/bin/mutmut"),
            patch(
                "ralph_py.verify.git.get_diff_names",
                return_value=["src/main.py"],
            ),
            patch(
                "ralph_py.verify.subprocess.run",
                side_effect=sp.TimeoutExpired("mutmut", 600),
            ),
        ):
            result = check_mutation_score(tmp_path, "main", timeout=600)
        assert result.passed is True
        assert "timed out" in result.message


class TestCheckDeadCode:
    def test_no_tools_available_skips(self, tmp_path: Path) -> None:
        """When neither ruff nor vulture are installed, skip gracefully."""
        with patch("shutil.which", return_value=None):
            result = check_dead_code(tmp_path, "main")
        assert result.passed is True
        assert "neither vulture nor custom command" in result.message.lower()

    def test_ruff_fixes_committed(self, tmp_path: Path) -> None:
        """When ruff finds fixable issues, they are auto-committed."""
        import subprocess as sp

        calls: list[str] = []

        def mock_run(cmd: str, **kwargs: object) -> sp.CompletedProcess[str]:
            calls.append(cmd)
            if "ruff check --fix" in cmd:
                return sp.CompletedProcess(cmd, 0, "Found 3 errors (2 fixed, 1 remaining).", "")
            if "git add" in cmd:
                return sp.CompletedProcess(cmd, 0, "", "")
            if "git commit" in cmd:
                return sp.CompletedProcess(cmd, 0, "", "")
            # vulture
            return sp.CompletedProcess(cmd, 0, "", "")

        def mock_which(name: str) -> str | None:
            if name in ("ruff", "vulture"):
                return f"/usr/bin/{name}"
            return None

        with (
            patch("shutil.which", side_effect=mock_which),
            patch("ralph_py.verify.subprocess.run", side_effect=mock_run),
            patch("ralph_py.verify.git.get_diff_names", return_value=["src/main.py"]),
        ):
            result = check_dead_code(tmp_path, "main")

        assert result.passed is True
        assert "auto-fixed 2" in result.message
        assert any("git commit" in c for c in calls)

    def test_vulture_findings_fail(self, tmp_path: Path) -> None:
        """When vulture finds dead code, the check fails with details."""
        import subprocess as sp

        def mock_run(cmd: str, **kwargs: object) -> sp.CompletedProcess[str]:
            if "ruff check --fix" in cmd:
                return sp.CompletedProcess(cmd, 0, "", "")
            # vulture output
            return sp.CompletedProcess(
                cmd, 1,
                "src/main.py:10: unused function 'old_handler' (60% confidence)\n"
                "src/utils.py:25: unused variable 'temp' (90% confidence)\n",
                "",
            )

        def mock_which(name: str) -> str | None:
            if name in ("ruff", "vulture"):
                return f"/usr/bin/{name}"
            return None

        with (
            patch("shutil.which", side_effect=mock_which),
            patch("ralph_py.verify.subprocess.run", side_effect=mock_run),
            patch("ralph_py.verify.git.get_diff_names", return_value=["src/main.py", "src/utils.py"]),
        ):
            result = check_dead_code(tmp_path, "main")

        assert result.passed is False
        assert "2 dead code issues" in result.message
        assert len(result.details) == 2

    def test_custom_command_used(self, tmp_path: Path) -> None:
        """When a custom command is provided, it runs instead of vulture."""
        import subprocess as sp

        def mock_run(cmd: str, **kwargs: object) -> sp.CompletedProcess[str]:
            if "ruff check --fix" in cmd:
                return sp.CompletedProcess(cmd, 0, "", "")
            if "my-custom-checker" in cmd:
                return sp.CompletedProcess(cmd, 0, "", "")
            return sp.CompletedProcess(cmd, 0, "", "")

        with (
            patch("shutil.which", return_value="/usr/bin/ruff"),
            patch("ralph_py.verify.subprocess.run", side_effect=mock_run),
        ):
            result = check_dead_code(tmp_path, "main", command="my-custom-checker src/")

        assert result.passed is True

    def test_no_python_files_changed_passes(self, tmp_path: Path) -> None:
        """When no Python files changed, skip vulture scan."""
        import subprocess as sp

        def mock_run(cmd: str, **kwargs: object) -> sp.CompletedProcess[str]:
            return sp.CompletedProcess(cmd, 0, "", "")

        def mock_which(name: str) -> str | None:
            if name in ("ruff", "vulture"):
                return f"/usr/bin/{name}"
            return None

        with (
            patch("shutil.which", side_effect=mock_which),
            patch("ralph_py.verify.subprocess.run", side_effect=mock_run),
            patch("ralph_py.verify.git.get_diff_names", return_value=["README.md", "docs/spec.md"]),
        ):
            result = check_dead_code(tmp_path, "main")

        assert result.passed is True

    def test_included_in_verification_when_enabled(self, tmp_path: Path) -> None:
        """When dead_code_cleanup is True, check_dead_code runs in verification."""
        config = VerifyConfig(dead_code_cleanup=True)

        # Mock everything to pass
        with (
            patch("ralph_py.verify.check_prd_stories", return_value=CheckResult("prd_stories", True)),
            patch("ralph_py.verify.check_test_suite", return_value=CheckResult("test_suite", True)),
            patch("ralph_py.verify.check_typecheck", return_value=CheckResult("typecheck", True)),
            patch("ralph_py.verify.check_linter", return_value=CheckResult("linter", True)),
            patch("ralph_py.verify.check_diff_scope", return_value=CheckResult("diff_scope", True)),
            patch("ralph_py.verify.check_bad_patterns", return_value=CheckResult("bad_patterns", True)),
            patch("ralph_py.verify.check_dead_code", return_value=CheckResult("dead_code", True)) as mock_dc,
        ):
            result = run_mechanical_verification(tmp_path, tmp_path / "prd.json", "main", None, config)

        mock_dc.assert_called_once()
        assert any(c.name == "dead_code" for c in result.checks)

    def test_excluded_from_verification_when_disabled(self, tmp_path: Path) -> None:
        """When dead_code_cleanup is False (default), check is skipped."""
        config = VerifyConfig(dead_code_cleanup=False)

        with (
            patch("ralph_py.verify.check_prd_stories", return_value=CheckResult("prd_stories", True)),
            patch("ralph_py.verify.check_test_suite", return_value=CheckResult("test_suite", True)),
            patch("ralph_py.verify.check_typecheck", return_value=CheckResult("typecheck", True)),
            patch("ralph_py.verify.check_linter", return_value=CheckResult("linter", True)),
            patch("ralph_py.verify.check_diff_scope", return_value=CheckResult("diff_scope", True)),
            patch("ralph_py.verify.check_bad_patterns", return_value=CheckResult("bad_patterns", True)),
        ):
            result = run_mechanical_verification(tmp_path, tmp_path / "prd.json", "main", None, config)

        assert not any(c.name == "dead_code" for c in result.checks)
