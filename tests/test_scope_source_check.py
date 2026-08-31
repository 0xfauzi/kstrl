"""A scope that could not be READ reports under its own name (#294).

R1.5 made Phase 1 fail closed when no trustworthy allowlist could be
established, and reported that through ``check_diff_scope`` as
``failed_check = diff_scope``. But ``diff_scope`` means "the diff
touched files outside the allowlist", so the retry context reads as
"narrow the diff" - and the allowlist here is resolved at plan time from
the pre-run checkout (``scope.ComponentScope``), which is OUTSIDE every
worktree and fixed for the life of the run. The engineer would narrow
its diff, fail identically, and burn the attempt.

#293 made the state unreachable from ``run_factory``
(``_preflight_component_scope`` refuses an unresolved component before
the scheduler launches anything), so this is a clarity fix rather than a
spend fix. It is not unreachable in general: ``run_mechanical_
verification`` is a public entry point, and any caller that bypasses the
plan-time preflight reopens it.

This file pins only what is new here:

1. The engineer-facing retry text names the real cause and no longer
   names the diff.
2. The old route is GONE rather than shadowed: ``check_diff_scope``
   cannot be handed the error at all.
3. Every consumer that keys on the check-name string was decided - the
   journal's category and signature move to the new name, the in-loop
   guard keeps the old one.

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

from kstrl.context import IterationContext
from kstrl.evolution import (
    category_for_check,
    signatures_from_verification,
    split_signature,
)
from kstrl.factory import _worker_scope
from kstrl.scope import ComponentScope
from kstrl.verify import (
    VerificationResult,
    VerifyConfig,
    check_diff_scope,
    check_scope_source,
    run_mechanical_verification,
)

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

    def test_the_failure_line_names_the_scope_source(self, retry_prompt: str) -> None:
        assert "- scope_source: FAIL" in retry_prompt
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


class TestTheConsumersOfTheCheckName:
    """Three things key on the string. Each was decided, not assumed."""

    def test_both_names_categorise_as_verification(self) -> None:
        """The new name because a name absent from
        ``_CATEGORY_BY_CHECK`` falls through to "iteration", filing a
        Phase 1 gate under the engineer loop; the old one because
        journal entries written before the split carry
        ``diff_scope:...`` signatures and are not migrated."""
        assert category_for_check("scope_source") == "verification"
        assert category_for_check("diff_scope") == "verification"

    def test_the_failure_signature_carries_the_new_check_name(self) -> None:
        signatures = signatures_from_verification([check_scope_source(SCOPE_ERROR)])
        assert len(signatures) == 1
        check, code = split_signature(signatures[0])
        assert check == "scope_source"
        # Whole words: ``signature_slug`` cuts at 60 characters, and a
        # signature is a cross-run grouping key an operator reads.
        assert code == "scope-could-not-be-read-at-plan-time-failing-closed"

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
