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
    check_self_critique,
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


class TestDefaultTypecheckCommand:
    """Gap 2 fix: when ``check_typecheck`` is called without an explicit
    command, it should defer to the project's pyproject.toml mypy
    configuration rather than overriding it with ``uv run mypy .``.

    The end-to-end factory validation run on 2026-05-27 surfaced this
    bug: the agent self-reported all checks passing (using CLAUDE.md's
    contract command), but Phase 1 failed because the factory ran
    ``uv run mypy .`` which scanned tests/ and pulled in errors that
    weren't in the agent's diff."""

    def _default(self, cwd: Path) -> str:
        from ralph_py.verify import _default_typecheck_command
        return _default_typecheck_command(cwd)

    def test_no_pyproject_falls_back_to_dot(self, tmp_path: Path) -> None:
        """A directory without pyproject.toml keeps the broad default
        so greenfield projects still get typecheck coverage."""
        assert self._default(tmp_path) == "uv run mypy ."

    def test_pyproject_without_mypy_section_falls_back_to_dot(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'foo'\n",
        )
        assert self._default(tmp_path) == "uv run mypy ."

    def test_mypy_files_present_uses_no_arg_form(self, tmp_path: Path) -> None:
        """With ``[tool.mypy] files`` configured, the default defers
        to the project's mypy config by passing no path argument."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mypy]\nstrict = true\nfiles = ["my_pkg"]\n',
        )
        assert self._default(tmp_path) == "uv run mypy"

    def test_mypy_packages_present_uses_no_arg_form(
        self, tmp_path: Path,
    ) -> None:
        """``[tool.mypy] packages`` also triggers the no-arg default."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mypy]\nstrict = true\npackages = ["my_pkg"]\n',
        )
        assert self._default(tmp_path) == "uv run mypy"

    def test_mypy_section_without_files_or_packages_falls_back(
        self, tmp_path: Path,
    ) -> None:
        """A [tool.mypy] section that only sets strict / python_version
        but doesn't constrain scope keeps the broad default."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.mypy]\nstrict = true\npython_version = "3.11"\n',
        )
        assert self._default(tmp_path) == "uv run mypy ."

    def test_malformed_pyproject_falls_back_to_dot(
        self, tmp_path: Path,
    ) -> None:
        """Don't crash on malformed TOML -- fall back to the broad
        default, which the operator can then override explicitly."""
        (tmp_path / "pyproject.toml").write_text("not [valid toml")
        assert self._default(tmp_path) == "uv run mypy ."


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
        assert any("config/secret.py" in d for d in result.details)
        # In-scope files are not listed as violations
        assert not any("src/main.py" in d for d in result.details)

    def test_failure_names_base_branch_and_allowed_paths(
        self, tmp_path: Path,
    ) -> None:
        """R0.4: the failure details must name the base branch and the
        allowed paths -- without them the retry agent guesses both (the
        recorded e2e run guessed `main`, checked out base-branch content,
        and failed again)."""
        with patch(
            "ralph_py.verify.git.get_diff_names",
            return_value=["evil.py"],
        ):
            result = check_diff_scope(
                tmp_path, "feat/retrospective-cleanup-2",
                allowed_paths=["src/", "tests/"],
            )
        assert result.passed is False
        assert "feat/retrospective-cleanup-2" in result.message
        joined = "\n".join(result.details)
        assert "Base branch: feat/retrospective-cleanup-2" in joined
        assert "Allowed paths (complete list): src/, tests/" in joined

    def test_retry_context_carries_base_and_full_allowed_paths(
        self, tmp_path: Path,
    ) -> None:
        """The retry prompt is built via VerificationResult.as_context()
        (which slices details[:10]) and IterationContext.format_for_prompt.
        The base branch, EVERY allowed path, EVERY shown violation, and the
        truncation marker must survive that pipeline verbatim."""
        from ralph_py.context import IterationContext
        from ralph_py.verify import VerificationResult

        allowed = [f"pkg{i}/" for i in range(12)]
        violations = [f"rogue{i}.py" for i in range(20)]
        with patch(
            "ralph_py.verify.git.get_diff_names", return_value=violations,
        ):
            result = check_diff_scope(tmp_path, "main", allowed_paths=allowed)

        verification = VerificationResult(passed=False, checks=[result])
        ctx = IterationContext()
        ctx.add_verification_failure(verification.as_context())
        prompt_text = ctx.format_for_prompt()

        assert "Base branch: main" in prompt_text
        for path in allowed:
            assert path in prompt_text
        for shown in violations[:15]:
            assert shown in prompt_text
        assert "and 5 more" in prompt_text


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


class TestCheckSelfCritique:
    """Tests for the engineer-prompt self-critique mechanical check."""

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = check_self_critique(tmp_path / "missing.txt")
        assert result.passed is False
        assert "Could not read" in result.message

    def test_no_block_fails(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n- did stuff\n- ran tests\n",
        )
        result = check_self_critique(progress)
        assert result.passed is False
        assert "No '## Self-Critique'" in result.message

    def test_block_with_three_bullets_passes(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n"
            "- did stuff\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: invalid input crashes parser\n"
            "  - Failure mode 2: concurrent writes race\n"
            "  - Failure mode 3: timeout swallowed silently\n"
        )
        result = check_self_critique(progress)
        assert result.passed is True
        assert "3 failure modes" in result.message

    def test_fewer_than_min_fails(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1 - US-001\n"
            "- **Self-Critique:**\n"
            "  - Failure mode 1: x\n"
            "  - Failure mode 2: y\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False
        assert "2 bullets" in result.message

    def test_tbd_bullets_dont_count(self, tmp_path: Path) -> None:
        """The check should reject placeholder content like TBD/TODO/N/A."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "- **Self-Critique:**\n"
            "  - TBD\n"
            "  - TODO write later\n"
            "  - N/A\n"
            "  - Failure mode: empty input crashes the parser\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False  # only 1 substantive bullet

    def test_latest_iteration_block_used(self, tmp_path: Path) -> None:
        """Multiple Self-Critique blocks in one file - the LAST one is
        evaluated so previous iterations don't carry the current one."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "- **Self-Critique:**\n"
            "  - mode1: x\n"
            "  - mode2: y\n"
            "  - mode3: z\n"
            "\n## Iteration 2\n"
            "- **Self-Critique:**\n"
            "  - only one this time\n"
        )
        result = check_self_critique(progress, min_bullets=3)
        assert result.passed is False  # latest iteration has only 1

    def test_h2_style_heading_recognized(self, tmp_path: Path) -> None:
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "Some narrative.\n"
            "## Self-Critique\n"
            "- failure A: detailed reason\n"
            "- failure B: detailed reason\n"
            "- failure C: detailed reason\n"
        )
        result = check_self_critique(progress)
        assert result.passed is True

    def test_does_not_match_self_critique_in_prose(self, tmp_path: Path) -> None:
        """A reference to 'self-critique' in body text must not be
        treated as the heading. Only proper headings count."""
        progress = tmp_path / "progress.txt"
        progress.write_text(
            "## Iteration 1\n"
            "I wrote a self-critique that lists three failure modes:\n"
            "- mode A\n"
            "- mode B\n"
            "- mode C\n"
            "\nDone.\n"
        )
        result = check_self_critique(progress)
        assert result.passed is False
        assert "No '## Self-Critique'" in result.message

    def test_fuzz_corpus_of_accepted_headings(self, tmp_path: Path) -> None:
        """Forms the engineer prompt's loose phrasing might produce."""
        accepted = [
            "## Self-Critique",
            "## self-critique",  # case-insensitive
            "### Self-Critique",  # H3 also OK
            "- **Self-Critique:**",
            "- **Self-Critique**",
            "* **Self-Critique:**",
            "## Self Critique",  # space instead of hyphen
        ]
        for heading in accepted:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"## Iteration 1\nbody\n{heading}\n"
                "- failure 1: realistic description with details\n"
                "- failure 2: realistic description with details\n"
                "- failure 3: realistic description with details\n"
            )
            result = check_self_critique(progress)
            assert result.passed is True, f"heading {heading!r} should be accepted"

    def test_fuzz_corpus_of_rejected_lines(self, tmp_path: Path) -> None:
        """Lines that mention self-critique but aren't a heading."""
        rejected = [
            "the self-critique below lists failure modes",
            "self-critique: yes",  # no leading marker
            "**self-critique:**",  # bare bold, no list marker
            "selfcritique",  # no separator
            "see Self-Critique above",
        ]
        for line in rejected:
            progress = tmp_path / "progress.txt"
            progress.write_text(
                f"## Iteration 1\n{line}\n"
                "- failure: realistic\n"
                "- failure: realistic\n"
                "- failure: realistic\n"
            )
            result = check_self_critique(progress)
            assert result.passed is False, (
                f"line {line!r} should not be treated as heading"
            )


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
                "ralph_py.verify.run_scrubbed",
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
            patch("ralph_py.verify.run_scrubbed", side_effect=mock_run),
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
            patch("ralph_py.verify.run_scrubbed", side_effect=mock_run),
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
            patch("ralph_py.verify.run_scrubbed", side_effect=mock_run),
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
            patch("ralph_py.verify.run_scrubbed", side_effect=mock_run),
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
