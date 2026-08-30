"""Structured output parsers for test runners, type checkers, and linters.

Transforms raw CLI output into structured failure objects that can generate
LLM-optimized context for retry prompts.

This module owns the shared types and the Python toolchain (pytest, mypy,
ruff). The other toolchains a gate can be pointed at live in
``kstrl.parsers_web``, and ``kstrl.gateparse`` is the dispatcher that
decides which of them a gate's output goes through (#258).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# CSI and OSC escape sequences. Tools colour their output whenever they
# think a human is watching, and `tsc --pretty` does it even through a
# pipe if the operator asked for it. Stripped once at the dispatcher so
# no parser has to carry `\x1b\[[0-9;]*m` in its own pattern (#258).
_ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(raw: str) -> str:
    """``raw`` with terminal colour and cursor escapes removed."""
    return _ANSI_RE.sub("", raw)


@dataclass
class ParsedFailure:
    """A single structured failure from a tool's output."""

    file: str = ""
    line: int = 0
    rule_or_test: str = ""  # test name, error code, or rule ID
    message: str = ""
    source_context: str = ""  # relevant source lines if available
    fix_hint: str = ""  # generated fix suggestion
    # Stable signature code for this failure, when the tool emits one:
    # a linter rule (`E501`, `no-unused-vars`), a checker error code
    # (`arg-type`, `TS2322`), or the exception class a test died on
    # (`assertion-error`). Empty when the tool gave nothing durable.
    #
    # `rule_or_test` cannot serve: for a test runner it holds the TEST
    # NAME, which is unique per test and useless as a signature.
    # evolution.signatures_from_verification used to recover the
    # difference by switching on `ParsedOutput.tool` against the exact
    # strings "ruff", "mypy" and "pytest" - so every tool added here fell
    # through to a prose slug, and a unioned label like "pytest+vitest"
    # broke the switch outright (#258). The parser knows its own format,
    # so the parser answers the question once, here.
    code: str = ""


@dataclass
class ParsedOutput:
    """Structured parse result from a tool's raw output."""

    # Which parser produced this. One registered name ("pytest", "vitest",
    # "mypy", "tsc", "ruff", "eslint"), or several joined with "+" when a
    # chained command emitted more than one format and the auto path
    # unioned them (#258). Read it as a label, never as a switch: the
    # capability consumers need is on ParsedFailure.code.
    tool: str
    total_errors: int = 0
    failures: list[ParsedFailure] = field(default_factory=list)
    raw_summary: str = ""  # last line(s) summary from the tool
    # The command the gate actually ran, when the caller knows it. Only
    # used to label a passthrough; the parser identity stays `tool`.
    command: str = ""

    @property
    def prompt_label(self) -> str:
        """What to call the output in the retry prompt.

        Parsed failures prove this parser understood the output, so the
        tool name is earned. With none, the parser may simply be the
        wrong one: `check_test_suite` runs whatever `test_command` is
        configured and always parses it as pytest, so a vitest failure
        reached the engineer tagged ``[pytest]`` and pointed it at the
        wrong toolchain (#258). Naming the command that actually ran is
        the one label that cannot be wrong.
        """
        if self.failures or not self.command:
            return self.tool
        return self.command

    def format_for_prompt(self, max_failures: int = 10, include_source: bool = True) -> list[str]:
        """Format failures as structured lines optimized for LLM consumption.

        Returns list of detail strings suitable for CheckResult.details.
        """
        lines: list[str] = []

        if self.raw_summary:
            lines.append(f"[{self.prompt_label}] {self.raw_summary}")

        if not self.failures:
            return lines

        shown = self.failures[:max_failures]
        for f in shown:
            location = f.file
            if f.line:
                location += f":{f.line}"
            tag = f.rule_or_test or "error"
            lines.append(f"  {location} [{tag}] {f.message}")

            if include_source and f.source_context:
                for ctx_line in f.source_context.splitlines():
                    lines.append(f"    | {ctx_line}")

            if f.fix_hint:
                lines.append(f"    hint: {f.fix_hint}")

        remaining = len(self.failures) - len(shown)
        if remaining > 0:
            lines.append(f"  ... and {remaining} more errors")

        return lines


# ---------------------------------------------------------------------------
# Pytest parser
# ---------------------------------------------------------------------------

# Matches: FAILED tests/test_foo.py::test_bar - AssertionError: some message
_PYTEST_FAILED_RE = re.compile(
    r"^FAILED\s+(?P<file>[^\s:]+)::(?P<test>[^\s]+)"
    r"(?:\s+-\s+(?P<message>.+))?$"
)

# Matches: ERROR tests/test_foo.py - CollectionError
_PYTEST_ERROR_RE = re.compile(
    r"^ERROR\s+(?P<file>[^\s:]+)(?:::(?P<test>[^\s]+))?"
    r"(?:\s+-\s+(?P<message>.+))?$"
)

# Matches: === 3 failed, 10 passed, 1 error in 4.52s ===
_PYTEST_SUMMARY_RE = re.compile(r"=+\s+(?P<summary>.+?)\s+=+\s*$")

# Extract failure count from a summary line like "3 failed". Not
# pytest-specific: vitest's footer says "2 failed | 3 passed (5)", so
# parsers_web reads the same count with the same pattern.
FAILED_COUNT_RE = re.compile(r"(\d+)\s+failed")

_PYTEST_ERROR_COUNT_RE = re.compile(r"(\d+)\s+error")


# Leading exception class in a test-runner failure message, e.g.
# "AssertionError: assert 1 == 2" or vitest's "AssertionError: expected
# false to be true". Python and JavaScript agree on the shape, so both
# test parsers share it.
_EXC_NAME_RE = re.compile(
    r"^([A-Z][A-Za-z0-9]*(?:Error|Exception|Failure|Warning|Exit|Interrupt))\b"
)

# Split points for CamelCase -> kebab-case, i.e. before each capital
# that is not the first character.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")


def exception_code(message: str) -> str:
    """Signature code for a test failure: "assertion-error", or "".

    Lifted out of ``evolution.signatures_from_verification`` so the
    parser that recognised the message shape also names it (#258).
    """
    m = _EXC_NAME_RE.match(message or "")
    if not m:
        return ""
    return _CAMEL_BOUNDARY_RE.sub("-", m.group(1)).lower()


def _pytest_failure_count(summary: str) -> int:
    """Failures plus errors reported by a pytest-shaped summary line."""
    count = 0
    failed_m = FAILED_COUNT_RE.search(summary)
    if failed_m:
        count += int(failed_m.group(1))
    error_m = _PYTEST_ERROR_COUNT_RE.search(summary)
    if error_m:
        count += int(error_m.group(1))
    return count


def _pytest_failure_from_line(stripped: str) -> ParsedFailure | None:
    """One FAILED or ERROR line as a failure, or None if it is neither."""
    m = _PYTEST_FAILED_RE.match(stripped)
    if m:
        message = m.group("message") or ""
        return ParsedFailure(
            file=m.group("file"),
            rule_or_test=m.group("test"),
            message=message,
            code=exception_code(message),
        )

    m = _PYTEST_ERROR_RE.match(stripped)
    if m:
        message = m.group("message") or ""
        return ParsedFailure(
            file=m.group("file"),
            rule_or_test=m.group("test") or "collection",
            message=message,
            code=exception_code(message),
        )

    return None


def parse_pytest_output(raw: str) -> ParsedOutput:
    """Parse pytest output into structured failures.

    Handles FAILED lines, ERROR lines, and the summary footer.
    Falls through gracefully on unparseable input.
    """
    result = ParsedOutput(tool="pytest")

    if not raw or not raw.strip():
        return result

    lines = raw.splitlines()

    for line in lines:
        stripped = line.strip()

        # The short-summary banner is "=" padded like the footer, so
        # without this it would match _PYTEST_SUMMARY_RE and stand as
        # raw_summary for any run whose footer never arrived.
        if "short test summary info" in stripped.lower():
            continue

        failure = _pytest_failure_from_line(stripped)
        if failure is not None:
            result.failures.append(failure)
            continue

        # Parse summary line
        m = _PYTEST_SUMMARY_RE.match(stripped)
        if m:
            result.raw_summary = m.group("summary").strip()

    # Nothing structured parsed AND no summary reporting a failure: what
    # we matched, if anything, is not describing whatever failed this
    # gate, so prefer the raw tail. Two measured cases (#258), both from
    # a polyglot repo running `uv run pytest && npm test`:
    #
    # - vitest alone: nothing matched at all, so there is no summary.
    # - pytest PASSING then vitest failing: `_PYTEST_SUMMARY_RE` matches
    #   pytest's footer, so the summary became "5 passed in 0.00s" and
    #   the failed gate's entire retry detail told the engineer that
    #   five tests passed, with the vitest failure dropped.
    #
    # The consequence for purely passing input is deliberate: the tail
    # replaces a clean footer. Only `check_test_suite` calls this, and
    # only on a nonzero exit, so "no failure in sight" always means the
    # parse missed it.
    #
    # The tail is still only the last few lines, so a foreign tool's
    # file, line and assertion detail is dropped either way; recovering
    # that needs a parser that knows the tool, which is the other half
    # of #258.
    if not result.failures and _pytest_failure_count(result.raw_summary) == 0:
        tail = lines[-5:] if len(lines) > 5 else lines
        result.raw_summary = "\n".join(tail)

    result.total_errors = (
        _pytest_failure_count(result.raw_summary) if result.raw_summary else len(result.failures)
    )

    return result


# ---------------------------------------------------------------------------
# Mypy parser
# ---------------------------------------------------------------------------

# Matches: file.py:10: error: Incompatible types [assignment]
_MYPY_ERROR_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):\s+error:\s+(?P<message>.+?)(?:\s+\[(?P<code>[^\]]+)\])?\s*$"
)

# Matches: Found 5 errors in 3 files (checked 12 source files)
_MYPY_SUMMARY_RE = re.compile(r"Found\s+(?P<count>\d+)\s+error[s]?\s+in\s+(?P<files>\d+)\s+file")


def parse_mypy_output(raw: str) -> ParsedOutput:
    """Parse mypy output into structured failures.

    Handles per-line error messages and the summary footer.
    Falls through gracefully on unparseable input.
    """
    result = ParsedOutput(tool="mypy")

    if not raw or not raw.strip():
        return result

    lines = raw.splitlines()

    for line in lines:
        stripped = line.strip()

        # Parse error lines
        m = _MYPY_ERROR_RE.match(stripped)
        if m:
            code = m.group("code") or ""
            result.failures.append(
                ParsedFailure(
                    file=m.group("file"),
                    line=int(m.group("line")),
                    rule_or_test=code,
                    message=m.group("message"),
                    code=code,
                )
            )
            continue

        # Parse summary line
        m = _MYPY_SUMMARY_RE.search(stripped)
        if m:
            result.raw_summary = stripped
            result.total_errors = int(m.group("count"))

    # Fallback total from parsed failures
    if result.total_errors == 0:
        result.total_errors = len(result.failures)

    # Fallback summary
    if not result.failures and not result.raw_summary:
        tail = lines[-3:] if len(lines) > 3 else lines
        result.raw_summary = "\n".join(tail)

    return result


# ---------------------------------------------------------------------------
# Ruff parser
# ---------------------------------------------------------------------------

# Matches: file.py:10:5: E501 Line too long (82 > 79)
#
# The rule slot is a RUFF-SHAPED code (letters then digits: E501, F401,
# PLR0913, RUF001), with the optional trailing colon pylint writes. It
# used to be a bare `\S+`, which matched any `file:line:col: message`
# line and put that message's first word in the rule slot. Measured on
# real `eslint --format unix` output, all three findings parsed with the
# rule recorded as `'cache'`, `Expected` and `'missingHelper'` - the real
# rule id sits at the END of an eslint line, and those slugs were fed
# to evolution.py as failure signatures. Requiring the shape both fixes
# that and keeps this parser out of the eslint parser's way when the
# auto path runs both over the same output (#258).
_RUFF_ERROR_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<rule>[A-Z]{1,6}\d{1,5}):?\s+(?P<message>.+)$"
)

# Matches: Found 12 errors.  /  Found 12 errors (8 fixed, 4 remaining).
_RUFF_SUMMARY_RE = re.compile(r"Found\s+(?P<count>\d+)\s+error")


def parse_ruff_output(raw: str) -> ParsedOutput:
    """Parse ruff output into structured failures.

    Handles per-line diagnostics and the summary footer.
    Falls through gracefully on unparseable input.
    """
    result = ParsedOutput(tool="ruff")

    if not raw or not raw.strip():
        return result

    lines = raw.splitlines()

    for line in lines:
        stripped = line.strip()

        # Parse error lines
        m = _RUFF_ERROR_RE.match(stripped)
        if m:
            result.failures.append(
                ParsedFailure(
                    file=m.group("file"),
                    line=int(m.group("line")),
                    rule_or_test=m.group("rule"),
                    message=m.group("message"),
                    code=m.group("rule"),
                )
            )
            continue

        # Parse summary line
        m = _RUFF_SUMMARY_RE.search(stripped)
        if m:
            result.raw_summary = stripped
            result.total_errors = int(m.group("count"))

    # Fallback total from parsed failures
    if result.total_errors == 0:
        result.total_errors = len(result.failures)

    # Fallback summary
    if not result.failures and not result.raw_summary:
        tail = lines[-3:] if len(lines) > 3 else lines
        result.raw_summary = "\n".join(tail)

    return result


# ---------------------------------------------------------------------------
# Source context helper
# ---------------------------------------------------------------------------


def add_source_context(failure: ParsedFailure, worktree_path: Path, context_lines: int = 3) -> None:
    """Read the source file and add surrounding lines to the failure.

    Reads `context_lines` above and below the failure line.
    Silently does nothing if the file cannot be read or the line is invalid.
    """
    if not failure.file or failure.line <= 0:
        return

    source_path = worktree_path / failure.file
    if not source_path.is_file():
        return

    try:
        file_lines = source_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return

    total = len(file_lines)
    if failure.line > total:
        return

    start = max(0, failure.line - 1 - context_lines)
    end = min(total, failure.line + context_lines)
    snippet_lines: list[str] = []
    for i in range(start, end):
        marker = ">" if i == failure.line - 1 else " "
        snippet_lines.append(f"{marker} {i + 1:4d} | {file_lines[i]}")

    failure.source_context = "\n".join(snippet_lines)


# ---------------------------------------------------------------------------
# Fix hint generator
# ---------------------------------------------------------------------------

# Each entry: (compiled regex matching the message, hint template)
# Use {m} in the template to interpolate the regex match object.
_HINT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Missing positional argument
    (
        re.compile(r"missing (\d+) required positional argument", re.IGNORECASE),
        "Check the function signature - a required argument is missing from the call.",
    ),
    # Too many arguments
    (
        re.compile(r"takes \d+ positional arguments? but \d+ (?:was|were) given", re.IGNORECASE),
        "Too many arguments passed - check the function signature for expected parameters.",
    ),
    # Optional type not handled (str | None assigned to str, etc.)
    (
        re.compile(
            r"Incompatible types in assignment.*"
            r"(?:Optional|None)",
            re.IGNORECASE,
        ),
        "The value can be None - add a None check or guard before using it.",
    ),
    (
        re.compile(r'has no attribute "([^"]+)"', re.IGNORECASE),
        "Attribute not found - check for typos or verify the object type.",
    ),
    # Import errors
    (
        re.compile(r"(?:No module named|cannot import name|ModuleNotFoundError)", re.IGNORECASE),
        "Import failed - verify the module is installed and the name is correct.",
    ),
    # Name not defined
    (
        re.compile(r"name '([^']+)' is not defined", re.IGNORECASE),
        "Undefined name - check for typos or add the missing import.",
    ),
    # Argument type mismatch
    (
        re.compile(
            r'Argument.*has incompatible type "([^"]+)".*expected "([^"]+)"',
            re.IGNORECASE,
        ),
        "Type mismatch in argument - convert or check the value before passing it.",
    ),
    # Return type mismatch
    (
        re.compile(r"Incompatible return value type", re.IGNORECASE),
        "Return type does not match the declared signature - fix the return value or annotation.",
    ),
    # Assert / comparison failures
    (
        re.compile(r"AssertionError|assert .+ == .+", re.IGNORECASE),
        "Assertion failed - check the expected vs actual values.",
    ),
    # Ruff: unused import
    (
        re.compile(r"F401", re.IGNORECASE),
        "Unused import - remove it or use it.",
    ),
    # Ruff: undefined name
    (
        re.compile(r"F821", re.IGNORECASE),
        "Undefined name - add the missing import or definition.",
    ),
    # Ruff: line too long
    (
        re.compile(r"E501", re.IGNORECASE),
        "Line too long - break it up or shorten the expression.",
    ),
]


def generate_fix_hint(failure: ParsedFailure) -> str:
    """Generate a fix hint for common error patterns.

    Uses pattern matching against the failure message and rule.
    Returns an empty string for uncommon patterns - no hint is better
    than a bad one.
    """
    # Combine message and rule for matching since ruff rules appear in rule_or_test
    text = f"{failure.message} {failure.rule_or_test}"

    for pattern, hint in _HINT_PATTERNS:
        if pattern.search(text):
            return hint

    return ""
