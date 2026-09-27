"""R8.5 Layer 0: test-diff discipline and oracle-signal linting.

Green tests cannot be a merge gate on their own. The evidence is
specific: 80.2% of 86k agent-authored test patches carry weak or no
oracle signals (arXiv:2606.18168), LLM assertions tend to encode the
behavior the code HAS rather than the behavior it SHOULD have, and
test-gaming is measured rather than hypothetical (ImpossibleBench,
arXiv:2510.20270). A suite an agent can edit is a suite an agent can
weaken, and "the tests pass" then means only "the tests that survived
pass".

This module is the cheapest of the four adequacy layers: it reads the
diff and the changed test files, needs no test execution, no coverage
run, no mutation tooling, and no historical data. It answers two
questions:

R8.5 Layer 1 (patch coverage, #152) lives in this module too, but only
its arithmetic does: :func:`added_line_numbers`, :func:`coverage_targets`
and :func:`measure_patch_coverage` are pure functions over a diff and an
already-produced coverage report. The coverage RUN - running the
project's test command a second time under ``--cov`` and the subprocess
handling around it - is :func:`kstrl.verify.check_patch_coverage`,
because it needs ``run_scrubbed`` and this module has no subprocess or
I/O dependency today. Layer 1 is advisory with no floor, same as Layer
0: it measures and reports, and the finding is emitted at every
percentage including 100%, because the distribution a floor would later
be set from is the whole point of shipping it now.

1. **Did this change weaken the existing suite?** Deleted tests,
   newly-skipped tests, and assertions replaced by nothing are all
   legitimate sometimes and suspicious always, so they are reported with
   the evidence rather than silently allowed.
2. **Do the NEW tests actually assert anything?** A test whose only
   assertion is ``assert result is not None`` passes for almost any
   implementation, including a broken one. The taxonomy below separates
   assertions that could FAIL from assertions that merely execute.

The two questions have different scopes, and the difference is
load-bearing. Question 1 is asked of every changed test file: any diff
can weaken any file. Question 2 is asked only of files the diff ADDS,
because a modified file carries tests written by someone else under a
different standard, and failing a one-line edit for a legacy file's weak
oracles is how a gate gets switched off. What a diff adds to a modified
file is still fair game, and is checked.

Known limit, measured rather than assumed: a custom assertion helper
that returns a bool (``assert snap_compare(...)``) is statically
indistinguishable from ``assert flag_set(0)``, so it classifies WEAK.
Across kstrl's own ~60 test files exactly one trips this. One strong
test carries a file and the floor applies only to newly added files, so
the rate stays low - but a repo built on custom assertion helpers should
expect it before setting ``layer0 = "block"``.

What it deliberately does NOT do: judge whether an assertion's expected
value is CORRECT. Nothing static can - that is what the fixtures oracle
(Layer 3) and spec-derived criteria are for. This layer only
distinguishes "asserts something falsifiable" from "asserts nothing",
which is a lower bar and a much more reliable signal.
"""

from __future__ import annotations

import ast
import re
import textwrap
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from kstrl.config_numbers import check_numbers
from kstrl.policy import diff_header_path

#: Test-file path fragments. Deliberately broad: a file that looks like a
#: test to a human should be judged as one, and a false positive here
#: costs a note, while a false negative silently exempts a file.
TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")


class OracleStrength(StrEnum):
    """How much a test file's assertions could actually catch.

    Named after the W1-W5 / S1-S3 taxonomy in arXiv:2606.18168, reduced
    to the three distinctions this layer can make statically.

    The dividing line is one question: **does the assertion name a value
    the code is supposed to produce?** If it does, a plausible-looking
    wrong answer fails it (STRONG). If it only asks whether something
    exists, is truthy, or has the right type, a wrong answer sails
    through (WEAK). See :func:`_classify_expr` for the exact rule.
    """

    STRONG = "strong"  # compares against an expected value or raises
    WEAK = "weak"  # executes, asserts only shape or truthiness
    NONE = "none"  # no assertion at all


class FindingKind(StrEnum):
    TEST_DELETED = "test_deleted"
    TEST_SKIPPED = "test_skipped"
    ASSERTION_REMOVED = "assertion_removed"
    WEAK_ORACLE = "weak_oracle"
    NO_ORACLE = "no_oracle"


@dataclass(frozen=True)
class AdequacyFinding:
    """One adequacy concern, with the evidence that produced it."""

    kind: FindingKind
    path: str
    detail: str
    symbol: str = ""

    def render(self) -> str:
        where = f"{self.path}::{self.symbol}" if self.symbol else self.path
        return f"{where}: {self.detail}"


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


# ---------------------------------------------------------------------------
# Oracle-signal linting (AST over the file's CURRENT content)
# ---------------------------------------------------------------------------
def _is_vacuous_constant(node: ast.expr) -> bool:
    """`assert True`, `assert 1`, `assert ...` - truth without content."""
    if isinstance(node, ast.Constant):
        return bool(node.value) and repr(node.value) != "False"
    return False


#: Calls that inspect an object's SHAPE rather than its value. Listed
#: explicitly because they read like real checks and are not.
_SHAPE_CALLS = frozenset({"isinstance", "issubclass", "callable", "hasattr"})


def _dotted_name(node: ast.expr) -> str:
    """``pytest.mark.skip`` for the attribute chain, "" for anything else."""
    parts: list[str] = []
    target: ast.expr = node
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _contains_comparison(node: ast.expr) -> bool:
    """Whether an expected value is stated ANYWHERE inside this expression.

    Walks the whole subtree on purpose, so a comparison inside a
    comprehension counts: ``all(x > 0 for x in items)`` is a genuine
    property oracle and must not be demoted alongside ``all(items)``.
    """
    return any(isinstance(child, ast.Compare) for child in ast.walk(node))


def _is_truthiness_call(node: ast.expr) -> bool:
    """A call asserted for its truthiness, with no expected value stated.

    ``assert compute()``, ``assert bool(result)`` and ``assert
    obj.is_ready()`` all pass for any truthy return, which is exactly the
    weakness ``assert result`` has - the ``bool(...)`` wrapper changes
    nothing about what could fail.

    The rule: a Call is STRONG only when its arguments contain a
    comparison (directly, or inside a comprehension). That keeps
    ``all(x > 0 for x in items)`` and ``any(v == 3 for v in vs)`` strong
    while demoting bare predicate calls.

    Known trade: a predicate that carries its expectation as a plain
    argument, e.g. ``assert s.startswith("prefix")``, is classified WEAK
    even though a human would call it an oracle. That is the deliberate
    direction - the finding only fires when a NEW test file has NO strong
    assertion anywhere, so one comparison anywhere in the file clears it,
    and under-flagging is far cheaper than blocking a correct suite.
    """
    if not isinstance(node, ast.Call):
        return False
    arguments: list[ast.expr] = [
        *node.args,
        *(kw.value for kw in node.keywords),
    ]
    return not any(_contains_comparison(arg) for arg in arguments)


def _is_shape_only(node: ast.expr) -> bool:
    """`assert x is not None`, `assert x`, `assert isinstance(x, T)`.

    These fire only when the code returns nothing or the wrong TYPE. They
    pass for a function that returns a plausible-looking wrong answer,
    which is the failure mode that matters for agent-written tests.
    """
    if isinstance(node, ast.Compare):
        # `x is not None` / `x != None`
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, (ast.IsNot, ast.NotEq)) and (
                isinstance(comparator, ast.Constant) and comparator.value is None
            ):
                return True
        return False
    if isinstance(node, ast.Call):
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else "")
        )
        if name in _SHAPE_CALLS:
            return True
        return _is_truthiness_call(node)
    # A bare name or attribute: `assert result`
    return isinstance(node, (ast.Name, ast.Attribute))


def _classify_expr(node: ast.expr) -> OracleStrength:
    """Oracle strength of one asserted expression.

    STRONG requires a stated expectation: a comparison against a value,
    an asserted exception, or a call whose arguments contain a comparison.
    Everything else executes code and checks that it did not blow up.

    Boolean operators aggregate differently, because they are satisfied
    differently: ``and`` needs EVERY operand to hold, so the conjunction
    is as strong as its strongest operand; ``or`` is satisfied by the
    first operand that holds, so it is only as strong as its WEAKEST one.
    ``assert x is not None or x == 3`` short-circuits on any non-None
    wrong value and can never reach the comparison.
    """
    if _is_vacuous_constant(node):
        return OracleStrength.WEAK
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        # `assert not x` is exactly as weak as `assert x`; `assert not
        # (a == b)` is exactly as strong as `assert a == b`.
        return _classify_expr(node.operand)
    if isinstance(node, ast.BoolOp):
        strengths = [_classify_expr(value) for value in node.values]
        if isinstance(node.op, ast.And):
            return (
                OracleStrength.STRONG if OracleStrength.STRONG in strengths else OracleStrength.WEAK
            )
        return (
            OracleStrength.STRONG
            if strengths and all(s is OracleStrength.STRONG for s in strengths)
            else OracleStrength.WEAK
        )
    if _is_shape_only(node):
        return OracleStrength.WEAK
    return OracleStrength.STRONG


def _classify_assert(node: ast.Assert) -> OracleStrength:
    return _classify_expr(node.test)


#: `unittest.TestCase` and `unittest.mock` assertions that pin a VALUE or
#: an exact interaction: a plausible-looking wrong answer fails them, so
#: they are the method-call spelling of a strong oracle. Collected because
#: a file written against `unittest` or `Mock` contains no bare `assert`
#: statement at all, and reading it as "asserts nothing" is the worst
#: false positive this layer can produce.
_STRONG_ASSERT_METHODS = frozenset(
    {
        "assertEqual",
        "assertNotEqual",
        "assertAlmostEqual",
        "assertNotAlmostEqual",
        "assertIn",
        "assertNotIn",
        "assertIs",
        "assertIsNot",
        "assertListEqual",
        "assertDictEqual",
        "assertSetEqual",
        "assertTupleEqual",
        "assertSequenceEqual",
        "assertMultiLineEqual",
        "assertCountEqual",
        "assertDictContainsSubset",
        "assertRegex",
        "assertNotRegex",
        "assertGreater",
        "assertGreaterEqual",
        "assertLess",
        "assertLessEqual",
        "assertRaises",
        "assertRaisesRegex",
        "assertWarns",
        "assertWarnsRegex",
        "assertLogs",
        "assertNoLogs",
        # Mock: these name the arguments the call must have had, or that no
        # call may have happened at all.
        "assert_called_with",
        "assert_called_once_with",
        "assert_any_call",
        "assert_has_calls",
        "assert_not_called",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_any_await",
        "assert_has_awaits",
        "assert_not_awaited",
    }
)

#: Assertions satisfied by ANY value or ANY call: they catch a missing
#: result, never a wrong one. Same tier as `assert x is not None`.
_WEAK_ASSERT_METHODS = frozenset(
    {
        "assertTrue",
        "assertFalse",
        "assertIsNone",
        "assertIsNotNone",
        "assertIsInstance",
        "assertNotIsInstance",
        "assertHasAttr",
        "assert_called",
        "assert_called_once",
        "assert_awaited",
        "assert_awaited_once",
    }
)


def _assertion_method_strengths(node: ast.AST) -> list[OracleStrength]:
    """Oracle strengths of the unittest/mock assertion CALLS in a test.

    ``self.assertTrue(x == 3)`` is upgraded to STRONG by the same rule
    that governs bare calls: the expectation is stated in the arguments,
    so a wrong value fails it.
    """
    strengths: list[OracleStrength] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        name = func.attr
        if name in _STRONG_ASSERT_METHODS:
            strengths.append(OracleStrength.STRONG)
        elif name in _WEAK_ASSERT_METHODS:
            strengths.append(
                OracleStrength.STRONG
                if any(_contains_comparison(arg) for arg in child.args)
                else OracleStrength.WEAK
            )
    return strengths


def _has_raises_context(node: ast.AST) -> bool:
    """`with pytest.raises(...)` is a strong oracle: it names the failure."""
    for child in ast.walk(node):
        if not isinstance(child, ast.With):
            continue
        for item in child.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                func = call.func
                attr = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else (func.id if isinstance(func, ast.Name) else "")
                )
                if attr in {"raises", "warns", "assertRaises"}:
                    return True
    return False


#: Name segments that disable execution. Matched per dotted SEGMENT, not
#: as a substring: `pytest.mark.skip` counts, `runner.skip_count` does
#: not, and a test named `test_skip_behaviour` is not a skip.
_SKIP_SEGMENTS = frozenset(
    {
        "skip",
        "skipif",
        "xfail",
        "skipTest",
        "skipIf",
        "skipUnless",
    }
)


def _is_skip_name(dotted: str) -> bool:
    return any(part in _SKIP_SEGMENTS for part in dotted.split("."))


def _skip_marker(node: ast.AST) -> str:
    """The name of a skip/xfail decorator on this node, or ""."""
    decorators = getattr(node, "decorator_list", [])
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        joined = _dotted_name(target)
        if joined and _is_skip_name(joined):
            return joined
    return ""


def _skip_reference(tree: ast.AST) -> str:
    """The dotted name of ANY pytest/unittest skip reference in a tree.

    Covers the mechanisms a decorator-only check misses: a `pytest.skip()`
    call in a test body, a module-level `pytestmark = pytest.mark.skip(...)`
    that disables every test in the file, and `marks=pytest.mark.xfail(...)`
    inside `pytest.param`. All three stop the tests from running, which is
    the thing being detected; the decorator was never the point.

    Deliberately narrow about WHERE a name counts - as a call target, a
    decorator, or the value of a `marks=` keyword - so that an ordinary
    identifier (`skip = 3`, `assert row.skip == 1`) is not a skip.
    """
    for child in ast.walk(tree):
        if isinstance(child, ast.Call):
            name = _dotted_name(child.func)
            if name and _is_skip_name(name):
                return name
        elif isinstance(child, ast.keyword) and child.arg == "marks":
            for inner in ast.walk(child.value):
                if isinstance(inner, (ast.Attribute, ast.Name)):
                    name = _dotted_name(inner)
                    if name and _is_skip_name(name):
                        return name
        for decorator in getattr(child, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = _dotted_name(target)
            if name and _is_skip_name(name):
                return name
    return ""


#: Module-level names pytest reads as file-wide markers.
_MODULE_MARKER_NAMES = frozenset({"pytestmark"})

#: Stands in for a test name when the skip is file-wide.
_MODULE_SKIP_SYMBOL = "<module>"


def _body_skip_call(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """An UNCONDITIONAL ``pytest.skip()`` / ``self.skipTest()`` in the body.

    Only top-level statements of the function count. ``if sys.platform ==
    "win32": pytest.skip(...)`` is a platform guard rather than a disabled
    test, and flagging every guarded skip would bury the case that matters:
    a test whose body stops it running no matter what.
    """
    for stmt in node.body:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        name = _dotted_name(stmt.value.func)
        if name and _is_skip_name(name):
            return name
    return ""


def _module_skip_marker(tree: ast.Module) -> str:
    """A file-wide `pytestmark` skip/xfail, or "".

    One assignment at module level disables every test in the file and
    leaves each individual test looking perfectly healthy to the AST.
    """
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id in _MODULE_MARKER_NAMES for t in targets):
            continue
        value = node.value
        if value is None:
            continue
        # Inside a `pytestmark` assignment any skip name counts, however
        # it is spelled: `pytest.mark.skip`, `pytest.mark.skipif(...)`,
        # or a list of either.
        for inner in ast.walk(value):
            if isinstance(inner, (ast.Attribute, ast.Name)):
                name = _dotted_name(inner)
                if name and _is_skip_name(name):
                    return name
    return ""


@dataclass
class TestFileReport:
    """Per-file oracle assessment."""

    path: str
    tests: int = 0
    strong: int = 0
    weak: int = 0
    without_assertions: list[str] = field(default_factory=list)
    weak_only: list[str] = field(default_factory=list)
    #: (test name, marker). A file-wide `pytestmark` is recorded against
    #: the sentinel name `<module>`: it disables every test in the file.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    parse_error: str = ""

    @property
    def strength(self) -> OracleStrength:
        if self.strong:
            return OracleStrength.STRONG
        if self.weak:
            return OracleStrength.WEAK
        return OracleStrength.NONE


def lint_test_source(path: str, source: str) -> TestFileReport:
    """Assess one test file's oracle signals.

    Fails OPEN on a syntax error: an unparseable file is a problem for
    the linter and the test runner both, and the runner reports it far
    better than a guess here would.
    """
    report = TestFileReport(path=path)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.parse_error = f"could not parse: {exc}"
        return report

    module_marker = _module_skip_marker(tree)
    if module_marker:
        report.skipped.append((_MODULE_SKIP_SYMBOL, module_marker))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        report.tests += 1

        marker = _skip_marker(node) or _body_skip_call(node)
        if marker:
            report.skipped.append((node.name, marker))

        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        strengths = [_classify_assert(a) for a in asserts]
        # unittest / mock assertion METHODS are oracles too; a file
        # written against either contains no bare `assert` at all.
        strengths.extend(_assertion_method_strengths(node))
        if _has_raises_context(node):
            strengths.append(OracleStrength.STRONG)

        if not strengths:
            report.without_assertions.append(node.name)
            continue
        if OracleStrength.STRONG in strengths:
            report.strong += 1
        else:
            report.weak += 1
            report.weak_only.append(node.name)
    return report


# ---------------------------------------------------------------------------
# Test-diff discipline (reads the unified diff)
# ---------------------------------------------------------------------------
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_ASSERT_RE = re.compile(r"^\s*assert\b")
#: Fallback only, for added lines that do not parse on their own (a
#: continuation, a fragment of a multi-line call). The primary detector is
#: :func:`_skip_reference` over the parsed line - see :func:`_added_skip`.
_SKIP_RE = re.compile(
    r"@(?:\w[\w.]*\.)?(skip|skipif|xfail)\b"
    r"|(?:^|[\s(\[=,])(?:\w[\w.]*\.)?(skip|skipif|xfail|skipTest)\s*\("
)


def _added_skip(body: str) -> str:
    """The skip mechanism an added diff line introduces, or "".

    Syntax-aware first: the line is dedented and parsed on its own, which
    covers every mechanism uniformly - the ``@pytest.mark.skip``
    decorator, a bare ``pytest.skip("disabled")`` in a body, a module-level
    ``pytestmark = pytest.mark.skip(...)``, and ``pytest.param(...,
    marks=pytest.mark.xfail(...))`` (which parses as a tuple, trailing
    comma and all). A line that cannot stand alone falls back to the
    regex: a fragment is worth a noisier check, not no check.
    """
    stripped = textwrap.dedent(body).strip()
    if not stripped:
        return ""
    try:
        tree = ast.parse(stripped)
    except SyntaxError:
        match = _SKIP_RE.search(body)
        return (match.group(1) or match.group(2)) if match else ""
    return _skip_reference(tree)


@dataclass
class DiffDiscipline:
    """What the diff did to the existing suite."""

    removed_tests: list[tuple[str, str]] = field(default_factory=list)
    #: Test defs ADDED by the diff, keyed by file. A name in both sets was
    #: MODIFIED, not deleted, and reporting it as a deletion would cry
    #: wolf on every edited test - the signal that matters is a test that
    #: leaves the suite entirely.
    added_tests: set[tuple[str, str]] = field(default_factory=set)
    added_skips: list[tuple[str, str]] = field(default_factory=list)
    removed_assertions: dict[str, int] = field(default_factory=dict)
    added_assertions: dict[str, int] = field(default_factory=dict)

    def deleted_tests(self) -> list[tuple[str, str]]:
        """Tests the diff removed WITHOUT re-adding them.

        A rewritten test shows up as a removed def and an added def for
        the same name; only the difference is a deletion.
        """
        return [
            (path, name)
            for (path, name) in self.removed_tests
            if (path, name) not in self.added_tests
        ]

    def net_assertion_loss(self) -> dict[str, int]:
        """Files whose assertion count went DOWN.

        Counting lines rather than semantics on purpose: this is a
        signal to look, not a verdict. A refactor that consolidates
        assertions shows up here and is explained in one sentence; a
        quiet weakening shows up here too, and that is the point.
        """
        loss: dict[str, int] = {}
        for path, removed in self.removed_assertions.items():
            delta = removed - self.added_assertions.get(path, 0)
            if delta > 0:
                loss[path] = delta
        return loss


_DEV_NULL = "/dev/null"


def _iter_diff_lines(diff_text: str) -> Iterator[tuple[str, str, str]]:
    """Walk a unified diff, yielding ``(source, target, line)``.

    ``source``/``target`` are the paths from the most recent file header
    - a ``+++ `` line immediately preceded by a ``--- `` line, the guard
    that keeps a content line whose TEXT merely starts with ``+++`` from
    being read as one - and reset to ``("", "")`` on every ``diff --git``
    that starts a new file entry, matching git's own file boundary. Both
    :func:`analyze_test_diff` and :func:`added_line_numbers` (#152
    simplify pass) need exactly this parse; what a DELETED
    (``target == _DEV_NULL``) or ADDED (``source == _DEV_NULL``) file
    means for "the current path" is left to each caller, because they
    read it differently - one keeps the source path for a deletion, the
    other drops the path entirely.
    """
    source = ""
    target = ""
    prev = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            source, target = "", ""
        elif line.startswith("+++ ") and prev.startswith("--- "):
            target = diff_header_path(line)
            source = diff_header_path(prev)
        yield source, target, line
        prev = line


def analyze_test_diff(diff_text: str) -> DiffDiscipline:
    """Extract suite-weakening signals from a unified diff."""
    result = DiffDiscipline()
    prev = ""
    for source, target, line in _iter_diff_lines(diff_text):
        if line.startswith("diff --git") or (line.startswith("+++ ") and prev.startswith("--- ")):
            prev = line
            continue
        # A DELETED file is `--- a/tests/x.py` / `+++ /dev/null`. Keeping
        # the source path is the whole point: deleting a test file
        # outright is the most direct way to weaken a suite, and
        # dropping it here made that the one case that reported nothing
        # at all. An ADDED file (`--- /dev/null`) keeps the target,
        # which is already the non-empty side.
        current = source if target == _DEV_NULL else target
        if current == _DEV_NULL:
            current = ""
        if current and is_test_path(current):
            if line.startswith("-") and not line.startswith("---"):
                body = line[1:]
                match = _DEF_RE.match(body)
                if match:
                    result.removed_tests.append((current, match.group(1)))
                if _ASSERT_RE.match(body):
                    result.removed_assertions[current] = (
                        result.removed_assertions.get(current, 0) + 1
                    )
            elif line.startswith("+") and not line.startswith("+++"):
                body = line[1:]
                added_def = _DEF_RE.match(body)
                if added_def:
                    result.added_tests.add((current, added_def.group(1)))
                if _added_skip(body):
                    result.added_skips.append((current, body.strip()))
                if _ASSERT_RE.match(body):
                    result.added_assertions[current] = result.added_assertions.get(current, 0) + 1
        prev = line
    return result


# ---------------------------------------------------------------------------
# R8.5 Layer 1 (#152): patch coverage arithmetic. No I/O, no subprocess -
# the coverage RUN is kstrl.verify.check_patch_coverage.
# ---------------------------------------------------------------------------
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _hunk_start(line: str) -> int:
    """The new-side first line number of a hunk header, or 0."""
    match = _HUNK_RE.match(line)
    return int(match.group(1)) if match else 0


def added_line_numbers(diff_text: str) -> dict[str, set[int]]:
    """New-side line numbers a unified diff ADDS, keyed by new-side path.

    D4: measured byte-identical whether the diff was fetched at ``-U0``
    or the default ``-U3`` (:func:`kstrl.git.get_diff_content`'s context
    size; see the PR body), so no new ``-U0`` git helper is added to
    :mod:`kstrl.git`.

    Header parsing is :func:`_iter_diff_lines`, the same walk
    :func:`analyze_test_diff` uses: a ``+++ `` line (note the trailing
    space) immediately after a ``--- `` line is a file header, never a
    diff line that merely happens to start with the same three
    characters. Both halves are load-bearing - the trailing space rules
    out a plain ``line.startswith("+++")`` match, and the ``prev`` guard
    rules out an ADDED line whose content looks like a header.
    """
    added: dict[str, set[int]] = {}
    path = ""
    lineno = 0
    prev = ""
    # The source path (a DELETED file's identity) contributes no ADDED
    # line either way, so it is unused here on purpose.
    for _source, target, line in _iter_diff_lines(diff_text):
        if line.startswith("diff --git"):
            path, lineno = "", 0
        elif line.startswith("+++ ") and prev.startswith("--- "):
            path, lineno = ("" if target == _DEV_NULL else target), 0
        elif line.startswith("@@"):
            lineno = _hunk_start(line)
        elif path and lineno:
            lineno = _record_added_line(line, lineno, path, added)
        prev = line
    return added


def _record_added_line(line: str, lineno: int, path: str, added: dict[str, set[int]]) -> int:
    """Record ``lineno`` under ``path`` when ``line`` is a ``+`` line; return
    the new-side line number the NEXT line will carry."""
    if line.startswith("+"):
        added.setdefault(path, set()).add(lineno)
        return lineno + 1
    if line.startswith("-") or line.startswith("\\"):
        return lineno
    return lineno + 1


def coverage_targets(diff_text: str) -> dict[str, set[int]]:
    """:func:`added_line_numbers`, filtered to non-test ``.py`` files.

    ONE home for the rule: :func:`kstrl.verify.check_patch_coverage`
    needs it BEFORE it spends a second full test run (the pre-flight
    ``no_target`` check), and :func:`measure_patch_coverage` needs the
    identical set AFTER the run to know which files and lines the ratio
    is over. Two copies is how the pre-flight and the arithmetic come to
    disagree about what counts.

    D6, measured on the fixture in ``tests/test_patch_coverage.py``:
    including the changed TEST file moves the number from 50.0% to
    66.7% (see the PR body), because a test file is close to 100%
    covered by construction, so it uses :func:`is_test_path`, adequacy's
    own canonical rule and already what
    :func:`kstrl.verify.check_test_adequacy` uses.
    """
    added = added_line_numbers(diff_text)
    return {
        path: lines
        for path, lines in added.items()
        if path.endswith(".py") and not is_test_path(path)
    }


@dataclass(frozen=True)
class PatchCoverage:
    """The result of measuring patch coverage over one diff.

    ``covered_lines`` is, per measured target, the changed lines that
    EXECUTED - the actual line numbers, not only a count. Layer 2
    (diff-scoped mutation, not yet built) needs to know WHICH lines to
    mutate, not only how many; #152's simplify pass adds this field so
    that data exists the first time it is needed rather than being
    reconstructed from the diff and the report a second time.

    ``files`` is sorted ``(path, covered, total)`` tuples, one per target
    file the coverage report actually measured; its covered count for a
    path is ``len(...)`` of that path's ``covered_lines`` entry, so the
    two never disagree because they come from the same computation.
    ``unmeasured`` names targets the coverage report has no usable entry
    for at all - present in the diff but absent from (or malformed in)
    the report, which D5 treats as a THIRD state, not 0% and not
    skipped.

    No ``percent`` property: the only caller, ``verify.check_patch_coverage``,
    already returns ``NotMeasured(no_target)`` before computing a
    percentage whenever ``total == 0``, so a property that exists only to
    raise on that case never has anything left to catch. Compute
    ``100.0 * covered / total`` at the call site, after that guard.
    """

    covered: int
    total: int
    covered_lines: tuple[tuple[str, frozenset[int]], ...]
    files: tuple[tuple[str, int, int], ...]
    unmeasured: tuple[str, ...]


def _int_set(entry: object, key: str) -> set[int] | None:
    """The ints at ``entry[key]``, or ``None`` if the shape is wrong.

    ``entry`` is ``object`` all the way down because it comes from a JSON
    file this code did not write - a coverage report from a pytest-cov
    version this was never tested against is not required to carry the
    shape expected here.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get(key)
    if not isinstance(value, list):
        return None
    return {item for item in value if isinstance(item, int)}


def measure_patch_coverage(
    targets: Mapping[str, set[int]], report: Mapping[str, object]
) -> PatchCoverage:
    """The patch-coverage ratio: covered changed lines over changed
    executable lines, restricted to ``targets``.

    ``report`` is a parsed ``coverage json`` document. Keys inside
    ``report["files"]`` are repo-relative POSIX paths - the same
    spelling ``git diff`` produces - with at most a leading ``./``
    stripped; critic-verified against a real report that no further
    normalisation is needed.

    D5: a target whose added lines carry no statement (a comment-only or
    blank-only change) drops out of BOTH sides of the ratio rather than
    counting as 0/0 or 100%. The same rule handles ``# pragma: no
    cover`` lines with no special case: coverage puts them in
    ``excluded_lines``, which is neither ``executed_lines`` nor
    ``missing_lines``, so they are absent from both sides of the
    intersection below without anything here naming them.

    A target present in ``targets`` but with no usable entry in
    ``report["files"]`` is UNMEASURED, not 0%: the coverage tool simply
    never produced a reading for it, which is a different fact than "it
    ran and nothing executed".
    """
    raw_files = report.get("files")
    files: dict[str, object] = raw_files if isinstance(raw_files, dict) else {}
    normalized = {(key[2:] if key.startswith("./") else key): value for key, value in files.items()}

    covered = 0
    total = 0
    covered_lines: list[tuple[str, frozenset[int]]] = []
    line_totals: dict[str, int] = {}
    unmeasured: list[str] = []
    for path, lines in sorted(targets.items()):
        entry = normalized.get(path)
        executed = _int_set(entry, "executed_lines")
        missing = _int_set(entry, "missing_lines")
        if executed is None or missing is None:
            unmeasured.append(path)
            continue
        hit = lines & executed
        t = len(lines & (executed | missing))
        if t == 0:
            continue
        covered_lines.append((path, frozenset(hit)))
        line_totals[path] = t
        covered += len(hit)
        total += t

    # `files` is DERIVED from `covered_lines` (#152 simplify pass), not a
    # second independent computation: its covered count for a path is
    # `len(...)` of that path's `covered_lines` entry, so a defect that
    # breaks one breaks the other rather than the two silently disagreeing.
    per_file = tuple((path, len(hit), line_totals[path]) for path, hit in covered_lines)
    return PatchCoverage(covered, total, tuple(covered_lines), per_file, tuple(unmeasured))


# ---------------------------------------------------------------------------
# R8.5 Layer 2 (#152): diff-scoped mutation. Pure functions only - the
# subprocess handling around mutmut lives in
# :func:`kstrl.verify.check_diff_mutation`, for the same reason Layer 1's
# split does (see :func:`measure_patch_coverage`'s module-docstring
# paragraph): this module has no subprocess or I/O dependency today.
# ---------------------------------------------------------------------------
def mutation_patch(targets: Mapping[str, Collection[int]]) -> str:
    """A synthetic unified diff naming exactly ``targets`` as added lines.

    mutmut 2.5.1's ``--use-patch-file`` reads ONLY the new-side line
    numbers of ``+`` lines (``read_patch_data``; measurements.md section
    2a), so a patch naming exactly the changed-and-covered lines selects
    exactly those lines for mutation and nothing else - not the rest of
    the target file, and not an untouched one.

    No ``a/``/``b/`` prefixes. whatthepatch strips them only when the
    document also carries a git ``index`` line; with the prefixes present
    ``+++ b/mod.py`` resolves to the path ``b/mod.py``, which then
    selects nothing at all (measurements 2c, verified end to end). The
    ``+x`` line's content is never read - ``read_patch_data`` keeps only
    ``change.new``, the new-side line number - so ``x`` is a deliberate
    placeholder, not a value to improve.

    One header pair per file, files sorted, and one one-line hunk per
    line, lines sorted, so the output is deterministic given ``targets``.
    Each hunk is ``@@ -<n-1>,0 +<n>,1 @@``, which stays valid at ``n ==
    1`` (``@@ -0,0 +1,1 @@``): an added covered line can be a module's
    first line.
    """
    pieces: list[str] = []
    for path in sorted(targets):
        numbers = sorted(set(targets[path]))
        if not numbers:
            continue
        pieces.append(f"--- {path}\n+++ {path}\n")
        for lineno in numbers:
            pieces.append(f"@@ -{lineno - 1},0 +{lineno},1 @@\n+x\n")
    return "".join(pieces)


#: A mutant mutmut ran and the runner's test suite killed - the change it
#: made produced a test failure, so the mutant is detected.
MUTANT_KILLED: str = "killed"
#: A mutant mutmut ran and the runner's test suite did NOT catch - the
#: mutated code passed every test unchanged.
MUTANT_SURVIVED: str = "survived"
#: mutmut generated the mutant but a truncated run never got to it.
MUTANT_UNTESTED: str = "untested"
#: Any status this code does not classify as one of the three above - a
#: timeout, a suspicious result, or a message this code has never seen.
#: Never treated as killed: an inconclusive result is not a detection.
MUTANT_INCONCLUSIVE: str = "inconclusive"

#: ``mutmut junitxml`` names each mutant ``Mutant #<id>``.
_MUTANT_NAME_RE = re.compile(r"^Mutant #(\d+)$")


@dataclass(frozen=True)
class Mutant:
    """One mutant, as reported by ``mutmut junitxml``."""

    mutant_id: int
    path: str
    line: int
    status: str


def _mutant_status(testcase: ET.Element) -> str:
    """The status of one ``<testcase>`` element, per measurements 2e/2g.

    | mutmut status | junitxml rendering |
    |---|---|
    | ``ok_killed`` | bare ``<testcase>``, no child element |
    | ``bad_survived`` | ``<failure message="bad_survived">`` |
    | ``bad_timeout`` | ``<error type="timeout" message="bad_timeout">`` |
    | ``untested`` (with ``--untested-policy=error``) | ``<error message="untested">`` |
    | ``skipped`` | nothing, ever - identical to killed |

    A ``<failure>`` whose message is not ``"bad_survived"``, or an
    ``<error>`` whose message is not ``"untested"``, is a status this
    code has never seen and is reported :data:`MUTANT_INCONCLUSIVE`
    rather than guessed at. ``skipped`` is unreachable except through a
    project's own ``mutmut_config.py`` (measurements 2g), which
    :func:`kstrl.verify.check_diff_mutation` refuses before any spawn.
    """
    failure = testcase.find("failure")
    if failure is not None:
        return MUTANT_SURVIVED if failure.get("message") == "bad_survived" else MUTANT_INCONCLUSIVE
    error = testcase.find("error")
    if error is not None:
        return MUTANT_UNTESTED if error.get("message") == "untested" else MUTANT_INCONCLUSIVE
    return MUTANT_KILLED


def parse_mutant_report(xml_text: str) -> tuple[Mutant, ...]:
    """Every mutant in a ``mutmut junitxml --untested-policy=error
    --suspicious-policy=error`` report, in document order.

    Raises :class:`ValueError` for any report this cannot read in full -
    malformed XML (:class:`xml.etree.ElementTree.ParseError`, re-raised
    as :class:`ValueError` so one clause covers both), a ``<testcase>``
    whose ``name`` does not match ``Mutant #<int>``, or one missing a
    usable ``file`` or ``line`` attribute. A testcase this cannot read is
    a REFUSAL, never a silently dropped row: dropping one is how a
    denominator shrinks with nothing failing (CLAUDE.md).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"mutmut junitxml report is not valid XML: {exc}") from exc

    mutants: list[Mutant] = []
    for testcase in root.iter("testcase"):
        name = testcase.get("name") or ""
        match = _MUTANT_NAME_RE.match(name)
        if match is None:
            raise ValueError(f"mutmut junitxml testcase has no readable id: name={name!r}")
        path = testcase.get("file")
        if not path:
            raise ValueError(f"mutmut junitxml testcase {name!r} has no file attribute")
        line_text = testcase.get("line")
        try:
            line = int(line_text) if line_text is not None else None
        except ValueError:
            line = None
        if line is None:
            raise ValueError(f"mutmut junitxml testcase {name!r} has no readable line attribute")
        mutants.append(Mutant(int(match.group(1)), path, line, _mutant_status(testcase)))
    return tuple(mutants)


#: Statuses a line's SELECTED mutant must carry (D4): the two mutmut
#: itself distinguishes as an outcome, never :data:`MUTANT_UNTESTED` or
#: :data:`MUTANT_INCONCLUSIVE`.
_DEFINITE_STATUSES = (MUTANT_KILLED, MUTANT_SURVIVED)


@dataclass(frozen=True)
class MutationScore:
    """The result of scoring a mutmut report against ``targets``.

    ``target_lines`` (L) is what kstrl asked mutmut to mutate, known
    before the run. ``mutable_lines`` (G) is how many of those lines the
    report carries at least one mutant for - a ``def`` line, for
    instance, mutmut simply cannot mutate, so ``G <= L`` even on a
    complete run. ``measured_lines`` (M) is how many of THOSE have a
    selected mutant with a definite status; ``M < G`` is what a truncated
    run looks like. ``killed_lines`` (K) is the killed subset of
    ``measured_lines``. ``survivors`` is every line whose selected mutant
    survived, sorted ``(path, line)`` - one entry per LINE, because the
    line is the actionable unit and the unit the score counts.
    """

    target_lines: int
    mutable_lines: int
    measured_lines: int
    killed_lines: int
    survivors: tuple[tuple[str, int], ...]


def score_mutants(
    targets: Mapping[str, Collection[int]], mutants: Iterable[Mutant]
) -> MutationScore:
    """Score ``mutants`` against ``targets``, one verdict per line (D4).

    mutmut's own scoping is never trusted for the number (D3): a mutant
    outside ``targets`` - mutmut mutating a line kstrl did not ask for -
    is dropped here, silently, because the synthetic patch is a request,
    not a guarantee, and it is this function's job to hold the line even
    if mutmut's did not.

    mutmut has no per-line cap (measurements 2d: one line produced three
    mutants), so the denominator here is LINES, not mutants. For each
    target line, only that line's mutants with a DEFINITE status
    (:data:`MUTANT_KILLED` or :data:`MUTANT_SURVIVED`) are candidates;
    the SELECTED mutant is the one among those with the lowest
    ``mutant_id``, and the line's verdict is that mutant's status. A line
    with no definite-status mutant is not measured at all - not in
    ``measured_lines``, not in ``killed_lines``. Lowest id rather than
    any, because mutmut generates and runs mutants in id order, so lowest
    is deterministic given the source; "lowest with a definite status"
    rather than bare-lowest, because a truncated run leaves ``untested``
    rows at low ids (measurements 2f run A), and taking the bare lowest
    there would throw away a line whose other mutants actually ran.
    """
    by_line: dict[tuple[str, int], list[Mutant]] = {}
    for mutant in mutants:
        lines = targets.get(mutant.path)
        if lines is None or mutant.line not in lines:
            continue
        by_line.setdefault((mutant.path, mutant.line), []).append(mutant)

    measured_lines = 0
    killed_lines = 0
    survivors: list[tuple[str, int]] = []
    for (path, line), line_mutants in by_line.items():
        definite = [m for m in line_mutants if m.status in _DEFINITE_STATUSES]
        if not definite:
            continue
        selected = min(definite, key=lambda m: m.mutant_id)
        measured_lines += 1
        if selected.status == MUTANT_KILLED:
            killed_lines += 1
        else:
            survivors.append((path, line))

    return MutationScore(
        target_lines=sum(len(set(lines)) for lines in targets.values()),
        mutable_lines=len(by_line),
        measured_lines=measured_lines,
        killed_lines=killed_lines,
        survivors=tuple(sorted(survivors)),
    )


# ---------------------------------------------------------------------------
# Config and level-gated severity
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdequacyConfig:
    """``[adequacy]`` config for the R8.5 gate.

    Opt-in, and advisory-first even once enabled, per the roadmap: the
    thresholds these layers want (mutation floors especially) have to be
    set from an empirical distribution that does not exist yet, and a
    gate that blocks on an invented number teaches people to disable
    gates.
    """

    enabled: bool = False
    #: Layer 0 severity floor. "advisory" records findings without
    #: failing; "block" fails Phase 1. The autonomy level can RAISE this
    #: (see severity_for_level) but never lower it.
    layer0: str = "advisory"
    #: Require at least one strong-oracle assertion per NEW test file.
    require_strong_oracle: bool = True
    #: Report tests that assert nothing at all.
    flag_assertionless_tests: bool = True
    #: R8.5 Layer 1. Runs the project's pytest command a SECOND time under
    #: coverage and records what fraction of the lines this change ADDED
    #: was executed. Advisory always: there is no floor key and no level
    #: can create one. Off by default because it costs a second full test
    #: run.
    patch_coverage: bool = False
    #: R8.5 Layer 2 (#152). Mutates the changed AND covered lines this
    #: change added and reports what fraction of them the suite detects a
    #: change to. Needs `patch_coverage` for the coverage half, rewrites
    #: source while it runs, and is capped by [verify] mutation_timeout.
    #: Advisory always: no floor key, and no level can create one.
    diff_mutation: bool = False

    @classmethod
    def from_env(cls) -> AdequacyConfig:
        import os

        defaults = cls()
        enabled = os.environ.get("KSTRL_ADEQUACY_ENABLED")
        layer0 = os.environ.get("KSTRL_ADEQUACY_LAYER0")
        return cls(
            enabled=defaults.enabled if enabled is None else enabled == "1",
            layer0=layer0 or defaults.layer0,
            require_strong_oracle=defaults.require_strong_oracle,
            flag_assertionless_tests=defaults.flag_assertionless_tests,
            patch_coverage=defaults.patch_coverage,
            diff_mutation=defaults.diff_mutation,
        )

    @classmethod
    def load(cls, root_dir: object = None) -> AdequacyConfig:
        """Precedence: env > toml > defaults; reads ``[adequacy]``."""
        import os
        from pathlib import Path

        from kstrl.config import load_toml_section, resolve_config_file

        base = Path(root_dir) if root_dir is not None else Path.cwd()  # type: ignore[arg-type]
        section = load_toml_section(resolve_config_file(base), "adequacy")
        defaults = cls()
        enabled = bool(section["enabled"]) if "enabled" in section else defaults.enabled
        layer0 = str(section["layer0"]) if "layer0" in section else defaults.layer0
        require_strong = (
            bool(section["require_strong_oracle"])
            if "require_strong_oracle" in section
            else defaults.require_strong_oracle
        )
        flag_assertionless = (
            bool(section["flag_assertionless_tests"])
            if "flag_assertionless_tests" in section
            else defaults.flag_assertionless_tests
        )
        patch_coverage = (
            bool(section["patch_coverage"])
            if "patch_coverage" in section
            else defaults.patch_coverage
        )
        diff_mutation = (
            bool(section["diff_mutation"]) if "diff_mutation" in section else defaults.diff_mutation
        )
        if "KSTRL_ADEQUACY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_ADEQUACY_ENABLED"] == "1"
        if "KSTRL_ADEQUACY_LAYER0" in os.environ:
            layer0 = os.environ["KSTRL_ADEQUACY_LAYER0"]
        return check_numbers(
            cls(
                enabled=enabled,
                layer0=layer0,
                require_strong_oracle=require_strong,
                flag_assertionless_tests=flag_assertionless,
                patch_coverage=patch_coverage,
                diff_mutation=diff_mutation,
            )
        )

    def __post_init__(self) -> None:
        if self.layer0 not in ("advisory", "block"):
            raise ValueError(
                f"invalid [adequacy] layer0 {self.layer0!r}; expected 'advisory' or 'block'"
            )
        if self.diff_mutation and not self.patch_coverage:
            raise ValueError(
                "[adequacy] diff_mutation needs patch_coverage = true: Layer 2 "
                "mutates only the changed lines Layer 1 measured as covered, and "
                "it runs no coverage run of its own"
            )


def layer0_blocks(config: AdequacyConfig, autonomy_level: int) -> bool:
    """Whether Layer 0 findings should FAIL Phase 1 at this level.

    The roadmap's level table: Layer 0 blocking from L1 up. The config
    can opt in early (``layer0 = "block"`` with the ladder off), and the
    ladder can force it on, but neither can turn it off once the other
    wants it - autonomy is allowed to tighten a gate, never to loosen
    one, which is the same direction R8.2's bundle clamps in.
    """
    if config.layer0 == "block":
        return True
    # autonomy_level == 0 means the R8.2 ladder is off, so only the
    # explicit config opt-in above can block. With the ladder on, the
    # roadmap's level table makes Layer 0 blocking from L1 upward: it is
    # the cheapest layer and the one an agent is most able to game, so
    # it is the floor rather than a perk of higher autonomy.
    return autonomy_level >= 1


def evaluate_layer0(
    diff_text: str,
    test_sources: dict[str, str],
    config: AdequacyConfig,
    *,
    new_paths: set[str],
) -> list[AdequacyFinding]:
    """Layer 0 findings for one change.

    ``test_sources`` maps changed test paths to their CURRENT content;
    the caller reads them, so this stays free of file I/O and trivially
    testable.

    ``new_paths`` are the paths git reports as ADDED (status ``A``). It is
    required rather than defaulted because the distinction decides what is
    fair to ask of a file: an added file is entirely this change's work,
    so the whole-file oracle floor applies to it; a modified file carries
    tests nobody here wrote, and holding an editor responsible for a
    legacy file's weak oracles turns a one-line edit into a blocked run.
    Modified files still get every diff-discipline check, plus the
    assertionless check on the test defs THIS diff added.
    """
    findings: list[AdequacyFinding] = []
    discipline = analyze_test_diff(diff_text)

    for path, name in discipline.deleted_tests():
        findings.append(
            AdequacyFinding(
                kind=FindingKind.TEST_DELETED,
                path=path,
                symbol=name,
                detail=(
                    "test removed by this diff; deleting a test is a change to "
                    "what the suite guarantees and needs a spec-linked reason"
                ),
            )
        )
    for path, marker in discipline.added_skips:
        findings.append(
            AdequacyFinding(
                kind=FindingKind.TEST_SKIPPED,
                path=path,
                detail=f"skip/xfail added: {marker}",
            )
        )
    for path, lost in discipline.net_assertion_loss().items():
        findings.append(
            AdequacyFinding(
                kind=FindingKind.ASSERTION_REMOVED,
                path=path,
                detail=(
                    f"{lost} more assertion line(s) removed than added; if this "
                    "is a consolidation say so, otherwise the suite got weaker"
                ),
            )
        )

    for path, source in sorted(test_sources.items()):
        report = lint_test_source(path, source)
        if report.parse_error or not report.tests:
            continue
        is_new = path in new_paths
        if config.require_strong_oracle and is_new and report.strength is not OracleStrength.STRONG:
            findings.append(
                AdequacyFinding(
                    kind=(
                        FindingKind.NO_ORACLE
                        if report.strength is OracleStrength.NONE
                        else FindingKind.WEAK_ORACLE
                    ),
                    path=path,
                    detail=(
                        f"new test file: {report.tests} test(s), none with a "
                        "strong-oracle assertion (a comparison against an "
                        "expected value, or an asserted exception). Shape-only "
                        "checks like `is not None` pass for a wrong answer too"
                    ),
                )
            )
        added_here = {name for (other, name) in discipline.added_tests if other == path}
        if config.flag_assertionless_tests:
            # In a NEW file every test is this change's; in a modified one
            # only the defs this diff added are, and reporting the rest
            # would flag a legacy file for being edited.
            silent = (
                report.without_assertions
                if is_new
                else [name for name in report.without_assertions if name in added_here]
            )
            if silent:
                findings.append(
                    AdequacyFinding(
                        kind=FindingKind.NO_ORACLE,
                        path=path,
                        symbol=", ".join(silent[:5]),
                        detail=(
                            f"{len(silent)} test(s) assert nothing; they pass "
                            "unless the code raises"
                        ),
                    )
                )
        # Collected skips were previously dropped on the floor. A skipped
        # test guarantees nothing, whether the skip arrived in this diff
        # (reported above, with the line as evidence) or was already
        # there in a file this change adds.
        skips = (
            report.skipped
            if is_new
            else [
                (name, marker)
                for (name, marker) in report.skipped
                # A module-wide marker counts on a modified file only when
                # this diff added tests under it: those tests are dead on
                # arrival. A pre-existing skip in a file you merely edited
                # is not something this change did.
                if name in added_here or (name == _MODULE_SKIP_SYMBOL and added_here)
            ]
        )
        if skips:
            names = [name for name, _ in skips]
            markers = ", ".join(sorted({marker for _, marker in skips}))
            what = (
                f"module-level marker disables every test in this file ({markers})"
                if _MODULE_SKIP_SYMBOL in names
                else f"{len(names)} test(s) disabled in this file ({markers})"
            )
            findings.append(
                AdequacyFinding(
                    kind=FindingKind.TEST_SKIPPED,
                    path=path,
                    symbol=", ".join(names[:5]),
                    detail=(f"{what}; a skipped test is a guarantee the suite no longer makes"),
                )
            )
    # Two detectors can see the same skip (the diff line and the file's
    # AST); identical findings collapse.
    unique: list[AdequacyFinding] = []
    seen: set[AdequacyFinding] = set()
    for finding in findings:
        if finding not in seen:
            seen.add(finding)
            unique.append(finding)
    return unique
