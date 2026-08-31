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
So the state is reachable in production, and naming it correctly is not
the whole fix - it is also failed rather than retried.

This file pins what is new here:

1. The engineer-facing retry text names the real cause and no longer
   names the diff, and it names a remedy that actually works.
2. The component is FAILED, not retried: no engineer attempt is spent
   on a verdict nothing in the worktree can change.
3. The old route is GONE rather than shadowed: ``check_diff_scope``
   cannot be handed the error at all, by keyword OR positionally.
4. Every consumer that keys on the check-name string was decided - the
   journal's category, signature and proposal move to the new name, the
   in-loop guard keeps the old one.

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
from kstrl.context import IterationContext
from kstrl.evolution import (
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
    category_for_check,
    signatures_from_verification,
    split_signature,
)
from kstrl.factory import ComponentResult, _worker_scope
from kstrl.manifest import Component
from kstrl.pipeline import FailureAction
from kstrl.scope import ComponentScope
from kstrl.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    _scope_checks,
    check_diff_scope,
    check_scope_unreadable,
    run_mechanical_verification,
)
from tests.test_progress_scope import _component as _progress_scope_component_factory
from tests.test_progress_scope import _pipeline as _progress_scope_pipeline

#: A realistic value: this is the shape ``ComponentScope.resolve``
#: records when the pre-run PRD will not load.
SCOPE_ERROR = "pre-run PRD not found: scripts/kstrl/comp-a/prd.json"


def _verify(root: Path, *, error: str | None) -> VerificationResult:
    """Phase 1 with only the cheap gates on.

    No git repository is set up, and none is needed: with an
    ``allowed_paths_error`` ``_scope_checks`` returns before any git
    call, ``prd_path`` is None, ``check_bad_patterns`` is off, and
    dead-code / mutation / self-critique are off by default. Measured:
    building a repo with a real diff first changes no assertion in this
    file and roughly doubles its runtime.
    """
    return run_mechanical_verification(
        root,
        None,
        "main",
        None,
        VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=30.0,
        ),
        allowed_paths_error=error,
    )


@pytest.fixture(scope="module")
def retry_prompt(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The text attempt 2's engineer actually reads, built once.

    Deterministic, and the three tests below assert three different
    substrings of it, so a per-test rebuild is three
    ``run_mechanical_verification`` runs (measured at 9.9 ms each) for
    one string.
    """
    result = _verify(tmp_path_factory.mktemp("scope-source"), error=SCOPE_ERROR)
    assert not result.passed
    ctx = IterationContext()
    ctx.add_verification_failure(result.as_context(), attempt=1)
    return ctx.format_for_prompt()


class TestTheEngineerIsToldWhatActuallyFailed:
    """The retry prompt is the only thing the next attempt reads."""

    def test_the_failure_line_names_the_unreadable_scope(self, retry_prompt: str) -> None:
        assert "- scope_unreadable: FAIL" in retry_prompt
        assert SCOPE_ERROR in retry_prompt

    def test_the_prompt_never_names_the_diff_check(self, retry_prompt: str) -> None:
        """The token the engineer acts on. Under the old behaviour this
        line read ``- diff_scope: FAIL``, which is an instruction to
        change a diff that cannot change the verdict."""
        assert "diff_scope" not in retry_prompt

    def test_the_prompt_says_the_worktree_cannot_fix_it(self, retry_prompt: str) -> None:
        """Naming the cause is not enough on its own: the engineer is
        still told "Fix the current failures" by the context footer, so
        the detail has to say that fixing it from here is impossible."""
        assert "NOT something an engineer can fix from inside the worktree" in retry_prompt
        assert "neither narrowing nor widening the diff changes this verdict" in retry_prompt

    def test_the_remedy_it_names_is_one_that_works(self, retry_prompt: str) -> None:
        """The defect #294 is about, one layer out. An earlier draft
        told the reader to "set --allowed-paths for the run", and
        ``ComponentScope.resolve`` returns unresolved BEFORE it consults
        that flag, so an operator following the text restarts the run
        and hits the identical refusal. Proven against resolve itself in
        ``test_the_run_flag_provably_cannot_clear_it`` below."""
        assert "Restore that file in the main checkout" in retry_prompt
        assert "A run-wide --allowed-paths cannot stand in for it" in retry_prompt

    def test_the_run_flag_provably_cannot_clear_it(self, tmp_path: Path) -> None:
        """The evidence behind the sentence above, so the remediation
        text cannot drift back to offering the flag."""
        comp = Component("comp-a", "A", "", [], "scripts/kstrl/comp-a/prd.json", "b")
        scope = ComponentScope.resolve(
            comp,
            tmp_path,
            KstrlConfig(allowed_paths=["src/"]),
        )
        assert scope.source == "unresolved"
        assert scope.allowed_paths is None
        assert scope.is_trustworthy is False


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

    def test_an_empty_error_means_unset_not_a_causeless_refusal(self) -> None:
        """``is not None`` made ``allowed_paths_error=""`` hard-fail
        Phase 1 with a detail rendering as the bare "Error: ". Empty
        means unset here as it does everywhere else in the module."""
        checks = _scope_checks(
            Path("."),
            "main",
            allowed_paths=None,
            allowed_paths_error="",
            harness_paths=None,
            compare=True,
        )
        assert [c.name for c in checks] == ["diff_scope"]
        assert checks[0].passed is True


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


def _verify_action(tmp_path: Path, failing: list[CheckResult]) -> FailureAction:
    """The action the REAL ``_phase_verify`` routes these checks to.

    Drives ``ComponentPipeline._phase_verify`` with the verification
    hook stubbed, rather than re-deriving the condition in the test: a
    test that reimplements the branch it is checking passes when the
    branch is deleted.
    """
    comp = _progress_scope_component_factory()
    pipeline = _progress_scope_pipeline(
        tmp_path,
        comp,
        tmp_path,
        run_mechanical_verification=lambda *a, **k: VerificationResult(
            passed=False,
            checks=failing,
        ),
    )
    result = pipeline._phase_verify(
        comp,
        ComponentResult(comp.id, success=True, iterations=1, duration_seconds=1.0),
        tmp_path,
    )
    assert result.failure is not None
    return result.failure.action


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
        action = _verify_action(tmp_path, [check_scope_unreadable(SCOPE_ERROR)])
        assert action is FailureAction.FAIL

    def test_an_ordinary_check_still_retries(self, tmp_path: Path) -> None:
        """The wall is what this one check reports, not the attempt."""
        action = _verify_action(tmp_path, [CheckResult("linter", False, "E501")])
        assert action is FailureAction.RETRY_OR_FAIL

    def test_a_mixed_failure_still_fails(self, tmp_path: Path) -> None:
        """A readable scope is a precondition for judging the rest, so
        an unreadable one is decisive whatever else also failed."""
        action = _verify_action(
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
        """Every other check in verify.py brackets its work with
        ``time.monotonic``, and ``ks sense`` publishes the number. A
        hardcoded 0.0 would be an unmeasured value printed as a
        measurement."""
        assert "time.monotonic()" in inspect.getsource(check_scope_unreadable)
        assert check_scope_unreadable(SCOPE_ERROR).duration_seconds >= 0.0
