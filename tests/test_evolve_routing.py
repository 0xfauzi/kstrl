"""#217: mechanical failures are routed to the inbox, not turned into
proposals.

Three concerns, one file. ``TestSurvivingArmsRenderUnchanged`` pins that
the six typed proposal arms render exactly as they did before this
change (a unit-level characterisation pin plus its end-to-end half).
``TestRoutingIsClosedOverTheTable`` is the routing guard, derived from
``_CATEGORY_BY_CHECK`` and ``PROPOSAL_CHECKS`` rather than hand-written.
``TestEvolveRoutesMechanicalFailuresAway`` and
``TestEvolvePrintsReadinessNumbers`` drive ``ks evolve`` end to end.

Two rules for every test here: never import a helper from another
``tests/test_*.py`` module (build journals with ``_write_journal``
below), and every ``CliRunner`` assertion carries ``result.exception``
in its message, because ``CliRunner.invoke`` swallows an exception into
that attribute and truncates stdout.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

import kstrl.evolution
from kstrl.cli import cli
from kstrl.evolution import (
    _CATEGORY_BY_CHECK,
    INFRASTRUCTURE_CHECKS,
    PROPOSAL_CHECKS,
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
    category_for_check,
    route_patterns,
)
from kstrl.verify import SCOPE_UNREADABLE_CHECK


def _write_journal(root: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (run_id, component_id, signature). One JSONL line each.

    The shape record_run writes, pinned the same way
    tests/test_evolve_apply.py:194-211 already does.
    """
    entries = [
        {
            "schema_version": 2,
            "run_id": run_id,
            "component_id": component_id,
            "event_type": "component_result",
            "status": "failed",
            "error": "failed",
            "failure_signatures": [signature],
            "findings_summary": {"total": 0, "by_category": {}},
        }
        for run_id, component_id, signature in rows
    ]
    path = root / ".kstrl" / "evolution.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _invoke(root: Path) -> Result:
    """`ks evolve` on one project root. Verbatim from
    tests/test_evolve_apply.py:61-67, which is the recipe that works."""
    return CliRunner().invoke(cli, ["evolve", "--root", str(root), "--ui", "plain", "--no-color"])


# The six pinned literals, derived by running the UNCHANGED code at
# a98ff21 (the exact script is in plan.md, section "## Tests to write
# first", T2). Each tuple is
# (check_name, title, description, target, suggested_change,
#  source_patterns, proposal_type) for a FailurePattern built with
# description="d", frequency=3, total_components=4,
# affected_components=["comp-a", "comp-b"], error_signature="SIG",
# category="verification".
_PINNED_ARMS: list[tuple[str, str, str, str, str, list[str], str]] = [
    (
        "linter",
        "Add linter convention for SIG to CLAUDE.md",
        "Linter rule SIG triggered in 3 components. Adding an explicit "
        "convention to CLAUDE.md will help the agent avoid this pattern.",
        "claude_md",
        "Add to CLAUDE.md:\n> Avoid triggering linter rule SIG. Check the "
        "rule in your linter's documentation for the correct pattern.",
        ["d"],
        "computational",
    ),
    (
        "typecheck",
        "Adjust type-checking config for 'SIG'",
        "Type error pattern 'SIG' recurred in 3 components. Consider "
        "adjusting the type checker's config or adding a CLAUDE.md note "
        "about the expected typing style.",
        "typecheck_config",
        "Review the type checker's configuration. If this is a known "
        "false positive, add it to the ignore list. Otherwise add to "
        "CLAUDE.md:\n> Ensure all functions have return type annotations "
        "to avoid 'SIG'.",
        ["d"],
        "computational",
    ),
    (
        "test_suite",
        "Add codebase scan focus for test pattern 'SIG'",
        "Test failure 'SIG' hit 3 components: comp-a, comp-b. Focusing "
        "codebase scan context on this pattern may help the agent fix the "
        "root cause earlier in the iteration loop.",
        "codebase_scan_config",
        "Add to codebase scan config or CLAUDE.md:\n> Known recurring test "
        "issue: 'SIG'. When tests fail with this pattern, check the "
        "affected modules before re-running.",
        ["d"],
        "computational",
    ),
    (
        "review",
        "Add review guidance for 'SIG'",
        "Review finding category 'SIG' (reviewer concern taxonomy) "
        "appeared in 3 components. Adding explicit guidance to CLAUDE.md "
        "can help the agent avoid this in the first pass.",
        "claude_md",
        "Add to CLAUDE.md:\n> Reviewer repeatedly flags 'SIG'. Address this pattern proactively.",
        ["d"],
        "computational",
    ),
    (
        "security",
        "Add security guidance for 'SIG'",
        "Security finding category 'SIG' (OWASP-mapped taxonomy) appeared "
        "in 3 components. Adding an explicit convention to CLAUDE.md can "
        "prevent the vulnerability class from being introduced at all.",
        "claude_md",
        "Add to CLAUDE.md:\n> Security reviewer repeatedly flags 'SIG'. "
        "Follow the secure pattern for this category from the start.",
        ["d"],
        "computational",
    ),
    (
        SCOPE_UNREADABLE_CHECK,
        "Repair the component scopes that would not resolve (3 runs)",
        "No trustworthy scope could be established for a component in 3 "
        "runs, across comp-a, comp-b. The component is refused before its "
        "engineer runs, because the snapshot is fixed for the life of the "
        "run. No agent can clear it: the scope is read from the main "
        "checkout, outside every worktree.",
        "repository",
        "Two faults produce this, and the run's failure record says "
        "which. A pre-run PRD that would not read: check that every "
        "component's `prdPath` names a readable, parseable file in the "
        "main checkout, and that decompose is writing it. No plan-time "
        "scope resolved for the component at all: the PRD is fine and the "
        "manifest disagrees with the resolved run scope, which is a "
        "harness fault. A run-wide `--allowed-paths` fixes neither: scope "
        "resolution refuses before it reaches the flag.",
        ["d"],
        "computational",
    ),
]


def _resolve_check_name_comparator(comparator: ast.expr) -> str:
    """Resolve one `pattern.check_name == <comparator>` right-hand side.

    Fails, rather than skipping, on any shape it cannot resolve: a
    comparator that is neither a Constant nor a Name, a Name that
    ``getattr`` raises on, or a resolved value that is not a str. This
    is a CLEARING guard, so skipping one shape is how it would go blind
    (CLAUDE.md, the eleven logged instances of that direction).
    """
    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
        return comparator.value
    if isinstance(comparator, ast.Name):
        try:
            value = getattr(kstrl.evolution, comparator.id)
        except AttributeError as exc:
            raise AssertionError(
                f"comparator {ast.unparse(comparator)!r} does not resolve on kstrl.evolution: {exc}"
            ) from exc
        if not isinstance(value, str):
            raise AssertionError(
                f"comparator {ast.unparse(comparator)!r} resolved to non-str {value!r}"
            )
        return value
    raise AssertionError(
        f"comparator {ast.unparse(comparator)!r} is neither a Constant nor a Name; "
        f"T3e must fail on it rather than skip it"
    )


class TestSurvivingArmsRenderUnchanged:
    """The six typed arms must render exactly as they did before #217.

    This is the "behaviour must not change" half of the change. T2 pins
    the rendering function directly (a unit-level characterisation
    pin); T2b drives the CLI end to end and checks all six still reach
    disk. Neither substitutes for the other.
    """

    @pytest.mark.parametrize(
        "check_name,title,description,target,suggested_change,source_patterns,proposal_type",
        _PINNED_ARMS,
        ids=[arm[0] for arm in _PINNED_ARMS],
    )
    def test_each_arm_renders_exactly_as_it_did_before(
        self,
        check_name: str,
        title: str,
        description: str,
        target: str,
        suggested_change: str,
        source_patterns: list[str],
        proposal_type: str,
    ) -> None:
        # category="verification" is deliberately wrong for review and
        # security; the arms key on check_name, not on category, and
        # that is what TestRoutingIsClosedOverTheTable::
        # test_the_router_recomputes_the_category_and_ignores_a_stamped_one
        # is about. Rendering must not change because of it.
        pattern = FailurePattern(
            description="d",
            frequency=3,
            total_components=4,
            affected_components=["comp-a", "comp-b"],
            check_name=check_name,
            error_signature="SIG",
            category="verification",
        )
        journal = EvolutionJournal(EvolutionConfig())
        proposal = journal.propose_improvements([pattern])[0]
        assert (
            proposal.title,
            proposal.description,
            proposal.target,
            proposal.suggested_change,
            proposal.source_patterns,
            proposal.proposal_type,
        ) == (title, description, target, suggested_change, source_patterns, proposal_type)

    def test_every_arm_still_reaches_disk_through_the_cli(self, tmp_path: Path) -> None:
        rows = [
            ("r1", "pr-arm-linter", "linter:S608"),
            ("r1", "pr-arm-typecheck", "typecheck:TS2322"),
            ("r1", "pr-arm-tests", "test_suite:assertion-error"),
            ("r1", "pr-arm-review", "review:prd_criterion"),
            ("r1", "pr-arm-security", "security:injection"),
            ("r1", "pr-arm-scope", "scope_unreadable:no-trustworthy-scope"),
            ("r2", "pr-arm-linter", "linter:S608"),
            ("r2", "pr-arm-typecheck", "typecheck:TS2322"),
            ("r2", "pr-arm-tests", "test_suite:assertion-error"),
            ("r2", "pr-arm-review", "review:prd_criterion"),
            ("r2", "pr-arm-security", "security:injection"),
            ("r2", "pr-arm-scope", "scope_unreadable:no-trustworthy-scope"),
        ]
        _write_journal(tmp_path, rows)
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)

        proposal_files = sorted((tmp_path / ".kstrl" / "proposals").glob("prop-*.md"))
        assert [p.name for p in proposal_files] == [
            "prop-001.md",
            "prop-002.md",
            "prop-003.md",
            "prop-004.md",
            "prop-005.md",
            "prop-006.md",
        ]

        combined = "\n".join(p.read_text(encoding="utf-8") for p in proposal_files)
        # These titles use the real per-check codes from the journal rows
        # above (S608, TS2322, ...), not the "SIG" placeholder T2 uses for
        # its direct-call unit pin: this test drives the real pipeline,
        # where the code comes from split_signature on the recorded
        # signature, not from a hand-built FailurePattern.
        expected_titles = [
            "Add linter convention for S608 to CLAUDE.md",
            "Adjust type-checking config for 'TS2322'",
            "Add codebase scan focus for test pattern 'assertion-error'",
            "Add review guidance for 'prd_criterion'",
            "Add security guidance for 'injection'",
            "Repair the component scopes that would not resolve (2 runs)",
        ]
        for title in expected_titles:
            assert combined.count(title) == 1, title
        assert "Take extra care with this pattern" not in combined


class TestRoutingIsClosedOverTheTable:
    """The routing guard, derived from the table rather than hand-written."""

    def test_every_enrolled_name_lands_in_exactly_one_bucket(self) -> None:
        patterns = [
            FailurePattern(
                description="d",
                frequency=2,
                total_components=2,
                affected_components=["comp-a"],
                check_name=name,
                error_signature="SIG",
                category="verification",
            )
            for name in _CATEGORY_BY_CHECK
        ]
        routing = route_patterns(patterns)
        assert len(routing.mechanical) + len(routing.lessons) + len(routing.unrouted) == len(
            _CATEGORY_BY_CHECK
        )
        mechanical_names = {p.check_name for p in routing.mechanical}
        lessons_names = {p.check_name for p in routing.lessons}
        unrouted_names = {p.check_name for p in routing.unrouted}
        assert mechanical_names.isdisjoint(lessons_names)
        assert mechanical_names.isdisjoint(unrouted_names)
        assert lessons_names.isdisjoint(unrouted_names)
        assert mechanical_names == set(INFRASTRUCTURE_CHECKS)
        assert lessons_names == set(PROPOSAL_CHECKS)
        assert unrouted_names == set(_CATEGORY_BY_CHECK) - INFRASTRUCTURE_CHECKS - PROPOSAL_CHECKS
        assert (len(mechanical_names), len(lessons_names), len(unrouted_names)) == (6, 6, 17)

    def test_proposal_checks_and_infrastructure_checks_are_disjoint(self) -> None:
        assert PROPOSAL_CHECKS & INFRASTRUCTURE_CHECKS == frozenset()

    def test_the_router_recomputes_the_category_and_ignores_a_stamped_one(self) -> None:
        mismatched_infra = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="pr",
            error_signature="SIG",
            category="review",
        )
        routing = route_patterns([mismatched_infra])
        assert routing.mechanical == (mismatched_infra,)
        assert routing.lessons == ()
        assert routing.unrouted == ()

        mismatched_lesson = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="review",
            error_signature="SIG",
            category="infrastructure",
        )
        routing = route_patterns([mismatched_lesson])
        assert routing.lessons == (mismatched_lesson,)
        assert routing.mechanical == ()
        assert routing.unrouted == ()

    def test_an_unenrolled_name_is_unrouted_and_never_a_lesson(self) -> None:
        assert category_for_check("zzz-never-enrolled") == "iteration"
        pattern = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="zzz-never-enrolled",
            error_signature="SIG",
            category="iteration",
        )
        routing = route_patterns([pattern])
        assert routing.unrouted == (pattern,)
        assert routing.lessons == ()
        assert routing.mechanical == ()

    def test_the_arms_in_the_source_are_exactly_PROPOSAL_CHECKS(self) -> None:
        from tests.helpers.astwalk import corpus

        tree = corpus.parsed(Path(kstrl.evolution.__file__))

        class_defs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "EvolutionJournal"
        ]
        assert len(class_defs) == 1, (
            f"expected exactly one EvolutionJournal ClassDef, found {len(class_defs)}"
        )
        function_defs = [
            item
            for item in class_defs[0].body
            if isinstance(item, ast.FunctionDef) and item.name == "propose_improvements"
        ]
        assert len(function_defs) == 1, (
            f"expected exactly one propose_improvements def in EvolutionJournal, "
            f"found {len(function_defs)}"
        )
        function_def = function_defs[0]

        compares = [
            node
            for node in ast.walk(function_def)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "check_name"
        ]
        assert compares, "found no `pattern.check_name == ...` comparisons to resolve"

        resolved = {_resolve_check_name_comparator(compare.comparators[0]) for compare in compares}
        assert resolved == set(PROPOSAL_CHECKS)

    def test_propose_improvements_refuses_a_pattern_the_router_would_not_send(self) -> None:
        pattern = FailurePattern(
            description="d",
            frequency=2,
            total_components=2,
            affected_components=["comp-a"],
            check_name="pr",
            error_signature="SIG",
            category="infrastructure",
        )
        journal = EvolutionJournal(EvolutionConfig())
        with pytest.raises(ValueError, match="pr") as exc_info:
            journal.propose_improvements([pattern])
        assert "route_patterns" in str(exc_info.value)


class TestEvolveRoutesMechanicalFailuresAway:
    def test_infrastructure_signatures_never_become_proposals(self, tmp_path: Path) -> None:
        rows = [
            ("r1", "comp-push", "pr:push-of-x-failed-to-https"),
            ("r1", "comp-review", "review:prd_criterion"),
            ("r1", "comp-patterns", "bad_patterns:hardcoded-secret"),
            ("r2", "comp-push", "pr:push-of-x-failed-to-https"),
            ("r2", "comp-review", "review:prd_criterion"),
            ("r2", "comp-patterns", "bad_patterns:hardcoded-secret"),
        ]
        _write_journal(tmp_path, rows)
        result = _invoke(tmp_path)

        assert result.exit_code == 0, (result.output, result.exception)
        proposal_files = sorted((tmp_path / ".kstrl" / "proposals").glob("prop-*.md"))
        assert [p.name for p in proposal_files] == ["prop-001.md"]

        combined = "\n".join(p.read_text(encoding="utf-8") for p in proposal_files)
        assert "Investigate recurring failure" not in combined
        assert "Take extra care with this pattern" not in combined
        assert "prd_criterion" in combined

        assert "push-of-x-failed-to-https" in result.output
        assert "ks inbox" in result.output
        assert "hardcoded-secret" in result.output
        assert "Not routed" in result.output
        assert "bad_patterns" in result.output


class TestEvolvePrintsReadinessNumbers:
    def test_the_readiness_numbers_print_when_nothing_recurs(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, [("r1", "comp-a", "linter:S608")])
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "recurring signatures" in result.output
        assert "fact utilization" in result.output
        assert "concern hit rate" in result.output

    def test_the_readiness_numbers_print_with_auto_propose_disabled(self, tmp_path: Path) -> None:
        rows = [
            ("r1", "comp-push", "pr:push-of-x-failed-to-https"),
            ("r1", "comp-review", "review:prd_criterion"),
            ("r1", "comp-patterns", "bad_patterns:hardcoded-secret"),
            ("r2", "comp-push", "pr:push-of-x-failed-to-https"),
            ("r2", "comp-review", "review:prd_criterion"),
            ("r2", "comp-patterns", "bad_patterns:hardcoded-secret"),
        ]
        _write_journal(tmp_path, rows)
        (tmp_path / "kstrl.toml").write_text(
            "[evolution]\nauto_propose = false\n", encoding="utf-8"
        )
        result = _invoke(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert "auto_propose is disabled" in result.output
        assert not (tmp_path / ".kstrl" / "proposals").exists()
        assert "recurring signatures" in result.output
        assert "push-of-x-failed-to-https" in result.output
