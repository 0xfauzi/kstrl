"""Tests for parsers module."""

from __future__ import annotations

from pathlib import Path

from ralph_py.parsers import (
    ParsedFailure,
    ParsedOutput,
    add_source_context,
    generate_fix_hint,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
)


# ---------------------------------------------------------------------------
# parse_pytest_output
# ---------------------------------------------------------------------------


class TestParsePytestOutput:
    def test_parse_pytest_output(self) -> None:
        raw = (
            "============================= test session starts =============================\n"
            "collected 5 items\n"
            "\n"
            "tests/test_foo.py ..F..\n"
            "\n"
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1, got 2\n"
            "FAILED tests/test_foo.py::test_baz - TypeError: unsupported operand\n"
            "============================= 2 failed, 3 passed in 1.23s =============================\n"
        )
        result = parse_pytest_output(raw)
        assert result.tool == "pytest"
        assert result.total_errors == 2
        assert len(result.failures) == 2
        assert result.failures[0].file == "tests/test_foo.py"
        assert result.failures[0].rule_or_test == "test_bar"
        assert "expected 1" in result.failures[0].message
        assert result.failures[1].rule_or_test == "test_baz"
        assert "2 failed" in result.raw_summary

    def test_parse_pytest_output_empty(self) -> None:
        result = parse_pytest_output("")
        assert result.tool == "pytest"
        assert result.total_errors == 0
        assert result.failures == []
        assert result.raw_summary == ""

    def test_parse_pytest_output_none_input(self) -> None:
        result = parse_pytest_output(None)  # type: ignore[arg-type]
        assert result.tool == "pytest"
        assert result.total_errors == 0

    def test_parse_pytest_output_error_lines(self) -> None:
        raw = (
            "=========================== short test summary info ===========================\n"
            "ERROR tests/test_foo.py - CollectionError: cannot import module\n"
            "============================= 1 error in 0.50s =============================\n"
        )
        result = parse_pytest_output(raw)
        assert len(result.failures) == 1
        assert result.failures[0].rule_or_test == "collection"
        assert result.total_errors == 1


# ---------------------------------------------------------------------------
# parse_mypy_output
# ---------------------------------------------------------------------------


class TestParseMypyOutput:
    def test_parse_mypy_output(self) -> None:
        raw = (
            "ralph_py/factory.py:10: error: Incompatible types in assignment [assignment]\n"
            "ralph_py/manifest.py:25: error: Missing return statement [return]\n"
            "Found 2 errors in 2 files (checked 10 source files)\n"
        )
        result = parse_mypy_output(raw)
        assert result.tool == "mypy"
        assert result.total_errors == 2
        assert len(result.failures) == 2
        assert result.failures[0].file == "ralph_py/factory.py"
        assert result.failures[0].line == 10
        assert result.failures[0].rule_or_test == "assignment"
        assert "Incompatible types" in result.failures[0].message
        assert result.failures[1].file == "ralph_py/manifest.py"
        assert result.failures[1].rule_or_test == "return"

    def test_parse_mypy_output_empty(self) -> None:
        result = parse_mypy_output("")
        assert result.tool == "mypy"
        assert result.total_errors == 0
        assert result.failures == []

    def test_parse_mypy_output_no_errors(self) -> None:
        raw = "Success: no issues found in 5 source files\n"
        result = parse_mypy_output(raw)
        assert result.tool == "mypy"
        assert result.total_errors == 0
        assert result.failures == []


# ---------------------------------------------------------------------------
# parse_ruff_output
# ---------------------------------------------------------------------------


class TestParseRuffOutput:
    def test_parse_ruff_output(self) -> None:
        raw = (
            "ralph_py/factory.py:15:1: E501 Line too long (95 > 79)\n"
            "ralph_py/factory.py:20:1: F401 `os` imported but unused\n"
            "ralph_py/manifest.py:5:1: F821 Undefined name `foo`\n"
            "Found 3 errors.\n"
        )
        result = parse_ruff_output(raw)
        assert result.tool == "ruff"
        assert result.total_errors == 3
        assert len(result.failures) == 3
        assert result.failures[0].file == "ralph_py/factory.py"
        assert result.failures[0].line == 15
        assert result.failures[0].rule_or_test == "E501"
        assert "Line too long" in result.failures[0].message
        assert result.failures[1].rule_or_test == "F401"
        assert result.failures[2].rule_or_test == "F821"

    def test_parse_ruff_output_empty(self) -> None:
        result = parse_ruff_output("")
        assert result.tool == "ruff"
        assert result.total_errors == 0
        assert result.failures == []

    def test_parse_ruff_output_with_fix_summary(self) -> None:
        raw = (
            "ralph_py/factory.py:10:5: W291 trailing whitespace\n"
            "Found 1 error (1 fixed, 0 remaining).\n"
        )
        result = parse_ruff_output(raw)
        assert result.total_errors == 1
        assert len(result.failures) == 1


# ---------------------------------------------------------------------------
# add_source_context
# ---------------------------------------------------------------------------


class TestAddSourceContext:
    def test_add_source_context(self, tmp_path: Path) -> None:
        src_file = tmp_path / "example.py"
        src_file.write_text(
            "line1\n"
            "line2\n"
            "line3\n"
            "line4_error_here\n"
            "line5\n"
            "line6\n"
            "line7\n"
        )
        failure = ParsedFailure(file="example.py", line=4, message="some error")
        add_source_context(failure, tmp_path, context_lines=2)
        assert failure.source_context != ""
        assert "line4_error_here" in failure.source_context
        # The error line should be marked with >
        assert ">" in failure.source_context

    def test_add_source_context_no_file(self, tmp_path: Path) -> None:
        failure = ParsedFailure(file="nonexistent.py", line=1, message="err")
        add_source_context(failure, tmp_path)
        assert failure.source_context == ""

    def test_add_source_context_no_line(self, tmp_path: Path) -> None:
        src_file = tmp_path / "example.py"
        src_file.write_text("line1\n")
        failure = ParsedFailure(file="example.py", line=0, message="err")
        add_source_context(failure, tmp_path)
        assert failure.source_context == ""


# ---------------------------------------------------------------------------
# generate_fix_hint
# ---------------------------------------------------------------------------


class TestGenerateFixHint:
    def test_missing_argument(self) -> None:
        failure = ParsedFailure(
            message="missing 1 required positional argument: 'name'",
            rule_or_test="test_create",
        )
        hint = generate_fix_hint(failure)
        assert "required argument" in hint.lower() or "function signature" in hint.lower()

    def test_type_mismatch(self) -> None:
        failure = ParsedFailure(
            message='Argument "x" has incompatible type "str"; expected "int"',
            rule_or_test="assignment",
        )
        hint = generate_fix_hint(failure)
        assert "type mismatch" in hint.lower() or "convert" in hint.lower()

    def test_import_error(self) -> None:
        failure = ParsedFailure(
            message="ModuleNotFoundError: No module named 'foo'",
            rule_or_test="test_import",
        )
        hint = generate_fix_hint(failure)
        assert "import" in hint.lower()

    def test_no_hint_for_uncommon_error(self) -> None:
        failure = ParsedFailure(
            message="something completely unique and unusual happened",
            rule_or_test="test_weird",
        )
        hint = generate_fix_hint(failure)
        assert hint == ""

    def test_ruff_unused_import(self) -> None:
        failure = ParsedFailure(
            message="`os` imported but unused",
            rule_or_test="F401",
        )
        hint = generate_fix_hint(failure)
        assert "unused import" in hint.lower()


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------


class TestFormatForPrompt:
    def test_format_for_prompt_with_failures(self) -> None:
        output = ParsedOutput(
            tool="pytest",
            total_errors=2,
            failures=[
                ParsedFailure(
                    file="test_foo.py", line=10,
                    rule_or_test="test_bar", message="assert 1 == 2",
                    source_context="  10 | assert 1 == 2",
                    fix_hint="Check expected vs actual values.",
                ),
                ParsedFailure(
                    file="test_foo.py", line=20,
                    rule_or_test="test_baz", message="KeyError",
                ),
            ],
            raw_summary="2 failed, 3 passed in 1.5s",
        )
        lines = output.format_for_prompt()
        assert len(lines) >= 3
        assert "[pytest]" in lines[0]
        assert "test_foo.py:10" in lines[1]
        assert "[test_bar]" in lines[1]
        # Source context should be included
        assert any("| assert 1 == 2" in line for line in lines)
        # Hint should be included
        assert any("hint:" in line for line in lines)

    def test_format_for_prompt_empty(self) -> None:
        output = ParsedOutput(tool="ruff", raw_summary="All good")
        lines = output.format_for_prompt()
        assert len(lines) == 1
        assert "[ruff]" in lines[0]

    def test_format_for_prompt_truncation(self) -> None:
        failures = [
            ParsedFailure(file=f"f{i}.py", line=i, message=f"err{i}")
            for i in range(15)
        ]
        output = ParsedOutput(tool="ruff", total_errors=15, failures=failures)
        lines = output.format_for_prompt(max_failures=5)
        # Should show 5 failures + "... and 10 more errors"
        assert any("10 more errors" in line for line in lines)

    def test_format_for_prompt_no_source(self) -> None:
        output = ParsedOutput(
            tool="mypy",
            failures=[
                ParsedFailure(
                    file="x.py", line=1, message="err",
                    source_context="  1 | x = 1",
                ),
            ],
        )
        lines = output.format_for_prompt(include_source=False)
        assert not any("|" in line for line in lines)
