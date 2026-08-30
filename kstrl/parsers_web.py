"""Output parsers for the JavaScript and TypeScript toolchains (#258).

kstrl's verify gates run whatever command the operator configured, and a
polyglot repo configures a chain: ``uv run pytest && (cd web && npm run
test)``. Before this module the three gates parsed every command's output
as pytest, mypy or ruff, so a real failing vitest run yielded ZERO parsed
failures - file, line, test name, assertion message and the
expected-versus-received diff were all present in the raw text and all
dropped.

Every regex here was written against output captured from the real tool
and checked in under ``tests/tool_output/``, never from documentation or
memory. Two of the shapes the issue predicted turned out to be wrong when
measured, and the fixtures are what caught it:

- ``tsc`` was expected to emit ``file:line:col: message`` like mypy. It
  emits ``src/broken.ts(5,7): error TS2322: ...`` with PARENTHESES
  through a pipe, and ``src/broken.ts:5:7 - error TS2322: ...`` under
  ``--pretty``. Neither matches the mypy or the ruff pattern; measured,
  all three existing parsers returned 0 failures on real tsc output.
- ``eslint --format unix`` DID match the old ruff pattern, three of three
  with the right file and line, but the rule slot took the message's
  first word (``'cache'``) and left the real rule id buried at the end of
  the line. ``--format compact`` did not parse at all. Both are moot for
  most repos anyway: eslint 9 removed the two formatters from core, and
  the default ``stylish`` output is what ``npm run lint`` actually
  prints, so all three shapes are handled here.

Adding another toolchain is one parse function, one entry in
``kstrl.gateparse.TOOL_PARSERS`` and ``GATE_TOOLS``, and one captured
fixture. A JavaScript or TypeScript tool (jest, biome, oxlint) belongs
here; a tool from another ecosystem (cargo, go test) belongs in its own
``kstrl/parsers_<ecosystem>.py`` importing the same shared types from
``kstrl.parsers``. The split is by ecosystem rather than by gate because
``kstrl.parsers`` is over half the 800-line ceiling the repo's
file-length ratchet enforces, and because a toolchain's formats change
together.

Two invariants a new parser owes the dispatcher, both enforced by tests
in ``tests/test_gateparse.py`` rather than by convention:

- Output it does not recognise must still leave ``raw_summary``
  populated, because ``gateparse`` returns the gate's PRIMARY parser
  when nothing matched and that summary is then the whole retry detail.
- It must not match another parser on the same gate. Auto unions without
  deduplicating, so an overlap doubles the failures the engineer sees.
"""

from __future__ import annotations

import re

from kstrl.parsers import FAILED_COUNT_RE, ParsedFailure, ParsedOutput, exception_code

# ---------------------------------------------------------------------------
# Vitest
# ---------------------------------------------------------------------------

# The header of one failure block under "Failed Tests" / "Failed Suites":
#
#   FAIL  tests/failing.test.ts > word counter > counts the words
#   FAIL  tests/broken-import.test.ts [ tests/broken-import.test.ts ]
#
# The tail is captured whole rather than alternated over, because the two
# shapes mean different things: "> ..." is the suite-and-test chain, and
# "[ ... ]" is a suite that never loaded, which HAS no test name.
_VITEST_FAIL_RE = re.compile(r"^FAIL\s+(?P<file>\S+)(?P<rest>.*)$")

# The location line inside a block: "❯ tests/failing.test.ts:5:41", or
# "❯ loadAndTransform node_modules/vite/dist/...js:51969:17" when the
# frame is somebody else's code. Only "❯" is accepted as the marker
# because that is the character measured in vitest 2.1.9's output.
_VITEST_LOC_RE = re.compile(r"^❯\s+.*?(?P<file>[^\s:]+):(?P<line>\d+):\d+\s*$")

# The footer rows: "Test Files  2 failed (2)" and
# "Tests  2 failed | 3 passed (5)". Two or more spaces after the label is
# the column separator vitest pads with.
_VITEST_SUMMARY_RE = re.compile(r"^(?:Test Files|Tests)\s{2,}\S")

# Lines that open their own section rather than describing the failure
# above them. Used to find a block's message line.
_VITEST_MARKERS = ("FAIL", "❯", "⎯")


def _vitest_message(block: list[str]) -> str:
    """The assertion or load error line of one FAIL block.

    Measured, it is always the line directly under the FAIL header
    ("AssertionError: expected 3 to be 4", "Error: Failed to load url
    ..."), but blank lines and section rules are skipped so a version
    that spaces its blocks out does not lose the message silently.
    """
    for line in block[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith(_VITEST_MARKERS):
            return stripped
    return ""


def _vitest_line_number(block: list[str], file: str) -> int:
    """Line number from the first frame pointing at the FAIL block's own file.

    The file is compared rather than the first frame taken: a suite that
    failed to load reports its top frame inside ``node_modules/vite``,
    and sending the engineer to a line in vite's bundle is worse than
    sending it no line at all.
    """
    for line in block:
        m = _VITEST_LOC_RE.match(line.strip())
        if m and m.group("file") == file:
            return int(m.group("line"))
    return 0


def _vitest_failure(block: list[str]) -> ParsedFailure:
    """One FAIL block as a structured failure."""
    head = _VITEST_FAIL_RE.match(block[0].strip())
    assert head is not None  # only called on lines that already matched
    file = head.group("file")
    rest = head.group("rest").strip()
    message = _vitest_message(block)
    return ParsedFailure(
        file=file,
        line=_vitest_line_number(block, file),
        rule_or_test=rest[1:].strip() if rest.startswith(">") else "",
        message=message,
        code=exception_code(message),
    )


def parse_vitest_output(raw: str) -> ParsedOutput:
    """Parse vitest output into structured failures."""
    result = ParsedOutput(tool="vitest")

    if not raw or not raw.strip():
        return result

    lines = raw.splitlines()
    # A failure's detail runs from its FAIL header to the next one, so
    # the blocks are the gaps between the headers.
    starts = [i for i, line in enumerate(lines) if _VITEST_FAIL_RE.match(line.strip())]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        result.failures.append(_vitest_failure(lines[start:end]))

    summary = [line.strip() for line in lines if _VITEST_SUMMARY_RE.match(line.strip())]
    result.raw_summary = "\n".join(summary)
    if not result.failures and not result.raw_summary:
        result.raw_summary = "\n".join(lines[-5:])

    count_m = FAILED_COUNT_RE.search(result.raw_summary)
    result.total_errors = len(result.failures) or (int(count_m.group(1)) if count_m else 0)
    return result


# ---------------------------------------------------------------------------
# TypeScript compiler
# ---------------------------------------------------------------------------

# Through a pipe, which is how the gate always runs it:
#   src/broken.ts(7,9): error TS2322: Type 'string' is not assignable ...
_TSC_PLAIN_RE = re.compile(
    r"^(?P<file>[^\s(]+)\((?P<line>\d+),\d+\):\s+"
    r"error\s+(?P<code>TS\d+):\s+(?P<message>.+)$"
)

# Under --pretty, which an npm script may well pass:
#   src/broken.ts:7:9 - error TS2322: Type 'string' is not assignable ...
_TSC_PRETTY_RE = re.compile(
    r"^(?P<file>[^\s:]+):(?P<line>\d+):\d+\s+-\s+"
    r"error\s+(?P<code>TS\d+):\s+(?P<message>.+)$"
)

# --pretty only; the piped form prints no footer at all.
_TSC_SUMMARY_RE = re.compile(r"^Found\s+\d+\s+error")


def parse_tsc_output(raw: str) -> ParsedOutput:
    """Parse TypeScript compiler diagnostics into structured failures."""
    result = ParsedOutput(tool="tsc")

    if not raw or not raw.strip():
        return result

    lines = raw.splitlines()
    for line in lines:
        stripped = line.strip()
        m = _TSC_PLAIN_RE.match(stripped) or _TSC_PRETTY_RE.match(stripped)
        if m:
            result.failures.append(
                ParsedFailure(
                    file=m.group("file"),
                    line=int(m.group("line")),
                    rule_or_test=m.group("code"),
                    message=m.group("message"),
                    code=m.group("code"),
                )
            )
            continue
        if _TSC_SUMMARY_RE.match(stripped):
            result.raw_summary = stripped

    if not result.failures and not result.raw_summary:
        result.raw_summary = "\n".join(lines[-3:])

    result.total_errors = len(result.failures)
    return result


# ---------------------------------------------------------------------------
# ESLint
# ---------------------------------------------------------------------------

# stylish (the default, and unchanged between eslint 8 and 9): a bare
# file path, then indented diagnostics padded into columns.
#
#   /repo/lint/draft.js
#     4:7   error    'cache' is assigned a value but never used  no-unused-vars
#
# The rule is optional because a fatal parse error has no rule id.
_ESLINT_STYLISH_RE = re.compile(
    r"^(?P<line>\d+):\d+\s+(?:error|warning)\s+"
    r"(?P<message>.+?)(?:\s{2,}(?P<rule>[\w@/-]+))?$"
)

# --format unix: /repo/lint/draft.js:4:7: 'cache' is ... [Error/no-unused-vars]
#
# The file group is `[^:]+`, not `.+?`. Lazy-prefix versions of these two
# patterns retry at every offset where `:digits:digits:` follows and then
# rescan the rest of the line, which is quadratic in line length:
# measured on one synthetic line of repeated `x:1:1: `, 23 ms at 7 KB,
# 368 ms at 28 KB, 1475 ms at 56 KB. Gate output is whatever the
# operator's command wrote, uncapped, and the lint gate now runs this
# parser on every failure whether eslint was involved or not. `[^:]+`
# is linear (0.37 ms on the same 56 KB line) and costs one POSIX
# assumption: no colon in the path.
_ESLINT_UNIX_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):\d+:\s+(?P<message>.+?)"
    r"\s+\[(?:Error|Warning)/(?P<rule>[^\]]*)\]$"
)

# --format compact: /repo/lint/draft.js: line 4, col 7, Error - msg (rule)
_ESLINT_COMPACT_RE = re.compile(
    r"^(?P<file>[^:]+):\s+line\s+(?P<line>\d+),\s+col\s+\d+,\s+"
    r"(?:Error|Warning)\s+-\s+(?P<message>.+?)(?:\s+\((?P<rule>[^)]*)\))?$"
)

# stylish's footer: "✖ 5 problems (4 errors, 1 warning)". unix and
# compact print a bare "5 problems".
_ESLINT_SUMMARY_RE = re.compile(r"^.?\s*\d+\s+problems?\b")


def _eslint_failure(stripped: str, current_file: str) -> ParsedFailure | None:
    """One eslint diagnostic in any of the three formats, or None.

    ``current_file`` carries the stylish file header down to the indented
    rows beneath it; the other two formats name the file on every line
    and ignore it.
    """
    for pattern in (_ESLINT_UNIX_RE, _ESLINT_COMPACT_RE, _ESLINT_STYLISH_RE):
        m = pattern.match(stripped)
        if not m:
            continue
        rule = (m.group("rule") or "").strip()
        return ParsedFailure(
            file=m.groupdict().get("file") or current_file,
            line=int(m.group("line")),
            rule_or_test=rule,
            message=m.group("message").strip(),
            code=rule,
        )
    return None


def _eslint_scan(lines: list[str]) -> tuple[list[ParsedFailure], str]:
    """Walk eslint output once, returning its failures and its footer."""
    failures: list[ParsedFailure] = []
    summary = ""
    current_file = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _ESLINT_SUMMARY_RE.match(stripped):
            summary = stripped
            continue
        failure = _eslint_failure(stripped, current_file)
        if failure is not None:
            failures.append(failure)
            continue
        # An unindented, undiagnosed line is stylish's file header. Any
        # other stray line simply becomes a header nothing attaches to.
        if line[:1].strip():
            current_file = stripped
    return failures, summary


def parse_eslint_output(raw: str) -> ParsedOutput:
    """Parse eslint output into structured failures.

    Warnings are kept alongside errors. eslint's exit code is what failed
    the gate and ``--max-warnings 0`` makes a warning do exactly that, so
    dropping them would hand the engineer an empty parse for a gate that
    genuinely failed.
    """
    result = ParsedOutput(tool="eslint")

    if not raw or not raw.strip():
        return result

    lines = raw.splitlines()
    result.failures, result.raw_summary = _eslint_scan(lines)
    if not result.failures and not result.raw_summary:
        result.raw_summary = "\n".join(lines[-3:])

    result.total_errors = len(result.failures)
    return result
