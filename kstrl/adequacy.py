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

1. **Did this change weaken the existing suite?** Deleted tests,
   newly-skipped tests, and assertions replaced by nothing are all
   legitimate sometimes and suspicious always, so they are reported with
   the evidence rather than silently allowed.
2. **Do the NEW tests actually assert anything?** A test whose only
   assertion is ``assert result is not None`` passes for almost any
   implementation, including a broken one. The taxonomy below separates
   assertions that could FAIL from assertions that merely execute.

What it deliberately does NOT do: judge whether an assertion's expected
value is CORRECT. Nothing static can - that is what the fixtures oracle
(Layer 3) and spec-derived criteria are for. This layer only
distinguishes "asserts something falsifiable" from "asserts nothing",
which is a lower bar and a much more reliable signal.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Test-file path fragments. Deliberately broad: a file that looks like a
#: test to a human should be judged as one, and a false positive here
#: costs a note, while a false negative silently exempts a file.
TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")

class OracleStrength(StrEnum):
    """How much a test file's assertions could actually catch.

    Named after the W1-W5 / S1-S3 taxonomy in arXiv:2606.18168, reduced
    to the three distinctions this layer can make statically.
    """

    STRONG = "strong"      # compares against an expected value or raises
    WEAK = "weak"          # executes, asserts only shape or truthiness
    NONE = "none"          # no assertion at all


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
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else ""
        )
        return name in {"isinstance", "issubclass", "callable", "hasattr"}
    # A bare name or attribute: `assert result`
    return isinstance(node, (ast.Name, ast.Attribute))


def _classify_assert(node: ast.Assert) -> OracleStrength:
    test = node.test
    if _is_vacuous_constant(test):
        return OracleStrength.WEAK
    if _is_shape_only(test):
        return OracleStrength.WEAK
    if isinstance(test, ast.BoolOp):
        # `assert a and b` is only as strong as its strongest operand.
        strengths = [
            _classify_assert(ast.Assert(test=value, msg=None))
            for value in test.values
        ]
        return (
            OracleStrength.STRONG
            if OracleStrength.STRONG in strengths
            else OracleStrength.WEAK
        )
    return OracleStrength.STRONG


def _has_raises_context(node: ast.AST) -> bool:
    """`with pytest.raises(...)` is a strong oracle: it names the failure."""
    for child in ast.walk(node):
        if not isinstance(child, ast.With):
            continue
        for item in child.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                func = call.func
                attr = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else ""
                )
                if attr in {"raises", "warns", "assertRaises"}:
                    return True
    return False


def _skip_marker(node: ast.AST) -> str:
    """The name of a skip/xfail decorator on this node, or ""."""
    decorators = getattr(node, "decorator_list", [])
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        joined = ".".join(reversed(parts))
        if any(token in joined for token in ("skip", "xfail")):
            return joined
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

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        report.tests += 1

        marker = _skip_marker(node)
        if marker:
            report.skipped.append((node.name, marker))

        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        strengths = [_classify_assert(a) for a in asserts]
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
_SKIP_RE = re.compile(r"@\w[\w.]*\.(skip|skipif|xfail)\b|@(skip|skipif|xfail)\b")


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
            (path, name) for (path, name) in self.removed_tests
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


def analyze_test_diff(diff_text: str) -> DiffDiscipline:
    """Extract suite-weakening signals from a unified diff."""
    result = DiffDiscipline()
    current = ""
    prev = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            current = ""
        elif line.startswith("+++ ") and prev.startswith("--- "):
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            current = "" if target == "/dev/null" else target
        elif current and is_test_path(current):
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
                if _SKIP_RE.search(body):
                    result.added_skips.append((current, body.strip()))
                if _ASSERT_RE.match(body):
                    result.added_assertions[current] = (
                        result.added_assertions.get(current, 0) + 1
                    )
        prev = line
    return result


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
        enabled = (
            bool(section["enabled"]) if "enabled" in section else defaults.enabled
        )
        layer0 = (
            str(section["layer0"]) if "layer0" in section else defaults.layer0
        )
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
        if "KSTRL_ADEQUACY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_ADEQUACY_ENABLED"] == "1"
        if "KSTRL_ADEQUACY_LAYER0" in os.environ:
            layer0 = os.environ["KSTRL_ADEQUACY_LAYER0"]
        return cls(
            enabled=enabled,
            layer0=layer0,
            require_strong_oracle=require_strong,
            flag_assertionless_tests=flag_assertionless,
        )

    def __post_init__(self) -> None:
        if self.layer0 not in ("advisory", "block"):
            raise ValueError(
                f"invalid [adequacy] layer0 {self.layer0!r}; "
                "expected 'advisory' or 'block'"
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
) -> list[AdequacyFinding]:
    """Layer 0 findings for one change.

    ``test_sources`` maps changed test paths to their CURRENT content;
    the caller reads them, so this stays free of file I/O and trivially
    testable.
    """
    findings: list[AdequacyFinding] = []
    discipline = analyze_test_diff(diff_text)

    for path, name in discipline.deleted_tests():
        findings.append(AdequacyFinding(
            kind=FindingKind.TEST_DELETED, path=path, symbol=name,
            detail=(
                "test removed by this diff; deleting a test is a change to "
                "what the suite guarantees and needs a spec-linked reason"
            ),
        ))
    for path, marker in discipline.added_skips:
        findings.append(AdequacyFinding(
            kind=FindingKind.TEST_SKIPPED, path=path,
            detail=f"skip/xfail added: {marker}",
        ))
    for path, lost in discipline.net_assertion_loss().items():
        findings.append(AdequacyFinding(
            kind=FindingKind.ASSERTION_REMOVED, path=path,
            detail=(
                f"{lost} more assertion line(s) removed than added; if this "
                "is a consolidation say so, otherwise the suite got weaker"
            ),
        ))

    for path, source in sorted(test_sources.items()):
        report = lint_test_source(path, source)
        if report.parse_error or not report.tests:
            continue
        if config.require_strong_oracle and report.strength is not (
            OracleStrength.STRONG
        ):
            findings.append(AdequacyFinding(
                kind=(
                    FindingKind.NO_ORACLE
                    if report.strength is OracleStrength.NONE
                    else FindingKind.WEAK_ORACLE
                ),
                path=path,
                detail=(
                    f"{report.tests} test(s), none with a strong-oracle "
                    "assertion (a comparison against an expected value, or "
                    "an asserted exception). Shape-only checks like "
                    "`is not None` pass for a wrong answer too"
                ),
            ))
        if config.flag_assertionless_tests and report.without_assertions:
            findings.append(AdequacyFinding(
                kind=FindingKind.NO_ORACLE, path=path,
                symbol=", ".join(report.without_assertions[:5]),
                detail=(
                    f"{len(report.without_assertions)} test(s) assert "
                    "nothing; they pass unless the code raises"
                ),
            ))
    return findings
