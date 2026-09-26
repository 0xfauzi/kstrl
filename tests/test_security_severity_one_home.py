"""The security severity ranking has one definition (#550).

``kstrl.security._SEVERITY_ORDER`` ranks critical, high, medium and low.
Phase 2.5's verdict, its fail count and the journal read it (#524). The
calibration scorer kept its own copy, so an edit to one ranking would
leave the scorer grading against the other with no message, and the
``--security-fail-threshold`` choices were a third copy.

Two layers:

- The behaviour tests edit the one ranking and assert the gate AND the
  scorer both move, driving the real parser on a reply and the saved
  fixture's own requirement. The CLI test reads ``ks factory --help``.
- The census walks every collection literal in ``kstrl/`` and counts
  those whose string members include all four severities. A new copy
  shows up as a census delta. The census is closed over collection
  literals; the shapes it cannot see are recorded below as strict
  xfails, so a widening that starts seeing them fails loudly.

``tests/`` is not walked. A test's literal list of severities is an
oracle that is independent of the code on purpose
(``tests/test_security_fail_count.py::ALL_FOUR`` pins counts 1 to 4
against it); deriving it from ``_SEVERITY_ORDER`` would make the test
agree with whatever the code says.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl import security
from kstrl.calibration_score import security_caught, security_false_positive
from kstrl.cli import cli
from kstrl.security import SecurityMode, SecurityResult, parse_security_output
from tests.helpers.astwalk import all_nodes, assert_census, blind_spot, package_sources, parse

FIXTURES = Path(__file__).resolve().parent / "adversarial_fixtures"
SEVERITIES = frozenset({"critical", "high", "medium", "low"})

# Every module in kstrl/ that holds a collection literal naming all four
# severities, and how many, keyed as ``astwalk.label`` names it (relative
# to kstrl/). Re-derive by running the census, never by editing this
# literal.
EXPECTED_SITES = {
    # The one ranking.
    "security.py": 1,
    # A display style per severity for BOTH vocabularies (security's four
    # and review's fail/advisory). It ranks nothing.
    "tui/widgets/findings_table.py": 1,
}

# One source per shape the census must see. They are the census's
# controls AND the parametrized positive control below.
SHAPES = (
    '_R = {"critical": 3, "high": 2, "medium": 1, "low": 0}',
    '_R = {"low": 0, "medium": 1, "high": 2, "critical": 3}',
    '_R = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}',
    'CHOICES = ["critical", "high", "medium", "low"]',
    'ORDER = ("critical", "high", "medium", "low")',
    'VALID = frozenset({"critical", "high", "medium", "low"})',
    "_R = dict(critical=3, high=2, medium=1, low=0)",
    '_R = dict([("critical", 3), ("high", 2), ("medium", 1), ("low", 0)])',
    'def f():\n    return {"critical": 3, "high": 2, "medium": 1, "low": 0}',
)


def _string(node: ast.AST | None) -> str | None:
    """A string constant, or the first element of a pair that starts with one."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        node = node.elts[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _members(node: ast.AST) -> set[str]:
    """The string members of a collection literal; empty for any other node.

    A dict contributes its keys, a list, tuple or set its elements (the
    first element of a pair, so ``dict([("critical", 3), ...])`` counts),
    and a call its keyword names (``dict(critical=3, ...)``).
    """
    if isinstance(node, ast.Call):
        return {kw.arg for kw in node.keywords if kw.arg is not None}
    if isinstance(node, ast.Dict):
        items: list[ast.expr | None] = list(node.keys)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = list(node.elts)
    else:
        return set()
    return {text for item in items if (text := _string(item)) is not None}


def _names_every_severity(node: ast.AST) -> bool:
    return SEVERITIES <= _members(node)


def _ranking_sites(source: str) -> int:
    """How many collection literals in ``source`` name all four severities."""
    return sum(1 for node in all_nodes(parse(source)) if _names_every_severity(node))


def test_the_security_severity_ranking_has_one_home() -> None:
    assert_census(
        sources=package_sources(),
        sees=_names_every_severity,
        expected=EXPECTED_SITES,
        control=SHAPES,
        message=(
            "A collection literal in kstrl/ names all four security severities. "
            "Read kstrl.security._SEVERITY_ORDER instead of copying it (#550)."
        ),
    )


@pytest.mark.parametrize("source", SHAPES)
def test_the_census_sees_each_shape_of_a_copy(source: str) -> None:
    assert _ranking_sites(source) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: a ranking built from one string constant is not a collection literal",
)
def test_blind_spot_a_ranking_split_from_one_string() -> None:
    blind_spot(_ranking_sites, 'ORDER = "critical high medium low".split()')


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="blind spot: an Enum's members are class attributes, not a collection literal",
)
def test_blind_spot_a_ranking_as_an_enum() -> None:
    blind_spot(
        _ranking_sites,
        "class Sev(IntEnum):\n    critical = 3\n    high = 2\n    medium = 1\n    low = 0\n",
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "blind spot: a copy that leaves out the lowest severity and relies on "
        ".get(x, 0) names three of the four. Lowering the census to three "
        "flags tui/screens/init_wizard.py's REASONING_LEVELS, which is not a ranking"
    ),
)
def test_blind_spot_a_ranking_missing_its_zero_rank() -> None:
    blind_spot(_ranking_sites, '_R = {"critical": 3, "high": 2, "medium": 1}')


def _meta(family: str, name: str) -> dict[str, object]:
    path = FIXTURES / family / f"{name}.meta.json"
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _parsed(category: str, severity: str, location: str) -> SecurityResult:
    """A reviewer reply with one finding, through the real parser."""
    reply = json.dumps(
        {
            "findings": [
                {
                    "category": category,
                    "severity": severity,
                    "location": location,
                    "explanation": "user input reaches the query unescaped",
                }
            ],
            "exhaustively_searched": True,
        }
    )
    return parse_security_output(reply, SecurityMode.ADVISORY.value)


def _gate_fails(result: SecurityResult, threshold: str) -> bool:
    judged = dataclasses.replace(result, mode=SecurityMode.HARD.value, fail_threshold=threshold)
    return judged.fail_count == 1


def test_the_scorer_detects_by_the_gates_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rank medium with high in the one ranking: the gate at threshold high
    now fails a medium finding, and the scorer must count the same finding
    as meeting the fixture's ``severity_at_least: high``."""
    requirement = _meta("security", "01_sql_injection")["must_detect"]
    assert isinstance(requirement, dict)
    result = _parsed("injection", "medium", "src/users.py:11")
    assert not _gate_fails(result, "high")
    assert security_caught(result, requirement)[0] is False

    monkeypatch.setitem(security._SEVERITY_ORDER, "medium", 2)

    assert _gate_fails(result, "high")
    assert security_caught(result, requirement)[0] is True


def test_the_scorer_flags_false_positives_by_the_gates_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rank low with medium in the one ranking: the gate at threshold medium
    now fails a low finding, and the scorer must count the same finding on
    a negative fixture (floor ``medium``) as a false positive."""
    requirement = _meta("security_negative", "01_parameterized_dynamic_sql")["must_not_flag"]
    assert isinstance(requirement, dict)
    result = _parsed("injection", "low", "src/products.py:20")
    assert not _gate_fails(result, "medium")
    assert security_false_positive(result, requirement)[0] is False

    monkeypatch.setitem(security._SEVERITY_ORDER, "low", 1)

    assert _gate_fails(result, "medium")
    assert security_false_positive(result, requirement)[0] is True


def test_the_threshold_choices_list_the_ranking_in_rank_order() -> None:
    """``ks factory --help`` lists the choices most severe first. The
    literal here is an independent oracle: a CLI that derives its choices
    from the ranking in any other order (``sorted``, a set) fails."""
    result = CliRunner().invoke(cli, ["factory", "--help"])

    assert result.exit_code == 0, result.output
    assert "--security-fail-threshold [critical|high|medium|low]" in result.output
