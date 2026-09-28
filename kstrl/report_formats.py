"""Gate reports in a declared machine format, read into the parse types (#629).

A gate normally learns what failed by running text parsers over its output
(``kstrl.gateparse``). A toolchain with no text parser, such as ``go test``,
reached the engineer only as the #622 excerpt. Most toolchains can write a
machine format instead, and this module reads one when the operator declares
it: ``[verify] test_tool = "go-test-json"``.

The command learns where to write from the ``KSTRL_REPORT`` environment
variable, which kstrl sets for that one command to a path in a fresh
directory outside the worktree, deleted when the gate is done. So a report
from an earlier run cannot be read as this run's, and the variable never comes
from the operator's shell: ``verify.run_scrubbed`` does not admit it.

Every way the read can fail is a :class:`ReportRefusal`, never an empty parse.
A missing report is refused, a report that does not parse is refused whole,
and a report that names no failed test on a failing exit is refused, because
each of those would otherwise read as "the tool found nothing".

Slice 1 of #629 holds two owner decisions at their most conservative setting.
The report is read only when the command exited non-zero, and the go reader
leaves ``recognised`` False, so a go row never becomes measured and no
baseline signature can be cleared by it.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kstrl.failure_excerpt import inside_worktree
from kstrl.gateparse import GATE_FORMATS
from kstrl.jsonread import read_json
from kstrl.parsers import ParsedFailure, ParsedOutput

#: The environment variable a gate command writes its report to.
REPORT_ENV = "KSTRL_REPORT"

#: The report's file name inside the fresh directory.
_REPORT_NAME = "report"

# go prints a test's location relative to its PACKAGE directory, indented:
# "    pricing_test.go:7: BulkPercent(20) = 11, want 10". With -fullpath the
# path is absolute.
_GO_LOCATION_RE = re.compile(r"^\s+(?P<path>\S+\.go):(?P<line>\d+): (?P<message>.*)$")

# go's own framing lines around a test's output: "=== RUN", "=== PAUSE",
# "=== CONT", "=== NAME", "--- FAIL", "--- PASS", "--- SKIP".
_GO_FRAMING = ("=== ", "--- ")

_GO_MODULE_RE = re.compile(r"^module\s+(?P<module>\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ReportRefusal:
    """Why a declared report could not be read.

    ``missing`` separates "the command wrote no report" from "it wrote one
    kstrl cannot read"; the gate turns the two into different ``NotMeasured``
    reasons. ``detail`` is prose for a human and is never parsed.
    """

    missing: bool
    detail: str


class _Unreadable(ValueError):
    """A report line that is not a go test event, with its line number."""


def _go_events(raw: bytes) -> list[dict[str, object]]:
    """Every event in a ``go test -json`` report, or ``_Unreadable``.

    One bad line rejects the whole report: a report half of which parsed is
    not a report of what failed.
    """
    events: list[dict[str, object]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = read_json(line)
        except Exception as exc:
            # The parser owns its taxonomy (CLAUDE.md). read_json turns all
            # of it, bytes that are not utf-8 included, into JSONDecodeError;
            # this clause does not depend on that staying true.
            raise _Unreadable(f"line {number}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("Action"), str):
            raise _Unreadable(f'line {number}: not a go test event (no string "Action")')
        for key in ("Test", "Output", "Package"):
            if key in event and not isinstance(event[key], str):
                raise _Unreadable(f'line {number}: "{key}" is not a string')
        events.append(event)
    return events


def _go_module(root: Path) -> str:
    """The module path in ``root/go.mod``, or "" when there is none."""
    try:
        text = (root / "go.mod").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    match = _GO_MODULE_RE.search(text)
    return match.group("module") if match else ""


def _package_dir(package: str, module: str) -> str | None:
    """``package``'s directory relative to the module root, or None."""
    if not module:
        return None
    if package == module:
        return ""
    if package.startswith(module + "/"):
        return package[len(module) + 1 :]
    return None


def _locate(
    failure: ParsedFailure, match: re.Match[str], directory: str | None, root: Path
) -> None:
    """Point ``failure`` at the location go printed, when it is in the worktree.

    A path outside the worktree is dropped and the message kept, the #622
    rule. A relative path whose package directory is unknown keeps its
    printed location in the message, since it names no path outside.
    """
    path, line, message = match.group("path"), int(match.group("line")), match.group("message")
    if Path(path).is_absolute():
        if inside_worktree(path, root):
            failure.file, failure.line = os.path.relpath(path, os.path.abspath(root)), line
        failure.message = message
    elif directory is not None:
        failure.file = str(Path(directory) / path) if directory else path
        failure.line = line
        failure.message = message
    else:
        failure.message = f"{path}:{line}: {message}"


def _go_failure(
    test: str, package: str, output: list[str], module: str, root: Path
) -> ParsedFailure:
    """One failed test, located by the first location line it printed."""
    failure = ParsedFailure(rule_or_test=test)
    for text in output:
        match = _GO_LOCATION_RE.match(text.rstrip("\n"))
        if match:
            _locate(failure, match, _package_dir(package, module), root)
            return failure
    said = [t.strip() for t in output if t.strip() and not t.strip().startswith(_GO_FRAMING)]
    failure.message = said[0] if said else ""
    return failure


def read_go_test_json(raw: bytes, root: Path) -> ParsedOutput:
    """A ``go test -json`` report as a parse: one failure per failed test.

    Raises ``_Unreadable`` for a report that is not go test events.
    ``recognised`` stays False (slice 1 of #629), so the row is unmeasured.
    """
    events = _go_events(raw)
    module = _go_module(root)
    output: dict[tuple[str, str], list[str]] = {}
    failures: list[ParsedFailure] = []
    for event in events:
        test = event.get("Test")
        if not isinstance(test, str):
            continue
        package = str(event.get("Package", ""))
        if event["Action"] == "output":
            output.setdefault((package, test), []).append(str(event.get("Output", "")))
        elif event["Action"] == "fail":
            failures.append(
                _go_failure(test, package, output.get((package, test), []), module, root)
            )
    return ParsedOutput(
        tool="go-test-json",
        total_errors=len(failures),
        failures=failures,
        raw_summary=f"{len(events)} events, {len(failures)} failed tests",
    )


#: Every format a gate can declare, whichever gate declares it.
FORMATS = frozenset(fmt for formats in GATE_FORMATS.values() for fmt in formats)


def _read(tool: str, raw: bytes, root: Path) -> ParsedOutput:
    """The reader for ``tool``, called directly rather than through a table.

    A format declared in ``GATE_FORMATS`` with no reader here raises, and
    :func:`read_gate_report` refuses the report, so it can never be read by
    the wrong reader or pass unread.
    """
    if tool == "go-test-json":
        return read_go_test_json(raw, root)
    raise ValueError(f"kstrl has no reader for the declared format {tool!r}")


@contextmanager
def fresh_report(tool: str | None) -> Iterator[Path | None]:
    """A report path nothing has written yet, or None when ``tool`` is not a format.

    The path is in a new directory outside the worktree, deleted on exit, so
    no earlier run's report can be at it.
    """
    if tool not in FORMATS:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="kstrl-report-") as tmp:
        yield Path(tmp) / _REPORT_NAME


def declared_formats(tools: Mapping[str, str | None]) -> dict[str, str]:
    """The gates in ``tools`` (gate -> configured tool) that declare a format."""
    return {
        gate: tool for gate, tool in tools.items() if tool and tool in GATE_FORMATS.get(gate, ())
    }


def read_gate_report(tool: str, report: Path, root: Path) -> ParsedOutput | ReportRefusal:
    """The failures a failing gate's report names, or why it cannot be read.

    Called on a non-zero exit only. The bytes are read outside the parse
    guard, so an ``OSError`` is the missing report and nothing else.
    """
    try:
        raw = report.read_bytes()
    except OSError:
        return ReportRefusal(
            missing=True,
            detail=(
                f'test_tool = "{tool}" but the command wrote no report to ${REPORT_ENV}; '
                f'write it with > "${{{REPORT_ENV}:-/dev/null}}"'
            ),
        )
    try:
        parsed = _read(tool, raw, root)
    except Exception as exc:
        # A reader's own taxonomy (CLAUDE.md): anything it raises refuses
        # the whole report.
        return ReportRefusal(
            missing=False, detail=f"the {tool} report at ${REPORT_ENV} could not be read: {exc}"
        )
    if not parsed.failures:
        return ReportRefusal(
            missing=False,
            detail=(
                f"the {tool} report at ${REPORT_ENV} names no failed test, but the command failed"
            ),
        )
    return parsed
