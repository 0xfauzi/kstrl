"""Every result row in ``kstrl/`` says whether it MEASURED anything.

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
result row is CONSTRUCTED, and pin four partitions of it. A new row cannot be
added, and an existing ``measured=False`` cannot be deleted, without moving one
of these dicts. The reason goes in the diff that moves it.

The behavioural half is elsewhere on purpose. This file proves the inventory;
``tests/test_check_result_measurement_behaviour.py`` drives the real check
functions and proves that each unmeasured row lands in ``unmeasured`` rather
than ``fixed``. Neither is sufficient alone: a census cannot tell whether the
site is RIGHT, and a behavioural test cannot tell whether a site exists.

TWO TYPES, ONE WALK. ``verify.CheckResult`` and ``fixtures.FixtureResult``
carry the same field for the same reason, one level apart: a fixture row folds
into the ``fixtures`` check row with ``all()``. Round 2 of review on #357
measured what covering only the first is worth - a NEW ``FixtureResult``
environment-failure row keeping the default measured=True was planted and the
suite stayed green, which is round 1's own blocker one type down. So the nets
below key on a SET of constructor names, and adding a third type of result row
means adding its name to that set rather than writing a fourth guard.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from kstrl.fixtures import FixtureResult
from kstrl.verify import CheckResult
from tests.helpers.astwalk import (
    assert_census,
    label,
    leaf_name,
    package_sources,
    parsed,
    scopes,
)
from tests.helpers.astwalk.scope import own_nodes

#: The constructors this file is about, and the helper that builds one on
#: behalf of the three gates. Named as constants because every net keys on them
#: and a rename that reached one and not the other would be a silent hole.
CONSTRUCTORS = ("CheckResult", "FixtureResult")
GATE_HELPER = "_failed_gate_result"

#: The field itself, named once because two nets key on it: the constructions
#: that set it, and the reads that act on it.
MEASUREMENT = "measured"


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


def constructor_of(node: ast.AST) -> str | None:
    """The result type this node constructs, or None if it constructs neither.

    ``leaf_name`` so that both spellings count: the bare name every module in
    ``kstrl/`` uses today, and ``verify.CheckResult(...)`` if one ever imports
    the module rather than the class. Deliberately resolves nothing further -
    this layer is the NET, and a net that depends on import resolution goes
    quiet on the import shape nobody enumerated.
    """
    if not isinstance(node, ast.Call):
        return None
    name = leaf_name(node.func)
    return name if name in CONSTRUCTORS else None


def constructs_a_result(node: ast.AST) -> bool:
    """Is this node a construction of either result type?"""
    return constructor_of(node) is not None


def calls_the_gate_helper(node: ast.AST) -> bool:
    """Is this node a call to :func:`kstrl.verify._failed_gate_result`?

    The three gates do not build their failing row themselves; they hand a
    parse to one helper. So the helper's own construction carries
    ``measured=measured`` and says nothing about whether a CALLER still passes
    the argument. Dropping it at one call site would leave every dict above
    untouched, which is the hole this second net closes.
    """
    return isinstance(node, ast.Call) and leaf_name(node.func) == GATE_HELPER


#: Each result type's field order, for the arguments passed POSITIONALLY.
#:
#: ``kstrl/fixtures.py`` writes ``FixtureResult(fixture, False, message=...)``
#: eighteen times, so a walk that reads keywords only sees no ``passed`` at
#: those sites and silently drops them out of the partition that asks what they
#: measured. Pinned here and checked against the real dataclasses by
#: ``test_the_positional_field_order_is_the_dataclasses_own``, so reordering a
#: field fails loudly rather than moving every row of every dict below.
POSITIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "CheckResult": ("name", "passed"),
    "FixtureResult": ("fixture", "passed", "actual", "message", "measured"),
}


def _argument(node: ast.Call, name: str) -> ast.expr | None:
    """The value passed for ``name``, whether by keyword or by position."""
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    constructor = constructor_of(node)
    order = POSITIONAL_FIELDS[constructor] if constructor else ()
    if name in order:
        index = order.index(name)
        if index < len(node.args) and not isinstance(node.args[index], ast.Starred):
            return node.args[index]
    return None


def declares_measurement(node: ast.AST) -> bool:
    """A result construction that says, in any spelling, what it measured."""
    return constructs_a_result(node) and _argument(node, MEASUREMENT) is not None  # type: ignore[arg-type]


def fails_with_a_default_measurement(node: ast.AST) -> bool:
    """A construction with a literal ``passed=False`` and NO ``measured``.

    Literal, not computed: a row whose ``passed`` is an expression cannot be
    decided statically, and this partition is here to be read by a person
    rather than to be exhaustive. The total census above is what is exhaustive.
    """
    if not constructs_a_result(node) or _argument(node, MEASUREMENT) is not None:  # type: ignore[arg-type]
        return False
    passed = _argument(node, "passed")  # type: ignore[arg-type]
    return isinstance(passed, ast.Constant) and passed.value is False


def _site(source_file: Path, node: ast.AST) -> str:
    tree = parsed(source_file)
    return f"{label(source_file)}: {_scope_names(tree).get(id(node), '<module>')}"


def site_row(source_file: Path, node: ast.AST) -> str:
    """``verify.py: check_linter: CheckResult`` - module, function, type.

    The type is in the key because one function may build both: without it, a
    ``FixtureResult`` added to a function that already builds a ``CheckResult``
    would read as a count that moved by one rather than as a new kind of row.
    """
    return f"{_site(source_file, node)}: {constructor_of(node)}"


def measurement_row(source_file: Path, node: ast.AST) -> str:
    """The site plus the ``measured`` argument VERBATIM.

    The argument text is part of the key so that changing
    ``measured=bool(py_files)`` to ``measured=True`` is a moved row rather than
    an unchanged count. An expression pinned by its rendering is the only way
    a census can hold a computed value to account.
    """
    argument = _argument(node, MEASUREMENT)  # type: ignore[arg-type]
    rendered = ast.unparse(argument) if argument is not None else "MISSING"
    return f"{site_row(source_file, node)}: measured={rendered}"


def helper_call_row(source_file: Path, node: ast.AST) -> str:
    """The gate-helper call site plus its ``measured`` argument VERBATIM."""
    argument = _argument(node, MEASUREMENT)  # type: ignore[arg-type]
    rendered = ast.unparse(argument) if argument is not None else "MISSING"
    return f"{_site(source_file, node)}: measured={rendered}"


#: Every result construction in ``kstrl/``, counted per function and per type.
#:
#: The net. A check cannot report anything without building one of these, so a
#: new check, a new failure branch or a new vacuous pass has to appear here
#: first, whatever it then says about ``measured``. Adding a row is not
#: forbidden; it is the point. The diff that adds one is where somebody says
#: what the new row measured.
EXPECTED_RESULT_SITES: dict[str, int] = {
    "fixtures.py: _dispatch_fixture: FixtureResult": 1,
    "fixtures.py: _fixture_file_text: FixtureResult": 2,
    "fixtures.py: check_fixtures: CheckResult": 2,
    "fixtures.py: check_fixtures_from_prd: CheckResult": 2,
    "fixtures.py: run_cli_fixture: FixtureResult": 7,
    "fixtures.py: run_file_fixture: FixtureResult": 8,
    "fixtures.py: run_function_fixture: FixtureResult": 9,
    "verify.py: _failed_gate_result: CheckResult": 1,
    "verify.py: _self_critique_text: CheckResult": 2,
    "verify.py: check_bad_patterns: CheckResult": 2,
    "verify.py: check_dead_code: CheckResult": 2,
    "verify.py: check_dead_code_ruff: CheckResult": 1,
    "verify.py: check_diff_scope: CheckResult": 4,
    "verify.py: check_linter: CheckResult": 2,
    "verify.py: check_mutation_score: CheckResult": 2,
    "verify.py: check_policy_envelope: CheckResult": 4,
    "verify.py: check_prd_stories: CheckResult": 4,
    "verify.py: check_scope_unreadable: CheckResult": 1,
    "verify.py: check_self_critique: CheckResult": 3,
    "verify.py: check_test_adequacy: CheckResult": 3,
    "verify.py: check_test_suite: CheckResult": 2,
    "verify.py: check_typecheck: CheckResult": 2,
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
    "fixtures.py: check_fixtures: CheckResult: measured=False": 1,
    "fixtures.py: check_fixtures: CheckResult: measured=all((r.measured for r in results))": 1,
    # Unreadable PRD and schema-invalid PRD: the check could not learn WHICH
    # fixtures to run, so it ran none.
    "fixtures.py: check_fixtures_from_prd: CheckResult: measured=False": 2,
    # The file existed when the caller looked and could not be read, or could
    # not be decoded. Either way the `contains` expectations never ran.
    "fixtures.py: _fixture_file_text: FixtureResult: measured=False": 2,
    # The command fixture's two environment failures: the process was killed on
    # the timeout, or could not be launched at all.
    "fixtures.py: run_cli_fixture: FixtureResult: measured=False": 2,
    # The function fixture's own two, which are separate sites and separate
    # branches from the command fixture's.
    "fixtures.py: run_function_fixture: FixtureResult: measured=False": 2,
    # The three gates' shared failing row, and the only place a gate's
    # measurement is decided. `recognised` is the parser saying it saw its own
    # tool report a failure; EXPECTED_GATE_HELPER_CALLS below is what stops a
    # caller overriding it.
    "verify.py: _failed_gate_result: CheckResult: measured=parsed.recognised": 1,
    # The progress file could not be read, or is not UTF-8. No bullets were
    # counted either way.
    "verify.py: _self_critique_text: CheckResult: measured=False": 2,
    # Nothing was opened: an empty diff, or changed Python files that are all
    # gone from the worktree.
    "verify.py: check_bad_patterns: CheckResult: measured=bool(scanned)": 1,
    # No allowed paths configured, and an empty diff: the check applies no rule
    # or applies it to nothing.
    "verify.py: check_diff_scope: CheckResult: measured=False": 2,
    # The three gate timeouts. The tool started and was killed, so its findings
    # are unknown rather than zero.
    "verify.py: check_linter: CheckResult: measured=False": 1,
    "verify.py: check_test_suite: CheckResult: measured=False": 1,
    "verify.py: check_typecheck: CheckResult: measured=False": 1,
    # The diff could not be read, and the policy could not be parsed. Both are
    # the harness failing to establish its own input.
    "verify.py: check_policy_envelope: CheckResult: measured=False": 2,
    # The PRD could not be loaded at all.
    "verify.py: check_prd_stories: CheckResult: measured=False": 1,
    # This check name exists ONLY in the unreadable state, so it never appears
    # on a healthy run. Marked rather than exempted: exempting it would put the
    # name in `measured_checks` on the one run that produces it, and its
    # absence from the next would then read as a sensor that stopped.
    "verify.py: check_scope_unreadable: CheckResult: measured=False": 1,
    # The diff could not be read.
    "verify.py: check_test_adequacy: CheckResult: measured=False": 1,
}

#: Every construction with a literal ``passed=False`` and no ``measured``.
#:
#: These are the rows that DID measure. One line each saying what, because
#: "the default was fine here" is a claim and this is where it is made. A new
#: row appearing in this dict is the census delta that asks the question.
EXPECTED_FAILING_WITH_DEFAULT: dict[str, int] = {
    # An unknown fixture_type is a malformed PRD, which is a stable property of
    # the artifact and whose disappearance is a real fix.
    "fixtures.py: _dispatch_fixture: FixtureResult": 1,
    # Three malformed definitions (no command, an empty command, a command the
    # shell lexer refused) and the comparison of a real exit code, stdout and
    # stderr against the expectation.
    "fixtures.py: run_cli_fixture: FixtureResult": 4,
    # Four malformed definitions, a spec that would not serialise, and a child
    # that exited without a result - which is the function under test taking
    # the process down, a property of the artifact.
    "fixtures.py: run_function_fixture: FixtureResult": 6,
    # Three malformed definitions (no path, an absolute or `..` path, a path
    # escaping the worktree) and three real comparisons against the file: it
    # was expected and absent, unexpected and present, or its content did not
    # match.
    "fixtures.py: run_file_fixture: FixtureResult": 6,
    # Scanned the changed Python files and found empty files, syntax errors or
    # secret patterns in them.
    "verify.py: check_bad_patterns: CheckResult": 1,
    # vulture ran over the changed files and reported dead code. Every way that
    # phase can measure NOTHING returns a NotMeasured gap instead of a row
    # (#335), so it needs no measured argument at all: the dampener reads a gap
    # and a measured=False row through the same code path.
    "verify.py: check_dead_code: CheckResult": 1,
    # Read the diff and applied the configured allowlist to it.
    "verify.py: check_diff_scope: CheckResult": 1,
    # mutmut ran and produced killed/survived counts.
    "verify.py: check_mutation_score: CheckResult": 1,
    # Evaluated the policy envelope against a diff it read successfully.
    "verify.py: check_policy_envelope: CheckResult": 1,
    # Compared the PRD against the pre-run snapshot, and counted stories that
    # are not marked passing. Both read the document.
    "verify.py: check_prd_stories: CheckResult": 2,
    # Found the progress entry and counted its Self-Critique bullets.
    "verify.py: check_self_critique: CheckResult": 2,
}

#: Every call to the three gates' shared failing-row helper, with its
#: ``measured`` argument verbatim.
#:
#: ``MISSING`` at all three is the CORRECT state and the point of the net. The
#: helper decides the measurement from the parse it is handed, so a gate that
#: passes ``measured=`` at all is overriding the parser's evidence with the
#: caller's opinion, and that is round 1's defect: the argument it passed was
#: ``result.returncode not in {126, 127}``, which is the SHELL's vocabulary for
#: a command it could not start, and the gate commands this repository ships go
#: through ``uv run``, which reports its own exit 2 instead.
EXPECTED_GATE_HELPER_CALLS: dict[str, int] = {
    "verify.py: check_linter: measured=MISSING": 1,
    "verify.py: check_test_suite: measured=MISSING": 1,
    "verify.py: check_typecheck: measured=MISSING": 1,
}


class TestEveryResultRowIsAccountedFor:
    """Four partitions of one inventory, each with its own failure message."""

    def test_the_positional_field_order_is_the_dataclasses_own(self) -> None:
        """The control for :data:`POSITIONAL_FIELDS`.

        The three nets below read ``passed`` and ``measured`` by POSITION as
        well as by keyword, because ``kstrl/fixtures.py`` passes them
        positionally. A pinned index that no longer matches the dataclass would
        read the wrong argument and answer confidently: a reordered
        ``FixtureResult`` would have the walk take ``actual`` for ``passed``,
        the partition would empty out, and the census would go quiet rather
        than red. So the pin is checked against the classes themselves.
        """
        actual = {
            "CheckResult": tuple(f.name for f in dataclasses.fields(CheckResult)),
            "FixtureResult": tuple(f.name for f in dataclasses.fields(FixtureResult)),
        }
        for constructor, pinned in POSITIONAL_FIELDS.items():
            assert actual[constructor][: len(pinned)] == pinned, (
                f"{constructor}'s field order moved. POSITIONAL_FIELDS is how the "
                f"nets in this file read a positional argument, so a stale prefix "
                f"makes them read the wrong one: {actual[constructor]}"
            )
        assert set(POSITIONAL_FIELDS) == set(CONSTRUCTORS), (
            "every constructor the nets walk needs its field order pinned, or a "
            "positionally-passed measured= at its sites is invisible."
        )

    def test_the_set_of_result_constructions_is_pinned(self) -> None:
        """The net: a new row anywhere in ``kstrl/`` moves this dict.

        This is the layer that is closed by construction. It enumerates no
        failure modes and reads no arguments, so it sees a shape nobody
        anticipated: a check that measures nothing in a way this file's other
        partitions have no vocabulary for still has to build one of these, and
        still lands here.
        """
        assert_census(
            sources=package_sources(),
            sees=constructs_a_result,
            key=site_row,
            expected=EXPECTED_RESULT_SITES,
            control=(
                'row = CheckResult(name="x", passed=False)\n',
                'row = verify.CheckResult(name="x", passed=False)\n',
                'row = FixtureResult(fixture, False, message="m")\n',
            ),
            message=(
                "The set of places kstrl builds a result row changed. If this row "
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
        weakening ``measured=bool(scanned)`` to ``measured=True`` fails here
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
                'row = FixtureResult(fixture, False, "", "m", False)\n',
            ),
            message=(
                "A result row's measured argument moved. Deleting one makes a row "
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
            control=(
                'row = CheckResult(name="x", passed=False, message="m")\n',
                'row = FixtureResult(fixture, False, message="m")\n',
            ),
            message=(
                "A failing result row carrying the default measured=True moved. "
                "Every row in this dict claims the failure is evidence about the "
                "artifact rather than about the environment. Add the new one with "
                "the one line that says what it measured, or pass measured=False."
            ),
        )

    def test_no_gate_overrides_the_shared_helper_s_measurement(self) -> None:
        """The hole the first three cannot see.

        ``_failed_gate_result`` builds the row for all three gates and decides
        ``measured`` from the parse, so its own construction is one census row
        whatever the callers do. A gate that started passing ``measured=``
        again would replace the parser's evidence with the caller's opinion and
        no dict above would move. ``MISSING`` at all three is the state this
        pins; the argument's text renders in the key when one appears.
        """
        assert_census(
            sources=package_sources(),
            sees=calls_the_gate_helper,
            key=helper_call_row,
            expected=EXPECTED_GATE_HELPER_CALLS,
            control=(
                "row = _failed_gate_result(name, msg, parsed, cmd, cwd, start, measured=False)\n",
                "row = _failed_gate_result(name, msg, parsed, cmd, cwd, start)\n",
            ),
            message=(
                "A gate started deciding for itself whether its tool ran. That "
                "decision belongs to the parser: round 1 of #357 made it from the "
                "exit code, which is uv's status and not the tool's, so a missing "
                "linter cleared every one of its baseline findings (#227)."
            ),
        )


#: Every READ of a ``.measured`` attribute in ``kstrl/``, counted per function.
#:
#: The other direction of the same field. The three censuses above pin where
#: the value is WRITTEN; this one pins who is allowed to act on it, and the
#: answer is the dampener and nobody else. ``measured`` says whether a row is
#: evidence about the ARTIFACT, which is a question about comparing two runs.
#: It is not a question about whether this run passed, and the moment the
#: mechanical verdict starts consulting it, a gate whose tool is missing stops
#: failing the run: ``all(c.passed for c in checks if c.measured)`` turns a
#: test suite that timed out into a PASS, silently, in the direction this
#: repository keeps finding.
#:
#: ``pipeline.py`` reads a DIFFERENT field of the same name -
#: ``FactUtilization.measured``, the R8 fact-utilization evidence flag - which
#: no walk can tell apart from this one without type inference. Those two rows
#: are pinned rather than excluded: this net flags, so over-matching costs
#: somebody a census delta to read, and narrowing it to a set of modules
#: somebody enumerated is how a guard goes blind on the module nobody thought
#: of.
EXPECTED_MEASUREMENT_READS: dict[str, int] = {
    # The dampener: which checks may have a missing signature read as fixed.
    "dampener.py: _measured_and_unmeasured: check.measured": 2,
    # The fixtures row folds its per-fixture measurements with `all`.
    "fixtures.py: check_fixtures: r.measured": 1,
    # Not CheckResult.measured. FactUtilization's own field, R8.
    "pipeline.py: ComponentPipeline._store_fact_utilization: util.measured": 2,
    "pipeline.py: FactUtilization.to_dict: self.measured": 1,
}


def reads_the_measurement(node: ast.AST) -> bool:
    """A LOAD of an attribute called ``measured``, in any spelling.

    Deliberately blind to what it is read FROM: an attribute access cannot be
    resolved to a type without inference this walk does not do, and a net that
    guessed would clear the one site that matters. Reading is the whole
    predicate; the dict above is where each read is accounted for.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == MEASUREMENT
        and isinstance(node.ctx, ast.Load)
    )


def read_row(source_file: Path, node: ast.AST) -> str:
    """``dampener.py: _measured_and_unmeasured: check.measured``."""
    return f"{_site(source_file, node)}: {ast.unparse(node)}"


class TestTheVerdictDoesNotDependOnMeasurement:
    """``measured`` changes what a COMPARISON says, never what a RUN says.

    ``run_mechanical_verification`` computes ``passed = all(c.passed for c in
    checks)``, and #227 must not have changed that. The behavioural half is
    ``tests/test_check_result_measurement_behaviour.py``, which runs the real
    function over a gate whose tool is missing and asserts the run still fails;
    this half is the inventory, because a behavioural test speaks only for the
    check it drove.
    """

    def test_the_places_that_read_a_measurement_are_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=reads_the_measurement,
            key=read_row,
            expected=EXPECTED_MEASUREMENT_READS,
            control=(
                "if check.measured:\n    pass\n",
                "flag = self.measured\n",
            ),
            message=(
                "Something new reads a `.measured`. If it is CheckResult.measured "
                "and it reaches a PASS/FAIL verdict, that is the #227 fail-open: a "
                "gate whose tool is missing reports measured=False, and a verdict "
                "that skips unmeasured rows turns it into a pass. The field exists "
                "to decide what a COMPARISON may call fixed."
            ),
        )
