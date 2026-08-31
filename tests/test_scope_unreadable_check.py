"""A scope that could not be READ reports under its own name (#294).

R1.5 made Phase 1 fail closed when no trustworthy allowlist could be
established, and reported that through ``check_diff_scope`` as
``failed_check = diff_scope``. But ``diff_scope`` means "the diff
touched files outside the allowlist", so the retry context reads as
"narrow the diff" - and the allowlist here is resolved at plan time from
the pre-run checkout (``scope.ComponentScope``), which is OUTSIDE every
worktree and fixed for the life of the run. The engineer would narrow
its diff, fail identically, and burn the attempt.

#293 put ``_preflight_component_scope`` in front of the ordinary path,
so a run whose components are all PENDING at the start refuses before
the scheduler launches anything. That preflight is not a proof of
unreachability: it only inspects PENDING components, and the contract
breaker resets a COMPLETED one to PENDING mid-run, long after it
returned. ``run_mechanical_verification`` is also a public entry point.
So the state is reachable in production, and naming it correctly was
never the whole fix.

Note what this file does NOT assert, because #294 removed it: a retry
prompt. The check routes to ``FailureAction.FAIL``, and
``_route_failure`` discards ``context_json`` on that path, so no attempt
2 exists and ``IterationContext`` is never rendered for this failure.
The surviving audience is the operator. Every text assertion here reads
what the operator reads.

This file pins what is new here:

1. The refusal names the real cause, no longer names the diff, does not
   assert a cause it cannot know, and names a remedy that works.
2. The component is FAILED, not retried, and the recorded error carries
   the file to restore rather than only the fact of the failure.
3. The old route is GONE rather than shadowed: ``check_diff_scope``
   cannot be handed the error at all, by keyword OR positionally, and an
   ambiguous empty sentinel refuses rather than passing.
4. Every consumer that keys on the check-name string was decided - the
   journal's category, signature and proposal move to the new name, the
   in-loop guard keeps the old one.

The cheapest refusal is not here: ``factory``'s launch gate refuses an
untrustworthy scope BEFORE the engineer runs, which is the only place
the spend is actually saved, and it lives in
``tests/test_scope_launch_gate.py``.

Two claims deliberately live elsewhere rather than being restated here,
because both files already asserted them before #294 and the split only
changed the name they assert:

- "the refusal does not depend on ``[verify] check_diff_scope``" is
  ``test_scope_snapshot.TestAnUnresolvedScopeCannotBeSwitchedOff``,
  parametrized over both toggle states.
- "the error wins over a half-loaded path list", and the unconfigured
  no-constraint PASS that makes the two checks alternatives, are
  ``test_scope_hardening.TestDiffScopeFailsClosed``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from kstrl.config import KstrlConfig
from kstrl.evolution import (
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
    category_for_check,
    signatures_from_verification,
    split_signature,
)
from kstrl.factory import _worker_scope
from kstrl.manifest import Component
from kstrl.pipeline import FailureAction
from kstrl.scope import ComponentScope
from kstrl.verify import (
    NO_CAUSE_RECORDED,
    SCOPE_UNREADABLE_CHECK,
    CheckResult,
    _scope_checks,
    check_diff_scope,
    check_scope_unreadable,
)
from tests.helpers.verify_phase import phase_verify_action, verify_with_cheap_gates

#: A realistic value: this is the shape ``ComponentScope.resolve``
#: records when the pre-run PRD will not load.
SCOPE_ERROR = "pre-run PRD not found: scripts/kstrl/comp-a/prd.json"


@pytest.fixture(scope="module")
def refusal_text(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Everything the refusal says, as one string, built once.

    NOT a retry prompt. #294 routes this check to
    ``FailureAction.FAIL``, and ``_route_failure`` discards
    ``PhaseFailure.context_json`` on that path, so no attempt 2 exists
    and ``IterationContext`` is never rendered for this failure. The
    surviving readers are the operator, through the PR body, the
    HALTED_RUN inbox item and ``ks sense``, and whoever reads the
    journal later. An earlier version of this file asserted the same
    substrings against a retry prompt that production can no longer
    produce.

    Deterministic, and the tests below assert different substrings of
    it, so a per-test rebuild is one ``run_mechanical_verification`` run
    each (measured at 9.9 ms) for one string.
    """
    result = verify_with_cheap_gates(
        tmp_path_factory.mktemp("scope-unreadable"),
        allowed_paths_error=SCOPE_ERROR,
    )
    assert not result.passed
    return result.as_context()


class TestTheRefusalNamesWhatActuallyFailed:
    """What a reader is told, and whether they can act on it."""

    def test_the_failure_line_names_the_unreadable_scope(self, refusal_text: str) -> None:
        assert "- scope_unreadable: FAIL" in refusal_text
        assert SCOPE_ERROR in refusal_text

    def test_it_never_names_the_diff_check(self, refusal_text: str) -> None:
        """The token a reader acts on. Under the old behaviour this line
        read ``- diff_scope: FAIL``, which reads as an instruction to
        change a diff that cannot change the verdict."""
        assert "diff_scope" not in refusal_text

    def test_it_says_the_worktree_cannot_fix_it(self, refusal_text: str) -> None:
        """The check still runs inside Phase 1, whose other failures are
        all things an engineer fixes, so this one has to say plainly
        that it is not."""
        assert "NOT something an engineer can fix from inside the worktree" in refusal_text
        assert "neither narrowing nor widening the diff changes this verdict" in refusal_text

    def test_it_does_not_assert_a_cause_it_cannot_know(self, refusal_text: str) -> None:
        """Two producers with different remedies: an unreadable PRD, and
        ``RunScope.for_component``'s stand-in for a component that got
        no plan-time scope at all. Asserting the first sends an operator
        on the second to inspect a file that reads perfectly."""
        assert "The Error line above names which of two faults this is" in refusal_text
        assert "the manifest and the run's resolved scope disagree" in refusal_text

    def test_the_remedy_it_names_is_one_that_works(self, refusal_text: str) -> None:
        """#294's own defect, one layer out. An earlier draft told the
        reader to "set --allowed-paths for the run", and
        ``ComponentScope.resolve`` returns unresolved BEFORE it consults
        that flag, so an operator following the text restarts the run
        and hits the identical refusal."""
        assert "A run-wide --allowed-paths fixes neither" in refusal_text
        assert "scope resolution refuses before it reaches the flag" in refusal_text

    def test_the_run_flag_provably_cannot_clear_it(self, tmp_path: Path) -> None:
        """The evidence behind the sentence above, so the remediation
        text cannot drift back to offering the flag."""
        comp = Component("comp-a", "A", "", [], "scripts/kstrl/comp-a/prd.json", "b")
        scope = ComponentScope.resolve(comp, tmp_path, KstrlConfig(allowed_paths=["src/"]))
        assert scope.source == "unresolved"
        assert scope.allowed_paths is None
        assert scope.is_trustworthy is False

    def test_the_recorded_error_names_the_file_to_restore(self, tmp_path: Path) -> None:
        """``pipeline.fail`` writes the routed error to ``comp.error``,
        the ComponentFailed event, the notification hook and the
        HALTED_RUN inbox detail. A fixed string left every one of those
        saying only THAT the scope was unreadable, while the path sat in
        the check details, where none of them look."""
        action, error = phase_verify_action(tmp_path, [check_scope_unreadable(SCOPE_ERROR)])
        assert action is FailureAction.FAIL
        assert SCOPE_ERROR in error


class TestTheOldRouteIsGoneNotShadowed:
    """A branch that is merely bypassed comes back with the next caller."""

    def test_check_diff_scope_no_longer_accepts_the_error(self) -> None:
        params = inspect.signature(check_diff_scope).parameters
        assert "allowed_paths_error" not in params
        with pytest.raises(TypeError):
            check_diff_scope(  # type: ignore[call-arg]
                Path("."),
                "main",
                None,
                allowed_paths_error=SCOPE_ERROR,
            )

    def test_the_freed_positional_slot_cannot_be_filled_by_accident(self) -> None:
        """Deleting a parameter promotes whatever followed it into the
        slot. ``harness_paths`` inherited the 4th position, so an
        unported caller passing the error string positionally got
        ``passed=True`` and "No scope constraints" where it meant a hard
        refusal - a silent fail-open in the one check whose whole job is
        to fail closed. Keyword-only closes it."""
        assert (
            inspect.signature(check_diff_scope).parameters["harness_paths"].kind
            is inspect.Parameter.KEYWORD_ONLY
        )
        with pytest.raises(TypeError):
            check_diff_scope(Path("."), "main", None, SCOPE_ERROR)  # type: ignore[misc]

    def test_an_empty_error_refuses_and_still_names_a_cause(self) -> None:
        """Two review rounds hit this from opposite sides and both were
        right about the defect. Truthiness let an empty sentinel PASS a
        ``diff_scope`` that had no allowlist to compare, a fail-open in
        the one check whose job is to fail closed. ``is not None`` alone
        refused while rendering the bare "Error: ", naming no cause.
        Neither problem requires the other: refuse, and substitute a
        placeholder."""
        checks = _scope_checks(
            Path("."),
            "main",
            allowed_paths=None,
            allowed_paths_error="",
            harness_paths=None,
            compare=True,
        )
        assert [c.name for c in checks] == [SCOPE_UNREADABLE_CHECK]
        assert checks[0].passed is False
        assert checks[0].details[0] == f"Error: {NO_CAUSE_RECORDED}"
        assert checks[0].details[0] != "Error: "


class TestTheConsumersOfTheCheckName:
    """Three things key on the string. Each was decided, not assumed."""

    def test_both_names_categorise_as_verification(self) -> None:
        """The new name because a name absent from
        ``_CATEGORY_BY_CHECK`` falls through to "iteration", filing a
        Phase 1 gate under the engineer loop; the old one because
        journal entries written before the split carry
        ``diff_scope:...`` signatures and are not migrated."""
        assert category_for_check("scope_unreadable") == "verification"
        assert category_for_check("diff_scope") == "verification"

    def test_the_failure_signature_carries_the_new_check_name(self) -> None:
        signatures = signatures_from_verification([check_scope_unreadable(SCOPE_ERROR)])
        assert len(signatures) == 1
        check, code = split_signature(signatures[0])
        assert check == "scope_unreadable"
        # Whole words: ``signature_slug`` cuts at 60 characters, and a
        # signature is a cross-run grouping key an operator reads.
        assert code == "scope-could-not-be-read-at-plan-time-failing-closed"

    def test_ks_evolve_proposes_an_operator_fix_not_agent_advice(self) -> None:
        """The other place that dispatches on the check name. Without a
        branch, a recurring scope failure falls into the generic arm and
        ``ks evolve`` writes "Add to CLAUDE.md: take extra care with
        this pattern" - advice to the agent, for a state the agent
        cannot influence."""
        pattern = FailurePattern(
            description="d",
            frequency=3,
            total_components=4,
            affected_components=["comp-a"],
            check_name="scope_unreadable",
            error_signature="scope-could-not-be-read-at-plan-time-failing-closed",
            category="verification",
        )
        proposal = EvolutionJournal(EvolutionConfig()).propose_improvements([pattern])[0]
        assert proposal.target == "repository"
        assert "CLAUDE.md" not in proposal.suggested_change
        assert "prdPath" in proposal.suggested_change

    def test_the_in_loop_guard_still_reports_diff_scope(self) -> None:
        """The third consumer, ``pipeline`` on ``guard_violations``, is
        untouched: it reports a REAL violation list, which is the thing
        ``diff_scope`` has always meant. It also cannot fire on this
        state - ``_worker_scope`` hands an untrustworthy snapshot an
        empty authored list, which leaves the guard inert."""
        untrustworthy = ComponentScope(
            allowed_paths=["src/"],
            source="unresolved",
            error=SCOPE_ERROR,
        )
        assert untrustworthy.is_trustworthy is False
        assert _worker_scope(untrustworthy) == ([], [])


class TestItIsAWallNotAGate:
    """#294 made the state identifiable; that is only useful if the
    control loop then acts on it."""

    def test_phase_1_fails_the_component_instead_of_retrying(self, tmp_path: Path) -> None:
        """Every other Phase 1 check measures the engineer's work, so a
        retry re-measures something that changed. This one measures the
        harness's own input, frozen at plan time, so attempt two runs
        the identical prompt into the identical refusal.
        ``_preflight_component_scope`` prices that burn at 14.49 dollars
        and 41 minutes over three attempts."""
        action, _ = phase_verify_action(tmp_path, [check_scope_unreadable(SCOPE_ERROR)])
        assert action is FailureAction.FAIL

    def test_an_ordinary_check_still_retries(self, tmp_path: Path) -> None:
        """The wall is what this one check reports, not the attempt."""
        action, _ = phase_verify_action(tmp_path, [CheckResult("linter", False, "E501")])
        assert action is FailureAction.RETRY_OR_FAIL

    def test_a_mixed_failure_still_fails(self, tmp_path: Path) -> None:
        """A readable scope is a precondition for judging the rest, so
        an unreadable one is decisive whatever else also failed."""
        action, _ = phase_verify_action(
            tmp_path,
            [CheckResult("linter", False, "E501"), check_scope_unreadable(SCOPE_ERROR)],
        )
        assert action is FailureAction.FAIL


class TestTheAuditTrail:
    """A harness-side fault has to leave a record, not just a message."""

    def test_it_carries_an_infrastructure_finding(self) -> None:
        """``len(findings) == 0`` is used across the codebase to mean
        "ran cleanly". Without a Finding, a run that dies here leaves an
        empty stream and reads as clean in the PR body and journal."""
        check = check_scope_unreadable(SCOPE_ERROR)
        assert len(check.findings) == 1
        finding = check.findings[0]
        assert finding.is_infrastructure_error
        assert finding.phase == "verify"
        assert SCOPE_ERROR in finding.explanation

    def test_the_duration_is_measured_not_asserted(self) -> None:
        """``ks sense`` prints this per check and emits it into its JSON
        document, so a hardcoded 0.0 would be an unmeasured value
        published as a measurement.

        Strictly greater than zero, which is the whole test: ``>= 0.0``
        is true of the literal it is meant to catch, and grepping the
        source for ``time.monotonic()`` is satisfied by a comment.
        Measured over 20000 calls before relying on it: zero of them
        returned 0.0, minimum 417 ns, median 500 ns. The work being
        timed is only the construction of the result, which is honest -
        the check does no I/O and says so."""
        assert check_scope_unreadable(SCOPE_ERROR).duration_seconds > 0.0
