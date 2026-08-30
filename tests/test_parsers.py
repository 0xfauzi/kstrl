"""Tests for parsers module."""

from __future__ import annotations

from pathlib import Path

from kstrl.parsers import (
    ParsedFailure,
    ParsedOutput,
    add_source_context,
    generate_fix_hint,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
)
from tests.helpers.tool_output import tool_output

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
            "kstrl/factory.py:10: error: Incompatible types in assignment [assignment]\n"
            "kstrl/manifest.py:25: error: Missing return statement [return]\n"
            "Found 2 errors in 2 files (checked 10 source files)\n"
        )
        result = parse_mypy_output(raw)
        assert result.tool == "mypy"
        assert result.total_errors == 2
        assert len(result.failures) == 2
        assert result.failures[0].file == "kstrl/factory.py"
        assert result.failures[0].line == 10
        assert result.failures[0].rule_or_test == "assignment"
        assert "Incompatible types" in result.failures[0].message
        assert result.failures[1].file == "kstrl/manifest.py"
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
            "kstrl/factory.py:15:1: E501 Line too long (95 > 79)\n"
            "kstrl/factory.py:20:1: F401 `os` imported but unused\n"
            "kstrl/manifest.py:5:1: F821 Undefined name `foo`\n"
            "Found 3 errors.\n"
        )
        result = parse_ruff_output(raw)
        assert result.tool == "ruff"
        assert result.total_errors == 3
        assert len(result.failures) == 3
        assert result.failures[0].file == "kstrl/factory.py"
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
            "kstrl/factory.py:10:5: W291 trailing whitespace\n"
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
        src_file.write_text("line1\nline2\nline3\nline4_error_here\nline5\nline6\nline7\n")
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
                    file="test_foo.py",
                    line=10,
                    rule_or_test="test_bar",
                    message="assert 1 == 2",
                    source_context="  10 | assert 1 == 2",
                    fix_hint="Check expected vs actual values.",
                ),
                ParsedFailure(
                    file="test_foo.py",
                    line=20,
                    rule_or_test="test_baz",
                    message="KeyError",
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
        failures = [ParsedFailure(file=f"f{i}.py", line=i, message=f"err{i}") for i in range(15)]
        output = ParsedOutput(tool="ruff", total_errors=15, failures=failures)
        lines = output.format_for_prompt(max_failures=5)
        # Should show 5 failures + "... and 10 more errors"
        assert any("10 more errors" in line for line in lines)

    def test_format_for_prompt_no_source(self) -> None:
        output = ParsedOutput(
            tool="mypy",
            failures=[
                ParsedFailure(
                    file="x.py",
                    line=1,
                    message="err",
                    source_context="  1 | x = 1",
                ),
            ],
        )
        lines = output.format_for_prompt(include_source=False)
        assert not any("|" in line for line in lines)


# ---------------------------------------------------------------------------
# Non-Python tool output (#258)
# ---------------------------------------------------------------------------

# Both captured from real runs; provenance and the two normalizations
# applied live in tests/helpers/tool_output.py. The vitest one is the
# reporter's own writers-room repo, one deliberately failing test. The
# pytest one is a run where every test PASSED: the polyglot gate chains
# toolchains (`uv run pytest && npm test`), so the half that passes can
# be the half the parser understands.
VITEST_FAILURE_OUTPUT = tool_output("vitest-2.1.9-writers-room.txt")
PYTEST_PASSING_OUTPUT = tool_output("pytest-9.1.1-passing.txt")


class TestNonPythonToolOutput:
    """What the pytest parser ALONE does with output pytest never wrote.

    These pin the behaviour of one parser, not of the gate. The gate no
    longer sends vitest output here unaccompanied: `check_test_suite`
    dispatches through `gateparse.parse_gate_output`, which also runs the
    vitest parser and unions the result (tests/test_gateparse.py). The
    pytest parser is still exercised on foreign text because it remains
    the test gate's PRIMARY, so its behaviour when it understands nothing
    is what an unrecognised toolchain still falls back to.
    """

    def test_vitest_output_parses_no_structured_failures(self) -> None:
        # Unchanged and still correct: this parser knows pytest. The
        # failing file, line, test name, assertion message and the
        # expected-versus-received diff are all in the raw output and
        # none of them survive HERE. Recovering them is the vitest
        # parser's job, and the dispatcher's job to call it.
        parsed = parse_pytest_output(VITEST_FAILURE_OUTPUT)
        assert parsed.failures == []

    def test_total_errors_is_not_zero_when_the_summary_says_failed(self) -> None:
        # Measured before the fix: 0, because total_errors was computed
        # from raw_summary BEFORE the tail fallback populated it, so the
        # field disagreed with the summary sitting next to it. Nothing
        # in kstrl/ reads total_errors today, so this pins a trap rather
        # than a live misread.
        parsed = parse_pytest_output(VITEST_FAILURE_OUTPUT)
        assert "1 failed" in parsed.raw_summary
        assert parsed.total_errors == 1

    def test_passthrough_is_labelled_with_the_command_that_ran(self) -> None:
        parsed = parse_pytest_output(VITEST_FAILURE_OUTPUT)
        parsed.command = "npm run test"
        lines = parsed.format_for_prompt()
        assert lines[0].startswith("[npm run test]")
        assert "[pytest]" not in "".join(lines)

    def test_tool_identity_is_unchanged(self) -> None:
        # `tool` names the PARSER; only the prompt label is derived from
        # the command. Nothing switches on `tool` any more (#258 moved
        # evolution onto ParsedFailure.code), but the two fields still
        # have to stay distinct or the label fix collapses back.
        parsed = parse_pytest_output(VITEST_FAILURE_OUTPUT)
        parsed.command = "npm run test"
        assert parsed.tool == "pytest"

    def test_parsed_failures_keep_the_tool_label(self) -> None:
        # Failures prove the parser understood the output, so `[pytest]`
        # is earned even when the command is recorded.
        raw = (
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1, got 2\n"
            "============================= 1 failed in 0.10s =============================\n"
        )
        parsed = parse_pytest_output(raw)
        parsed.command = "uv run pytest"
        assert parsed.format_for_prompt()[0].startswith("[pytest]")

    def test_label_falls_back_to_the_tool_without_a_command(self) -> None:
        parsed = parse_pytest_output(VITEST_FAILURE_OUTPUT)
        assert parsed.command == ""
        assert parsed.format_for_prompt()[0].startswith("[pytest]")

    def test_a_passing_half_does_not_become_the_failure_detail(self) -> None:
        """A failed gate must not report the passing toolchain's summary.

        The measured shape from the polyglot repo behind #258: pytest
        passes, vitest fails, `test_command` chains them. `_PYTEST_
        SUMMARY_RE` matches pytest's footer, so the summary became
        "5 passed in 0.00s" and, with the tail fallback suppressed by a
        summary already being set, that single line was the whole retry
        detail. The engineer was told five tests passed by a gate that
        had just failed, and the vitest failure was gone.
        """
        combined = PYTEST_PASSING_OUTPUT + VITEST_FAILURE_OUTPUT

        parsed = parse_pytest_output(combined)
        parsed.command = "uv run pytest && npm test"
        detail = "".join(parsed.format_for_prompt())

        assert parsed.failures == []
        assert "5 passed" not in detail
        # What survives is the failing half's own summary.
        assert "1 failed" in detail
        assert parsed.total_errors == 1

    def test_a_summary_reporting_failures_is_kept(self) -> None:
        # The guard is "no failure in sight", not "no parsed failures":
        # a real pytest footer that reports failures still wins over the
        # tail even when no FAILED line was parseable from it.
        raw = (
            "collected 5 items\n"
            "============================= 2 failed, 3 passed in 1.23s ==============================\n"
        )
        parsed = parse_pytest_output(raw)
        assert parsed.failures == []
        # The tail always contains the last line, so the discriminator is
        # the "=" padding: the extracted summary has none, the raw tail
        # keeps it. Taking the tail here would lose the count as well.
        assert parsed.raw_summary == "2 failed, 3 passed in 1.23s"
        assert parsed.total_errors == 2
