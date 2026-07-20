"""Structured output parsers for test runners, type checkers, and linters.

Transforms raw CLI output into structured failure objects that can generate
LLM-optimized context for retry prompts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedFailure:
    """A single structured failure from a tool's output."""

    file: str = ""
    line: int = 0
    rule_or_test: str = ""  # test name, error code, or rule ID
    message: str = ""
    source_context: str = ""  # relevant source lines if available
    fix_hint: str = ""  # generated fix suggestion


@dataclass
class ParsedOutput:
    """Structured parse result from a tool's raw output."""

    tool: str  # "pytest", "mypy", "ruff"
    total_errors: int = 0
    failures: list[ParsedFailure] = field(default_factory=list)
    raw_summary: str = ""  # last line(s) summary from the tool

    def format_for_prompt(
        self, max_failures: int = 10, include_source: bool = True
    ) -> list[str]:
        """Format failures as structured lines optimized for LLM consumption.

        Returns list of detail strings suitable for CheckResult.details.
        """
        lines: list[str] = []

        if self.raw_summary:
            lines.append(f"[{self.tool}] {self.raw_summary}")

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
_PYTEST_SUMMARY_RE = re.compile(
    r"=+\s+(?P<summary>.+?)\s+=+\s*$"
)

# Extract failure count from summary like "3 failed"
_PYTEST_FAILED_COUNT_RE = re.compile(r"(\d+)\s+failed")
_PYTEST_ERROR_COUNT_RE = re.compile(r"(\d+)\s+error")


def parse_pytest_output(raw: str) -> ParsedOutput:
    """Parse pytest output into structured failures.

    Handles FAILED lines, ERROR lines, and the summary footer.
    Falls through gracefully on unparseable input.
    """
    result = ParsedOutput(tool="pytest")

    if not raw or not raw.strip():
        result.raw_summary = raw.strip() if raw else ""
        return result

    lines = raw.splitlines()
    in_short_summary = False

    for line in lines:
        stripped = line.strip()

        # Detect short test summary section
        if "short test summary info" in stripped.lower():
            in_short_summary = True
            continue

        # End of short summary section (next separator line)
        if in_short_summary and stripped.startswith("=") and stripped.endswith("="):
            in_short_summary = False
            # This is likely the final summary line - fall through to summary match

        # Parse FAILED lines (appear in short summary or standalone)
        m = _PYTEST_FAILED_RE.match(stripped)
        if m:
            result.failures.append(ParsedFailure(
                file=m.group("file"),
                rule_or_test=m.group("test"),
                message=m.group("message") or "",
            ))
            continue

        # Parse ERROR lines
        m = _PYTEST_ERROR_RE.match(stripped)
        if m:
            result.failures.append(ParsedFailure(
                file=m.group("file"),
                rule_or_test=m.group("test") or "collection",
                message=m.group("message") or "",
            ))
            continue

        # Parse summary line
        m = _PYTEST_SUMMARY_RE.match(stripped)
        if m:
            result.raw_summary = m.group("summary").strip()

    # Extract total error count from summary
    if result.raw_summary:
        failed_m = _PYTEST_FAILED_COUNT_RE.search(result.raw_summary)
        error_m = _PYTEST_ERROR_COUNT_RE.search(result.raw_summary)
        count = 0
        if failed_m:
            count += int(failed_m.group(1))
        if error_m:
            count += int(error_m.group(1))
        result.total_errors = count
    else:
        result.total_errors = len(result.failures)

    # Fallback: if we parsed nothing useful, preserve raw tail as summary
    if not result.failures and not result.raw_summary:
        tail = lines[-5:] if len(lines) > 5 else lines
        result.raw_summary = "\n".join(tail)

    return result


# ---------------------------------------------------------------------------
# Mypy parser
# ---------------------------------------------------------------------------

# Matches: file.py:10: error: Incompatible types [assignment]
_MYPY_ERROR_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):\s+error:\s+(?P<message>.+?)(?:\s+\[(?P<code>[^\]]+)\])?\s*$"
)

# Matches: Found 5 errors in 3 files (checked 12 source files)
_MYPY_SUMMARY_RE = re.compile(
    r"Found\s+(?P<count>\d+)\s+error[s]?\s+in\s+(?P<files>\d+)\s+file"
)


def parse_mypy_output(raw: str) -> ParsedOutput:
    """Parse mypy output into structured failures.

    Handles per-line error messages and the summary footer.
    Falls through gracefully on unparseable input.
    """
    result = ParsedOutput(tool="mypy")

    if not raw or not raw.strip():
        result.raw_summary = raw.strip() if raw else ""
        return result

    lines = raw.splitlines()

    for line in lines:
        stripped = line.strip()

        # Parse error lines
        m = _MYPY_ERROR_RE.match(stripped)
        if m:
            result.failures.append(ParsedFailure(
                file=m.group("file"),
                line=int(m.group("line")),
                rule_or_test=m.group("code") or "",
                message=m.group("message"),
            ))
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
_RUFF_ERROR_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<rule>\S+)\s+(?P<message>.+)$"
)

# Matches: Found 12 errors.  /  Found 12 errors (8 fixed, 4 remaining).
_RUFF_SUMMARY_RE = re.compile(
    r"Found\s+(?P<count>\d+)\s+error"
)


def parse_ruff_output(raw: str) -> ParsedOutput:
    """Parse ruff output into structured failures.

    Handles per-line diagnostics and the summary footer.
    Falls through gracefully on unparseable input.
    """
    result = ParsedOutput(tool="ruff")

    if not raw or not raw.strip():
        result.raw_summary = raw.strip() if raw else ""
        return result

    lines = raw.splitlines()

    for line in lines:
        stripped = line.strip()

        # Parse error lines
        m = _RUFF_ERROR_RE.match(stripped)
        if m:
            result.failures.append(ParsedFailure(
                file=m.group("file"),
                line=int(m.group("line")),
                rule_or_test=m.group("rule"),
                message=m.group("message"),
            ))
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


def add_source_context(
    failure: ParsedFailure, worktree_path: Path, context_lines: int = 3
) -> None:
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
