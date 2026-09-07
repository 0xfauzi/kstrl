"""R10.6 (#227): the dampener's arithmetic, and how a comparison renders.

No CLI here. These drive :mod:`kstrl.dampener` and
:mod:`kstrl.dampener_report` directly, so the bucket rules are pinned
independently of how ``ks sense`` wires them up. Three siblings hold the rest:
``tests/test_sense_dampener_baseline.py`` the artifact on disk and every way
reading it fails, ``tests/test_sense_dampener_cli.py`` the command surface, and
``tests/test_sense_dampener_workflow.py`` the dogfood workflow. The helpers
below are shared, and this file is where they live.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl import dampener, dampener_report
from kstrl.parsers import ParsedFailure, ParsedOutput
from kstrl.verify import CheckResult, NotMeasured, VerificationResult

#: The digest both sides of a comparison carry unless a test is about the
#: refusal itself. A literal, because `verify_digest` is what produces a real
#: one and pinning its output here would make this helper a second
#: implementation of it.
DIGEST = "0" * 16

#: The project both sides carry unless a test is about the mismatch note.
PROJECT = "0xfauzi/kstrl"


def _baseline(
    signatures: dict[str, int] | None = None,
    *,
    # Sorted, because that is the order the document round-trips through:
    # `to_document` sorts every collection so the file diffs cleanly.
    measured: tuple[str, ...] = ("linter", "test_suite", "typecheck"),
    unmeasured: tuple[str, ...] = (),
    reasons: dict[str, str] | None = None,
    sense_schema_version: int = 2,
    base_ref: str | None = "0123456789abcdef",
    passed: bool = False,
    project: str = PROJECT,
    digest: str = DIGEST,
) -> dampener.Baseline:
    return dampener.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref=base_ref,
        project=project,
        passed=passed,
        sense_schema_version=sense_schema_version,
        verify_digest=digest,
        measured_checks=measured,
        unmeasured_checks=unmeasured,
        # Defaulted from `unmeasured` so a test that names a hole does not have
        # to spell the reason twice; `from_document` requires the two key sets
        # to match, and this helper keeps that true by construction.
        unmeasured_reasons=reasons or {name: "did not run" for name in unmeasured},
        signatures=dict(signatures or {}),
    )


# --- the four buckets ---------------------------------------------------


def test_compare_no_regression() -> None:
    base = _baseline({"linter:E501": 3})

    comparison = dampener.compare(base, _baseline({"linter:E501": 3}))

    assert comparison.regressed is False
    assert comparison.new == {}
    assert comparison.increased == {}
    assert comparison.fixed == {}
    assert comparison.unmeasured == {}


def test_compare_detects_new_signature() -> None:
    comparison = dampener.compare(
        _baseline({"linter:E501": 3}),
        _baseline({"linter:E501": 3, "typecheck:arg-type": 1}),
    )

    assert comparison.new == {"typecheck:arg-type": 1}
    assert comparison.regressed is True


def test_compare_detects_increased_count() -> None:
    comparison = dampener.compare(_baseline({"linter:E501": 1}), _baseline({"linter:E501": 2}))

    assert comparison.increased == {"linter:E501": (1, 2)}
    assert comparison.new == {}
    assert comparison.regressed is True


def test_compare_reports_fixed_without_regression() -> None:
    """The positive case for ``fixed``, and the control for the M2 mutation.

    ``linter`` measured something in the current run, so its signature's
    absence is a fix that was PROVED. A ``compare`` that never fills ``fixed``
    at all would satisfy the "a gap is not a fix" test below while failing
    this one.
    """
    comparison = dampener.compare(_baseline({"linter:E501": 4}), _baseline({}))

    assert comparison.fixed == {"linter:E501": 4}
    assert comparison.unmeasured == {}
    assert comparison.regressed is False


def test_a_signature_whose_check_did_not_run_is_not_fixed() -> None:
    """A check that produced no measurement cannot prove anything.

    The whole reason ``unmeasured`` exists. ``fixed`` CLEARS, so it has to be
    narrow: an over-matching clear turns "the sensor stopped running" into
    "the problem went away", which is the mechanism deleting itself quietly.

    And the sensor going dark is a REGRESSION, not a silent note. The two
    buckets are the same fact from two directions: ``unmeasured`` says which
    baseline signatures cannot be spoken for, ``stopped_measuring`` says which
    sensor stopped, and the verdict follows the second.
    """
    base = _baseline({"dead_code:unused-import": 2}, measured=("dead_code", "linter"))
    current = _baseline(
        {},
        measured=("linter",),
        unmeasured=("dead_code",),
        reasons={"dead_code": "Dead code scan timed out after 30.0s, skipping"},
    )

    comparison = dampener.compare(base, current)

    assert comparison.unmeasured == {"dead_code:unused-import": 2}
    assert comparison.fixed == {}
    assert comparison.stopped_measuring == {
        "dead_code": "Dead code scan timed out after 30.0s, skipping"
    }
    assert comparison.regressed is True


def test_a_new_signature_from_a_check_the_baseline_never_measured_is_flagged() -> None:
    """The deliberate over-flag, pinned so silencing it is a red test.

    A signature from a check the BASELINE never measured still lands in
    ``new``. That over-flags when a toolchain gains a binary rather than the
    tree getting worse, and over-flagging is the safe direction for a guard
    that flags: the cost is an advisory comment somebody reads.
    """
    base = _baseline({}, measured=("linter",), unmeasured=("dead_code",))
    current = _baseline({"dead_code:unused-import": 1}, measured=("linter", "dead_code"))

    comparison = dampener.compare(base, current)

    assert comparison.new == {"dead_code:unused-import": 1}
    assert comparison.regressed is True


def test_a_decreased_count_is_not_a_regression() -> None:
    comparison = dampener.compare(_baseline({"linter:E501": 9}), _baseline({"linter:E501": 2}))

    assert comparison.increased == {}
    assert comparison.new == {}
    assert comparison.fixed == {}
    assert comparison.regressed is False


def test_buckets_are_sorted_by_signature() -> None:
    """Report order is signature order, so two runs diff cleanly."""
    current = _baseline({"typecheck:arg-type": 1, "linter:E501": 1, "linter:F401": 1})

    comparison = dampener.compare(_baseline({}), current)

    assert list(comparison.new) == ["linter:E501", "linter:F401", "typecheck:arg-type"]


@pytest.mark.parametrize(
    ("regressed", "fail_on_regression", "expected"),
    [(False, False, 0), (False, True, 0), (True, False, 0), (True, True, 1)],
)
def test_exit_code_table(regressed: bool, fail_on_regression: bool, expected: int) -> None:
    """Advisory by default: only the flag plus a regression is a non-zero exit."""
    current = _baseline({"linter:E501": 1} if regressed else {})
    comparison = dampener.compare(_baseline({}), current)
    assert comparison.regressed is regressed

    assert dampener.exit_code_for(comparison, fail_on_regression=fail_on_regression) == expected


# --- the schema note ----------------------------------------------------


def test_a_sense_schema_change_is_a_note_not_a_refusal() -> None:
    comparison = dampener.compare(
        _baseline({}, sense_schema_version=2),
        _baseline({}, sense_schema_version=3),
    )

    assert comparison.sense_schema_changed == (2, 3)
    assert comparison.regressed is False


def test_an_unchanged_sense_schema_records_no_note() -> None:
    assert dampener.compare(_baseline({}), _baseline({})).sense_schema_changed is None


# --- building a baseline from a run -------------------------------------


def _result(
    checks: list[CheckResult],
    gaps: list[NotMeasured] | None = None,
) -> VerificationResult:
    return VerificationResult(
        passed=all(c.passed for c in checks),
        checks=checks,
        not_measured=list(gaps or []),
    )


def _from(result: VerificationResult) -> dampener.Baseline:
    return dampener.baseline_from_result(
        result,
        base_ref="abc1234def",
        project=PROJECT,
        generated_at="2026-09-06T00:00:00Z",
        sense_schema_version=2,
        digest=DIGEST,
    )


def _failing_linter(count: int = 3) -> CheckResult:
    return CheckResult(
        name="linter",
        passed=False,
        message="Linter failed",
        parsed=ParsedOutput(
            tool="ruff",
            failures=[ParsedFailure(code="E501", message="long") for _ in range(count)],
        ),
    )


def test_a_baseline_counts_every_signature_not_the_journal_cap() -> None:
    """``limit=None``: a baseline that dropped a check's sixth signature would
    report it as new on the very next run."""
    parsed = ParsedOutput(
        tool="ruff",
        failures=[
            ParsedFailure(code=code, message=code)
            for code in ("E501", "F401", "S608", "E731", "B008", "C901", "N802")
        ],
    )
    result = _result([CheckResult(name="linter", passed=False, message="x", parsed=parsed)])

    assert len(_from(result).signatures) == 7


def test_a_timed_out_check_contributes_no_signatures_and_is_unmeasured() -> None:
    """The measured reason this rule exists.

    At the default 300s verify timeout this repository's own test suite times
    out. That is a FAILING row, not a gap, and its fallback signature strips
    the digits, so 300s and 1800s produce the same string: a baseline written
    on a loaded machine records ``test_suite:test-suite-timed-out-after-s`` and
    the same tree on a faster machine reports it FIXED.
    """
    timed_out = CheckResult(
        name="test_suite",
        passed=False,
        message="Test suite timed out after 300.0s",
        measured=False,
    )
    baseline = _from(_result([timed_out, _failing_linter()]))

    assert baseline.signatures == {"linter:E501": 3}
    assert baseline.unmeasured_checks == ("test_suite",)
    assert baseline.measured_checks == ("linter",)


def test_a_gap_is_unmeasured_on_the_baseline_side_too() -> None:
    result = _result(
        [_failing_linter()],
        [NotMeasured(check="mutation_testing", reason="tool_missing", detail="mutmut absent")],
    )

    assert _from(result).unmeasured_checks == ("mutation_testing",)


def test_a_check_that_is_both_a_row_and_a_gap_counts_as_unmeasured() -> None:
    """Closed by construction, not by trusting that it cannot happen: the
    clearing side has to be the narrow one whatever the sensor emits."""
    result = _result(
        [_failing_linter()],
        [NotMeasured(check="linter", reason="timed_out", detail="x")],
    )

    baseline = _from(result)
    assert baseline.measured_checks == ()
    assert baseline.unmeasured_checks == ("linter",)


def test_passed_is_the_sensors_own_verdict_not_an_empty_signature_map() -> None:
    """A check can fail with no parsed codes at all; ``passed`` is not
    recomputed from ``signatures``."""
    result = _result([CheckResult(name="diff_scope", passed=False, message="out of scope")])

    baseline = _from(result)
    assert baseline.passed is False
    assert baseline.signatures != {}


def test_total_findings_counts_occurrences() -> None:
    assert _baseline({"linter:E501": 12, "typecheck:arg-type": 3}).total_findings == 15


# --- rendering ----------------------------------------------------------


def _comparison(**kwargs: Any) -> dampener.Comparison:
    defaults: dict[str, Any] = {
        "new": {},
        "increased": {},
        "fixed": {},
        "unmeasured": {},
        "stopped_measuring": {},
        "sense_schema_changed": None,
        "project_changed": None,
    }
    return dampener.Comparison(**{**defaults, **kwargs})


def test_human_report_heading_and_verdict() -> None:
    lines = dampener_report.render_human(_comparison(), _baseline({}), Path("scripts/b.json"))

    assert lines[0] == "sense regression report vs scripts/b.json (0123456)"
    assert lines[-1] == "no regression"


def test_human_report_names_the_counts_when_it_regressed() -> None:
    comparison = _comparison(new={"linter:E501": 2}, increased={"linter:F401": (1, 4)})

    lines = dampener_report.render_human(comparison, _baseline({}), Path("b.json"))

    assert lines[-1] == "regression: 1 new, 1 increased, 0 stopped measuring"
    assert "  linter:E501  2" in lines
    assert "  linter:F401  1 -> 4" in lines


def test_human_report_says_unknown_when_the_baseline_has_no_commit() -> None:
    lines = dampener_report.render_human(
        _comparison(), _baseline({}, base_ref=None), Path("b.json")
    )

    assert lines[0].endswith("(unknown)")
    assert any("outside a repository" in line for line in lines)


def test_markdown_first_line_is_the_marker_exactly() -> None:
    """A workflow finds its own earlier comment by this line, so nothing may
    precede it: not a blank line, not a heading."""
    text = dampener_report.render_markdown(_comparison(), _baseline({}), Path("b.json"))

    marker = dampener_report.MARKDOWN_MARKER
    assert text.splitlines()[0] == marker == "<!-- kstrl-sense-dampener -->"


def test_markdown_carries_a_table_per_non_empty_bucket() -> None:
    comparison = _comparison(
        new={"linter:E501": 2},
        increased={"linter:F401": (1, 4)},
        fixed={"typecheck:arg-type": 1},
        unmeasured={"dead_code:x": 3},
    )

    text = dampener_report.render_markdown(comparison, _baseline({}), Path("b.json"))

    assert "| `linter:E501` | 2 |" in text
    assert "| `linter:F401` | 1 | 4 |" in text
    assert "| `typecheck:arg-type` | 1 |" in text
    assert "| `dead_code:x` | 3 |" in text
    assert "advisory" in text


def test_a_reason_with_a_pipe_or_a_newline_stays_in_its_cell() -> None:
    """The only free-text value in any of these tables.

    Every other one is a signature or an integer. A reason is a check message
    or a `reason: detail` pair, and both carry subprocess and git stderr
    verbatim. Measured in round 2 of review on #357: this exact string ended
    the row mid-cell and shifted every later column of the posted comment.
    """
    reason = "command_failed: git said\nfatal: bad | ref"
    comparison = _comparison(stopped_measuring={"diff_scope": reason})

    text = dampener_report.render_markdown(comparison, _baseline({}), Path("b.json"))

    row = next(line for line in text.splitlines() if line.startswith("| `diff_scope`"))
    assert row == "| `diff_scope` | command_failed: git said fatal: bad \\| ref |"
    assert row.count("|") == 3 + 1  # three cell walls, plus the escaped pipe
    assert "fatal: bad | ref" not in text


def test_the_human_report_needs_no_escaping() -> None:
    """The control for the escape above, and the reason it is not shared.

    `render_human` prints one reason per line with no cell walls, so the same
    string has to arrive there INTACT. An escape applied in both places would
    put a backslash in front of every pipe an operator reads in a terminal.
    """
    reason = "command_failed: git said\nfatal: bad | ref"
    comparison = _comparison(stopped_measuring={"diff_scope": reason})

    lines = dampener_report.render_human(comparison, _baseline({}), Path("b.json"))

    assert any("fatal: bad | ref" in line for line in lines)


def test_the_footer_says_what_the_mode_in_force_does() -> None:
    """`docs/dampener.md` says adding --fail-on-regression is the whole of
    graduating to blocking. Before this, the comment sitting on a pull request
    that had just been failed by this very report said it never fails a job."""
    comparison = _comparison(new={"linter:E501": 1})
    base = _baseline({})

    advisory = dampener_report.render_markdown(comparison, base, Path("b.json"))
    blocking = dampener_report.render_markdown(
        comparison, base, Path("b.json"), fail_on_regression=True
    )

    assert advisory.endswith(dampener_report.ADVISORY_FOOTER)
    assert blocking.endswith(dampener_report.BLOCKING_FOOTER)
    assert dampener_report.ADVISORY_FOOTER not in blocking
    assert dampener_report.BLOCKING_FOOTER not in advisory


def test_markdown_omits_the_table_of_an_empty_bucket() -> None:
    text = dampener_report.render_markdown(_comparison(), _baseline({}), Path("b.json"))

    assert "New signatures" not in text
    assert "no regression" in text


def test_both_renderers_carry_the_schema_note() -> None:
    comparison = _comparison(sense_schema_changed=(2, 3))
    base = _baseline({})

    human = "\n".join(dampener_report.render_human(comparison, base, Path("b.json")))
    markdown = dampener_report.render_markdown(comparison, base, Path("b.json"))

    for text in (human, markdown):
        assert "from 2 to 3" in text
        assert "--write-baseline --force" in text


def test_the_write_summary_line_names_the_unmeasured_sensors() -> None:
    """Always, ``none`` included. A baseline written while the test suite
    timed out has a hole in it, and the operator has to see that at the moment
    they commit the file rather than infer it from the JSON later."""
    quiet = dampener.write_summary_line(Path("b.json"), _baseline({"linter:E501": 12}))
    holed = dampener.write_summary_line(
        Path("b.json"),
        _baseline({"linter:E501": 12}, unmeasured=("dead_code", "test_suite")),
    )

    assert quiet == "baseline written: b.json (1 signatures, 12 total findings); unmeasured: none"
    assert holed.endswith("; unmeasured: dead_code, test_suite")


def test_the_json_block_carries_every_bucket_and_the_verdict() -> None:
    comparison = _comparison(new={"a:b": 1}, increased={"c:d": (1, 2)}, sense_schema_changed=(2, 3))

    document = dampener_report.comparison_document(
        comparison,
        _baseline({"c:d": 1}),
        _baseline({"a:b": 1, "c:d": 2}),
        Path("b.json"),
    )

    assert document["new"] == {"a:b": 1}
    assert document["increased"] == {"c:d": {"baseline": 1, "current": 2}}
    assert document["regressed"] is True
    assert document["sense_schema_changed"] == {"baseline": 2, "current": 3}
    assert document["baseline"]["schema_version"] == 1
    assert document["current"]["signatures"] == {"a:b": 1, "c:d": 2}
    assert json.loads(json.dumps(document))["baseline_path"] == "b.json"


# --- a sensor that stopped measuring ------------------------------------


#: This repository's own committed baseline shape, reduced to what makes the
#: defect reproducible: a GREEN tree, so ``signatures`` is empty and there is
#: no signature anywhere for any of the other four buckets to hold.
def _green_committed_baseline() -> dampener.Baseline:
    return _baseline(
        {},
        measured=("bad_patterns", "diff_scope", "linter", "test_suite", "typecheck"),
        passed=True,
    )


def test_a_branch_whose_test_suite_stops_finishing_is_a_regression() -> None:
    """The defect this bucket exists for, in the shape it was reproduced in.

    Round 1 of review on #357 drove exactly this against the committed
    baseline: ``regressed=False``, ``exit_code_for(fail_on_regression=True)``
    of 0, and ``no regression`` in the markdown, for a branch on which the
    test suite no longer finishes. The four signature buckets cannot cover it,
    because a green baseline carries no signature to bucket.
    """
    timed_out = CheckResult(
        name="test_suite",
        passed=False,
        message="Test suite timed out after 1800.0s",
        measured=False,
    )
    current = _from(_result([timed_out]))

    comparison = dampener.compare(_green_committed_baseline(), current)

    assert comparison.new == {}
    assert comparison.increased == {}
    assert comparison.unmeasured == {}
    assert comparison.stopped_measuring["test_suite"] == "Test suite timed out after 1800.0s"
    assert comparison.regressed is True
    assert dampener.exit_code_for(comparison, fail_on_regression=True) == 1


def test_a_check_switched_off_entirely_is_also_a_sensor_that_stopped() -> None:
    """Set difference over ``measured_checks``, so it covers both ways a sensor
    goes dark: a row that measured nothing, and no row at all."""
    current = _baseline({}, measured=("linter",))

    comparison = dampener.compare(_baseline({}, measured=("linter", "typecheck")), current)

    assert comparison.stopped_measuring == {"typecheck": dampener.NO_REASON_RECORDED}
    assert comparison.regressed is True


def test_a_sensor_the_baseline_never_measured_does_not_flag_when_it_is_still_off() -> None:
    """The other direction, which must stay quiet. A repository whose baseline
    has holes must not report those same holes as new every run."""
    base = _baseline({}, measured=("linter",), unmeasured=("dead_code",))
    current = _baseline({}, measured=("linter",), unmeasured=("dead_code",))

    comparison = dampener.compare(base, current)

    assert comparison.stopped_measuring == {}
    assert comparison.regressed is False


def test_both_renderers_name_the_check_and_the_reason() -> None:
    comparison = _comparison(stopped_measuring={"test_suite": "Test suite timed out after 1800.0s"})

    human = dampener_report.render_human(comparison, _baseline({}), Path("b.json"))
    markdown = dampener_report.render_markdown(comparison, _baseline({}), Path("b.json"))

    assert any("test_suite  Test suite timed out after 1800.0s" in line for line in human)
    assert "| `test_suite` | Test suite timed out after 1800.0s |" in markdown
    assert "0 new, 0 increased, 1 stopped measuring" in markdown


def test_the_json_block_carries_the_stopped_sensors() -> None:
    comparison = _comparison(stopped_measuring={"linter": "Linter failed (exit code 127)"})

    document = dampener_report.comparison_document(
        comparison, _baseline({}), _baseline({}), Path("b.json")
    )

    assert document["stopped_measuring"] == {"linter": "Linter failed (exit code 127)"}
    assert document["regressed"] is True
