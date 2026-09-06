"""Every ``CheckResult`` in ``kstrl/`` says whether it MEASURED anything.

``CheckResult.measured`` (#227) is what stops a sensor's silence reading as a
fix: a row that timed out, whose tool is missing, or that passed vacuously
contributes no signature to a sense baseline, and its absence from a later run
lands in the dampener's ``unmeasured`` bucket instead of ``fixed``.

The field defaults to True, which means the failure mode is a NEW row that
should have been False and nobody noticing. Round 1 of review on #357 measured
exactly that: all six ``measured=False`` lines were deleted in one edit and the
full suite stayed green at 5811 passed. That is this repository's most-repeated
defect class, in its usual direction, and a per-site assertion cannot close it
because it says nothing about the site nobody wrote yet.

So the shape here is CLOSED BY CONSTRUCTION rather than a ledger of exceptions,
in the form ``EXPECTED_JOURNAL_PATH_SITES`` takes: inventory every place a
``CheckResult`` is CONSTRUCTED, and pin three partitions of it. A new row
cannot be added, and an existing ``measured=False`` cannot be deleted, without
moving one of these dicts. The reason goes in the diff that moves it.

The behavioural half is elsewhere on purpose. This file proves the inventory;
``tests/test_check_result_measurement_behaviour.py`` drives the real check
functions and proves that each unmeasured row lands in ``unmeasured`` rather
than ``fixed``. Neither is sufficient alone: a census cannot tell whether the
site is RIGHT, and a behavioural test cannot tell whether a site exists.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.astwalk import (
    assert_census,
    label,
    leaf_name,
    package_sources,
    parsed,
    scopes,
)
from tests.helpers.astwalk.scope import own_nodes

#: The constructor this file is about, and the helper that builds one on behalf
#: of the three gates. Named as constants because both nets key on them and a
#: rename that reached one and not the other would be a silent hole.
CONSTRUCTOR = "CheckResult"
GATE_HELPER = "_failed_gate_result"


def _scope_names(tree: ast.Module) -> dict[int, str]:
    """Every node in a module mapped to the qualified name of its INNERMOST scope.

    ``own_nodes`` stops at a nested function, so a ``CheckResult`` built inside
    a closure is credited to the closure and not to the function around it.
    Keyed by ``id`` because ``parsed`` caches by source text, so the same call
    returns the same node objects for the whole session.

    The scope, not the line number, is the census key. A line number would make
    every row of every dict below move whenever anything above it in
    ``verify.py`` changed, and a guard whose expected values churn on unrelated
    edits is a guard people stop reading.
    """
    found: dict[int, str] = {}
    for node, name in scopes(tree):
        for child in own_nodes(node):
            found[id(child)] = name
    return found


def constructs_a_check_result(node: ast.AST) -> bool:
    """Is this node a ``CheckResult(...)`` construction?

    ``leaf_name`` so that both spellings count: the bare name every module in
    ``kstrl/`` uses today, and ``verify.CheckResult(...)`` if one ever imports
    the module rather than the class. Deliberately resolves nothing further -
    this layer is the NET, and a net that depends on import resolution goes
    quiet on the import shape nobody enumerated.
    """
    return isinstance(node, ast.Call) and leaf_name(node.func) == CONSTRUCTOR


def calls_the_gate_helper(node: ast.AST) -> bool:
    """Is this node a call to :func:`kstrl.verify._failed_gate_result`?

    The three gates do not build their failing row themselves; they hand a
    parse to one helper. So the helper's own construction carries
    ``measured=measured`` and says nothing about whether a CALLER still passes
    the argument. Dropping it at one call site would leave every dict above
    untouched, which is the hole this second net closes.
    """
    return isinstance(node, ast.Call) and leaf_name(node.func) == GATE_HELPER


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def declares_measurement(node: ast.AST) -> bool:
    """A ``CheckResult`` construction that says, in any spelling, what it measured."""
    return constructs_a_check_result(node) and _keyword(node, "measured") is not None  # type: ignore[arg-type]


def fails_with_a_default_measurement(node: ast.AST) -> bool:
    """A construction with a literal ``passed=False`` and NO ``measured``.

    Literal, not computed: a row whose ``passed`` is an expression cannot be
    decided statically, and this partition is here to be read by a person
    rather than to be exhaustive. The total census above is what is exhaustive.
    """
    if not constructs_a_check_result(node) or _keyword(node, "measured") is not None:  # type: ignore[arg-type]
        return False
    passed = _keyword(node, "passed")  # type: ignore[arg-type]
    return isinstance(passed, ast.Constant) and passed.value is False


def _site(source_file: Path, node: ast.AST) -> str:
    tree = parsed(source_file)
    return f"{label(source_file)}: {_scope_names(tree).get(id(node), '<module>')}"


def site_row(source_file: Path, node: ast.AST) -> str:
    """``verify.py: check_linter`` - the module and the function that builds it."""
    return _site(source_file, node)


def measurement_row(source_file: Path, node: ast.AST) -> str:
    """The site plus the ``measured`` argument VERBATIM.

    The argument text is part of the key so that changing
    ``measured=bool(py_files)`` to ``measured=True`` is a moved row rather than
    an unchanged count. An expression pinned by its rendering is the only way
    a census can hold a computed value to account.
    """
    argument = _keyword(node, "measured")  # type: ignore[arg-type]
    rendered = ast.unparse(argument) if argument is not None else "MISSING"
    return f"{_site(source_file, node)}: measured={rendered}"


#: Every ``CheckResult(...)`` construction in ``kstrl/``, counted per function.
#:
#: The net. A check cannot report anything without building one of these, so a
#: new check, a new failure branch or a new vacuous pass has to appear here
#: first, whatever it then says about ``measured``. Adding a row is not
#: forbidden; it is the point. The diff that adds one is where somebody says
#: what the new row measured.
EXPECTED_CHECK_RESULT_SITES: dict[str, int] = {
    "fixtures.py: check_fixtures": 2,
    "fixtures.py: check_fixtures_from_prd": 2,
    "verify.py: _failed_gate_result": 1,
    "verify.py: _self_critique_text": 2,
    "verify.py: check_bad_patterns": 2,
    "verify.py: check_dead_code": 6,
    "verify.py: check_diff_scope": 3,
    "verify.py: check_linter": 2,
    "verify.py: check_mutation_score": 2,
    "verify.py: check_policy_envelope": 4,
    "verify.py: check_prd_stories": 4,
    "verify.py: check_scope_unreadable": 1,
    "verify.py: check_self_critique": 3,
    "verify.py: check_test_adequacy": 3,
    "verify.py: check_test_suite": 2,
    "verify.py: check_typecheck": 2,
}

#: Every construction that states its measurement, with the argument verbatim.
#:
#: Deleting one of these lines is what round 1 of review on #357 did to all six
#: of them without a single test failing. Here it is a missing row.
#:
#: One line per row saying why the row measured NOTHING. The rule they share:
#: the ENVIRONMENT failed or was never consulted, so the row's silence is not
#: evidence about the artifact. A row whose failure is a stable property of the
#: artifact stays measured, and lives in the third dict below.
EXPECTED_MEASURED_ARGUMENTS: dict[str, int] = {
    # A run with no fixtures ran no oracle, so it cannot prove one stopped
    # failing; a run in which any fixture timed out or could not be launched
    # cannot either, and `all` is what makes that the narrow direction.
    "fixtures.py: check_fixtures: measured=False": 1,
    "fixtures.py: check_fixtures: measured=all((r.measured for r in results))": 1,
    # Unreadable PRD and schema-invalid PRD: the check could not learn WHICH
    # fixtures to run, so it ran none.
    "fixtures.py: check_fixtures_from_prd: measured=False": 2,
    # The three gates' shared failing row. Its own argument is threaded from
    # the caller; EXPECTED_GATE_HELPER_CALLS below is what pins the callers.
    "verify.py: _failed_gate_result: measured=measured": 1,
    # The progress file could not be read, or is not UTF-8. No bullets were
    # counted either way.
    "verify.py: _self_critique_text: measured=False": 2,
    # "Scanned 0 Python files" opened nothing.
    "verify.py: check_bad_patterns: measured=bool(py_files)": 1,
    # vulture absent (twice, with and without a ruff note), vulture timed out,
    # and vulture handed no files to read.
    "verify.py: check_dead_code: measured=False": 4,
    # No allowed paths configured: the check applies no rule and reads no diff.
    "verify.py: check_diff_scope: measured=False": 1,
    # The three gate timeouts. The tool started and was killed, so its findings
    # are unknown rather than zero.
    "verify.py: check_linter: measured=False": 1,
    "verify.py: check_test_suite: measured=False": 1,
    "verify.py: check_typecheck: measured=False": 1,
    # The diff could not be read, and the policy could not be parsed. Both are
    # the harness failing to establish its own input.
    "verify.py: check_policy_envelope: measured=False": 2,
    # The PRD could not be loaded at all.
    "verify.py: check_prd_stories: measured=False": 1,
    # This check name exists ONLY in the unreadable state, so it never appears
    # on a healthy run. Marked rather than exempted: exempting it would put the
    # name in `measured_checks` on the one run that produces it, and its
    # absence from the next would then read as a sensor that stopped.
    "verify.py: check_scope_unreadable: measured=False": 1,
    # The diff could not be read.
    "verify.py: check_test_adequacy: measured=False": 1,
}

#: Every construction with a literal ``passed=False`` and no ``measured``.
#:
#: These are the rows that DID measure. One line each saying what, because
#: "the default was fine here" is a claim and this is where it is made. A new
#: row appearing in this dict is the census delta that asks the question.
EXPECTED_FAILING_WITH_DEFAULT: dict[str, int] = {
    # Scanned the changed Python files and found empty files, syntax errors or
    # secret patterns in them.
    "verify.py: check_bad_patterns": 1,
    # vulture ran over the changed files and reported dead code.
    "verify.py: check_dead_code": 1,
    # Read the diff and applied the configured allowlist to it.
    "verify.py: check_diff_scope": 1,
    # mutmut ran and produced killed/survived counts.
    "verify.py: check_mutation_score": 1,
    # Evaluated the policy envelope against a diff it read successfully.
    "verify.py: check_policy_envelope": 1,
    # Compared the PRD against the pre-run snapshot, and counted stories that
    # are not marked passing. Both read the document.
    "verify.py: check_prd_stories": 2,
    # Found the progress entry and counted its Self-Critique bullets.
    "verify.py: check_self_critique": 2,
}

#: Every call to the three gates' shared failing-row helper, with its
#: ``measured`` argument verbatim.
#:
#: Without this the helper's own ``measured=measured`` would be pinned while a
#: caller quietly stopped passing the argument, and the default would silently
#: take over. ``MISSING`` renders in the key when a caller passes nothing, so
#: that case fails loudly rather than reading as an unchanged count.
EXPECTED_GATE_HELPER_CALLS: dict[str, int] = {
    "verify.py: check_linter: measured=result.returncode not in COMMAND_NOT_RUN_EXIT_CODES": 1,
    "verify.py: check_test_suite: measured=result.returncode not in COMMAND_NOT_RUN_EXIT_CODES": 1,
    "verify.py: check_typecheck: measured=result.returncode not in COMMAND_NOT_RUN_EXIT_CODES": 1,
}


class TestEveryCheckResultIsAccountedFor:
    """Three partitions of one inventory, each with its own failure message."""

    def test_the_set_of_check_result_constructions_is_pinned(self) -> None:
        """The net: a new row anywhere in ``kstrl/`` moves this dict.

        This is the layer that is closed by construction. It enumerates no
        failure modes and reads no arguments, so it sees a shape nobody
        anticipated: a check that measures nothing in a way this file's other
        two partitions have no vocabulary for still has to build a
        ``CheckResult``, and still lands here.
        """
        assert_census(
            sources=package_sources(),
            sees=constructs_a_check_result,
            key=site_row,
            expected=EXPECTED_CHECK_RESULT_SITES,
            control=(
                'row = CheckResult(name="x", passed=False)\n',
                'row = verify.CheckResult(name="x", passed=False)\n',
            ),
            message=(
                "The set of places kstrl builds a CheckResult changed. If this row "
                "can report a failure having measured NOTHING - a timeout, a missing "
                "tool, a vacuous pass over an empty file list - pass measured=False "
                "so its silence cannot be read as a fix (#227). If it measured "
                "something, add it to EXPECTED_FAILING_WITH_DEFAULT with the reason."
            ),
        )

    def test_every_stated_measurement_is_pinned_with_its_argument(self) -> None:
        """Deleting a ``measured=False`` is a missing row, not a silent default.

        Round 1 of review on #357 deleted all six that existed and measured the
        full suite still green. The argument text is part of the key, so
        weakening ``measured=bool(py_files)`` to ``measured=True`` fails here
        too rather than passing as an unchanged count.
        """
        assert_census(
            sources=package_sources(),
            sees=declares_measurement,
            key=measurement_row,
            expected=EXPECTED_MEASURED_ARGUMENTS,
            control=(
                "row = CheckResult(passed=False, measured=False)\n",
                "row = CheckResult(passed=True, measured=scanned_something)\n",
            ),
            message=(
                "A CheckResult's measured argument moved. Deleting one makes a row "
                "that measured nothing contribute signatures to a sense baseline, "
                "whose later absence reads as fixed (#227). If a check genuinely "
                "started measuring, move its row and say so in the diff."
            ),
        )

    def test_every_failing_row_that_keeps_the_default_says_what_it_measured(self) -> None:
        """The other side of the same partition, with a reason per row.

        A row here is a CLAIM that this failure is evidence about the artifact
        rather than about the environment, so its signature belongs in a
        baseline and its disappearance is a fix. The claim is worth writing
        down: eight of the nineteen rows that were in this dict on the head of
        #357 turned out to be false, and each one cleared a real finding.
        """
        assert_census(
            sources=package_sources(),
            sees=fails_with_a_default_measurement,
            key=site_row,
            expected=EXPECTED_FAILING_WITH_DEFAULT,
            control='row = CheckResult(name="x", passed=False, message="m")\n',
            message=(
                "A failing CheckResult carrying the default measured=True moved. "
                "Every row in this dict claims the failure is evidence about the "
                "artifact rather than about the environment. Add the new one with "
                "the one line that says what it measured, or pass measured=False."
            ),
        )

    def test_every_gate_still_tells_the_shared_helper_whether_it_measured(self) -> None:
        """The hole the first three cannot see.

        ``_failed_gate_result`` builds the row for all three gates, so its own
        construction is one census row whatever the callers do. A gate that
        stopped passing ``measured=`` would fall back to the default and no
        dict above would move. Pinned by the argument's text, and ``MISSING``
        is what renders when a caller passes none.
        """
        assert_census(
            sources=package_sources(),
            sees=calls_the_gate_helper,
            key=measurement_row,
            expected=EXPECTED_GATE_HELPER_CALLS,
            control=(
                "row = _failed_gate_result(name, msg, parsed, cmd, cwd, start, measured=False)\n",
                "row = _failed_gate_result(name, msg, parsed, cmd, cwd, start)\n",
            ),
            message=(
                "A gate stopped telling _failed_gate_result whether its tool ran. "
                "Exit 126 and 127 mean the command never started, and a gate that "
                "reports measured=True in that case clears every one of its "
                "baseline findings (#227)."
            ),
        )
