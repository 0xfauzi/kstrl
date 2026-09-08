"""Assertions two behaviour suites share about a row's ``measured`` field.

Here rather than in one of the suites because both drive REAL check functions
and both have to ask the same two questions of the row that comes back: does it
say it measured nothing, and does the dampener then put a baseline signature in
``unmeasured`` rather than in ``fixed``? A private copy in each file would let
the two drift, and the second copy is the one nobody would notice weakening.
"""

from __future__ import annotations

from kstrl import dampener
from kstrl.verify import CheckResult, VerificationResult


def assert_unmeasured(row: CheckResult) -> None:
    """The row measured nothing, and the dampener treats it accordingly.

    Both halves, because either alone is satisfiable by a broken mechanism: a
    ``measured=False`` nothing reads is a comment, and a comparison that never
    fills ``fixed`` would pass the second assertion with the field deleted.

    The baseline is built to hold one signature this check produced earlier, so
    the question "did this get fixed?" is live. It must be answered no.
    """
    assert row.measured is False, f"{row.name} claims to have measured: {row.message!r}"

    signature = f"{row.name}:an-earlier-finding"
    current = dampener.baseline_from_result(
        VerificationResult(passed=row.passed, checks=[row], not_measured=[]),
        base_ref="0" * 40,
        project="proj",
        generated_at="2026-09-07T00:00:00Z",
        sense_schema_version=3,
        digest="d" * 16,
    )
    baseline = dampener.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref="1" * 40,
        project="proj",
        passed=False,
        sense_schema_version=3,
        verify_digest="d" * 16,
        measured_checks=(row.name,),
        unmeasured_checks=(),
        unmeasured_reasons={},
        signatures={signature: 3},
    )

    comparison = dampener.compare(baseline, current)

    assert comparison.unmeasured == {signature: 3}
    assert comparison.fixed == {}
    assert comparison.new == {}
    # The flagging half of the same fact: the sensor went dark, so the verdict
    # is a regression rather than "no regression" and exit 0.
    assert row.name in comparison.stopped_measuring
    assert comparison.regressed is True
    # And the row contributes no signature of its own, which is what stops a
    # timeout message becoming a finding that later "gets fixed".
    assert current.signatures == {}


def assert_measured(row: CheckResult) -> None:
    """The control: the same check, having actually measured something."""
    assert row.measured is True, f"{row.name} claims it measured nothing: {row.message!r}"
