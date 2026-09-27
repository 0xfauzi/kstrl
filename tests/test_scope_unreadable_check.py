"""A scope that could not be READ reports under its own name (#294).

R1.5 made Phase 1 fail closed when no trustworthy allowlist could be
established, and reported that through ``check_diff_scope`` as
``failed_check = diff_scope``. But ``diff_scope`` means "the diff
touched files outside the allowlist", so the retry context reads as
"narrow the diff" - and the allowlist here is resolved at plan time from
the pre-run checkout, which is OUTSIDE every worktree and fixed for the
life of the run. #293 put ``_preflight_component_scope`` in front of the
ordinary path, but that preflight only inspects PENDING components, and
``run_mechanical_verification`` is a public entry point, so the state is
reachable in production and naming it correctly was never the whole fix.

Every test here runs the real ``run_mechanical_verification`` over a
scratch directory with an ``allowed_paths_error`` and reads what the
operator reads (the PR body, the HALTED_RUN inbox item, ``ks check``):
the refusal names the real cause, never names the diff check, says the
worktree cannot fix it, does not assert a cause it cannot know, and names
a remedy that works. Not a retry prompt: the check routes to
``FailureAction.FAIL`` and no attempt 2 exists for this failure.

The cheapest refusal, ``factory``'s launch gate, lives in
``tests/test_scope_launch_gate.py``; "the refusal does not depend on
``[verify] check_diff_scope``" is ``test_scope_snapshot``, and "the error
wins over a half-loaded path list" is ``test_scope_hardening``.
"""

from __future__ import annotations

import pytest

from tests.helpers.verify_phase import verify_with_cheap_gates

#: A realistic value: this is the shape ``ComponentScope.resolve``
#: records when the pre-run PRD will not load.
SCOPE_ERROR = "pre-run PRD not found: scripts/kstrl/comp-a/prd.json"


@pytest.fixture(scope="module")
def refusal_text(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Everything the refusal says, as one string, built once.

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
