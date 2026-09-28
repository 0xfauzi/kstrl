"""Which test files the test gate actually ran (#620).

The test gate runs the project's own test command, and WHICH tests that
command runs is decided by files the engineer can edit: ``pytest.ini`` or
``pyproject.toml`` addopts, ``conftest.py``, the ``package.json`` test
script. Measured on #620: one ``addopts = --ignore=tests/test_bulk.py``
line, or a ``package.json`` test script narrowed to one file, left a
failing test in the tree and every Phase 1 row passing. The gate's exit
status cannot show that, because the command decides what it measures.

So the gate also reads which test files ran, from the runner's own report,
and names every test file the diff added or changed that did not run. The
runner is asked for that report by kstrl, not by the project, so a project
config edit cannot hide it.

Two readers, for the two test runners kstrl already parses
(``kstrl.gateparse.GATE_TOOLS``):

- pytest. ``PYTEST_ADDOPTS`` asks for a junit report in a directory kstrl
  owns. pytest applies ``PYTEST_ADDOPTS`` AFTER the ini ``addopts``, so a
  project's own ``--junitxml`` cannot redirect it. ``junit_family=xunit1``
  is the family whose ``testcase`` carries a ``file`` attribute.
- vitest. vitest reads no environment variable that adds a reporter
  (measured on vitest 3.2: the only ``VITEST_*`` names it reads are pool
  settings), so its per-file lines are read from the gate's own output:
  `` ✓ src/a.test.ts (2 tests) 1ms`` from the default reporter and
  `` ✓ src/a.test.ts > name 1ms`` from the verbose one.

A test file whose runner left nothing readable is NOT MEASURED, never a
pass: the caller names it in a :class:`kstrl.verify.NotMeasured` record.
A test file matches by the runners' DEFAULT naming only (below), so a
project that configures other names (``python_files = check_*.py``) is
not judged on those files. That is a miss in the skip direction and it is
stated here rather than left implicit.
"""

from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from kstrl.parsers import strip_ansi

#: The ``check`` of the :class:`kstrl.verify.NotMeasured` record this
#: module's caller writes when a changed test file's runner left nothing
#: kstrl can read.
TESTS_RAN_CHECK = "tests_ran"

PYTEST = "pytest"
VITEST = "vitest"

#: The files each runner collects under its DEFAULT configuration:
#: pytest's ``python_files = test_*.py *_test.py`` and vitest's
#: ``include: **/*.{test,spec}.?(c|m)[jt]s?(x)``.
TEST_FILE_PATTERNS: dict[str, re.Pattern[str]] = {
    PYTEST: re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$"),
    VITEST: re.compile(r"(^|/)[^/]+\.(test|spec)\.[cm]?[jt]sx?$"),
}

PYTEST_REPORT = "pytest-junit.xml"
GATE_OUTPUT = "gate-output.txt"

# One file line per test file (default reporter) or per test (verbose).
# The failure-block location line, "❯ src/a.test.ts:3:49", has no "(" or
# ">" after the path and so never matches.
_VITEST_FILE_RE = re.compile(r"^\s*[✓×❯↓]\s+(?P<file>\S+)\s+(?:\(\d+ tests?\b|>\s)")
_VITEST_NO_FILES = "No test files found"


def runner_for(path: str) -> str | None:
    """The runner whose default naming ``path`` matches, or None."""
    for kind, pattern in TEST_FILE_PATTERNS.items():
        if pattern.search(path):
            return kind
    return None


def inventory_env(report_dir: Path | None) -> dict[str, str] | None:
    """The environment that makes pytest report into ``report_dir``."""
    if report_dir is None:
        return None
    report = shlex.quote(str(report_dir / PYTEST_REPORT))
    return {"PYTEST_ADDOPTS": f"--junitxml={report} -o junit_family=xunit1"}


def save_gate_output(report_dir: Path | None, output: str) -> None:
    """Keep the gate's output for :func:`vitest_files`.

    A write that fails leaves the file absent, which the reader reports
    as not measured, so nothing here is silent.
    """
    if report_dir is None:
        return
    try:
        (report_dir / GATE_OUTPUT).write_text(output, encoding="utf-8")
    except OSError:
        return


def pytest_files(report_dir: Path) -> set[str] | None:
    """Every ``file`` in pytest's junit report, or None when there is none.

    The bytes are read outside the parse guard, and the parse catches
    ``Exception``: expat raises ``ParseError`` for malformed XML and a
    plain ``ValueError`` for an encoding declaration it does not support,
    so the parser owns its error taxonomy (CLAUDE.md).
    """
    try:
        raw = (report_dir / PYTEST_REPORT).read_bytes()
    except OSError:
        return None
    try:
        root = ET.fromstring(raw)
    except Exception:
        return None
    return {case.get("file") or "" for case in root.iter("testcase")} - {""}


def vitest_files(report_dir: Path) -> set[str] | None:
    """Every test file vitest printed a line for, or None when it printed
    nothing kstrl can read (another reporter, or no vitest at all)."""
    try:
        text = (report_dir / GATE_OUTPUT).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    lines = strip_ansi(text).splitlines()
    files = {match.group("file") for line in lines if (match := _VITEST_FILE_RE.match(line))}
    if not files and not any(_VITEST_NO_FILES in line for line in lines):
        return None
    return files


def _ran(path: str, files: set[str]) -> bool:
    """Whether ``path`` (repository-relative) is one of ``files``.

    A runner reports paths relative to its own root, which is a
    subdirectory when the command is ``cd web && npm test``, so a suffix
    at a directory boundary matches too.
    """
    return any(path == name or path.endswith("/" + name) for name in files)


def unrun_test_files(changed: Sequence[str], report_dir: Path) -> tuple[list[str], list[str]]:
    """``(did_not_run, not_measured)`` over ``changed`` test files.

    A file lands in ``not_measured`` when its runner left no report kstrl
    can read, and in ``did_not_run`` when the report was read and does
    not name it. Every file is compared by its path, never by a count: a
    count lets one excluded file be swapped for a new trivial one.
    """
    reports = {PYTEST: pytest_files(report_dir), VITEST: vitest_files(report_dir)}
    did_not_run: list[str] = []
    not_measured: list[str] = []
    for path in changed:
        files = reports.get(runner_for(path) or "")
        if files is None:
            not_measured.append(path)
        elif not _ran(path, files):
            did_not_run.append(path)
    return did_not_run, not_measured
