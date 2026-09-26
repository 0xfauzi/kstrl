"""#527: every failing row a logging gate builds carries the gate's output.

``_write_gate_logs`` (#462) writes ``CheckResult.output`` for each failed
check and skips a row whose output is ``None``. So a gate exit that builds its
failing row without ``output=`` writes no log, and nothing else notices: the
row still fails, the event still lists the failure, and the operator finds no
file. That is how the timeout and decode exits of the test, typecheck and lint
gates dropped the output ``run_scrubbed`` had captured (#527).

THE POPULATION is every scope in ``kstrl/`` that logs a gate's output: it
calls ``_failed_gate_result``, or it builds a ``CheckResult`` with an
``output`` keyword itself. Inside those scopes, every ``CheckResult`` whose
``passed`` is not the literal ``True`` is a row, keyed by the ``output``
argument VERBATIM, or ``MISSING`` when there is none. A new exit in a logging
gate is a new row, and a new logging gate brings its exits with it.

THIS GUARD FLAGS. A ``passed`` it cannot fold counts as failing, and an
``output`` passed positionally or through ``**kwargs`` renders as
``MISSING``: both over-match, which costs a census delta somebody reads.

THE BEHAVIOURAL HALF is ``tests/test_gate_output_logs.py``, which drives the
real gates through a timeout and an undecodable byte and reads the log on
disk. This file cannot tell whether an ``output=`` argument holds the right
text; that file cannot tell whether a new exit exists.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    all_nodes,
    blind_spot,
    label,
    leaf_name,
    package_sources,
    parse,
    parsed,
    scope_of,
)

#: The helper that builds the three gates' shared failing row.
GATE_HELPER = "_failed_gate_result"

#: The field ``_write_gate_logs`` reads.
OUTPUT = "output"

#: Every failing row built inside a logging gate, with its ``output``
#: argument verbatim.
#:
#: Each gate has three failing exits: the tool timed out, its output was not
#: utf-8, and it exited non-zero (built by ``_failed_gate_result``, whose row
#: is the first line). A ``MISSING`` here is a failing gate that writes no
#: log; ``test_no_failing_gate_row_drops_its_output`` refuses one whatever
#: this dict says.
EXPECTED_GATE_FAILURE_ROWS: dict[str, int] = {
    "verify.py: _failed_gate_result: output=bounded_gate_output(output)": 1,
    "verify.py: check_linter: output=_output_before_stop(exc.stdout, exc.stderr)": 1,
    "verify.py: check_linter: output=_output_before_stop(expired.stdout, expired.stderr)": 1,
    "verify.py: check_test_suite: output=_output_before_stop(exc.stdout, exc.stderr)": 1,
    "verify.py: check_test_suite: output=_output_before_stop(expired.stdout, expired.stderr)": 1,
    "verify.py: check_typecheck: output=_output_before_stop(exc.stdout, exc.stderr)": 1,
    "verify.py: check_typecheck: output=_output_before_stop(expired.stdout, expired.stderr)": 1,
}

#: Source the census MUST read as three rows in two logging gates, and none
#: in the third function. One logging gate is recognised by its call to the
#: helper, the other by its own ``output=``, so each half of the population
#: test is proved separately.
CONTROL = """
def gate_through_the_helper():
    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(name="x", passed=False, message="timed out")
    return _failed_gate_result("x", "m", parsed, cmd, cwd, start, output=text)

def gate_building_its_own_row():
    if broken:
        return CheckResult(name="y", passed=False, output=text)
    return verify.CheckResult(name="y", passed=ok)

def not_a_gate():
    return CheckResult(name="z", passed=False)
"""

CONTROL_ROWS = {
    "control: gate_through_the_helper: output=MISSING": 1,
    "control: gate_building_its_own_row: output=text": 1,
    "control: gate_building_its_own_row: output=MISSING": 1,
}


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _builds_a_check_result(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and leaf_name(node.func) == "CheckResult"


def _logs_a_gate(node: ast.AST) -> bool:
    """A node that makes its scope a logging gate."""
    if isinstance(node, ast.Call) and leaf_name(node.func) == GATE_HELPER:
        return True
    return (
        _builds_a_check_result(node)
        and isinstance(node, ast.Call)
        and _keyword(node, OUTPUT) is not None
    )


def _passes(node: ast.Call) -> bool:
    """Only a literal ``passed=True`` is a passing row; anything else counts."""
    passed = _keyword(node, "passed")
    if passed is None and len(node.args) >= 2:
        passed = node.args[1]
    return isinstance(passed, ast.Constant) and passed.value is True


def gate_failure_rows(tree: ast.Module, where: str) -> dict[str, int]:
    """The census for one module: ``{"<where>: <scope>: output=<arg>": n}``."""
    owner = scope_of(tree)
    nodes = all_nodes(tree)
    gates = {owner[id(node)] for node in nodes if _logs_a_gate(node)}
    rows: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, ast.Call) or not _builds_a_check_result(node):
            continue
        if owner[id(node)] not in gates or _passes(node):
            continue
        argument = _keyword(node, OUTPUT)
        rendered = ast.unparse(argument) if argument is not None else "MISSING"
        row = f"{where}: {owner[id(node)]}: output={rendered}"
        rows[row] = rows.get(row, 0) + 1
    return rows


def _package_rows() -> dict[str, int]:
    corpus: list[Path] = package_sources()
    assert corpus, "no kstrl/ sources were found, so the census below is about nothing"
    found: dict[str, int] = {}
    for source_file in corpus:
        for row, count in gate_failure_rows(parsed(source_file), label(source_file)).items():
            found[row] = found.get(row, 0) + count
    return found


class TestEveryFailingGateRowCarriesItsOutput:
    def test_the_census_reads_the_control(self) -> None:
        """Without this, an empty census is also what a switched-off walk returns."""
        assert gate_failure_rows(parse(CONTROL), "control") == CONTROL_ROWS

    def test_the_failing_gate_rows_are_the_ones_pinned(self) -> None:
        found = _package_rows()
        assert found == EXPECTED_GATE_FAILURE_ROWS, (
            "The set of failing rows built inside a gate that logs its output "
            "moved. Each row is an exit the operator reads a log for: pass the "
            "output the gate captured, as the timeout and decode exits do with "
            f"_output_before_stop (#527). Found: {found}"
        )

    def test_no_failing_gate_row_drops_its_output(self) -> None:
        """The rule, stated apart from the pin so a pin edit cannot waive it."""
        dropped = sorted(
            row
            for row in _package_rows()
            if row.endswith(": output=MISSING") or row.endswith(": output=None")
        )
        assert dropped == [], (
            "A gate exit builds a failing row with no output, so "
            "_write_gate_logs writes no log for it and the event names no file "
            f"(#527): {dropped}"
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_gate_that_never_logs_is_outside_the_population(self) -> None:
        """Disclosed limit. A check that runs a tool and never passes
        ``output=`` on any exit is not a logging gate, so its failing rows
        are not counted: ``check_dead_code`` and the fixtures check are two
        today, and their failures write no log on any exit, not only on a
        timeout. Making them log is a change to what #462 covers, not a
        wider walk. The day the walk counts them, this XPASSes."""
        blind_spot(
            lambda source: gate_failure_rows(parse(source), "probe"),
            "def check_new():\n"
            "    result = run_scrubbed(cmd, cwd=cwd, timeout=t)\n"
            "    return CheckResult(name='n', passed=False)\n",
        )
