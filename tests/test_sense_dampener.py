"""R10.6 (#227): the dampener's arithmetic and its baseline document.

No CLI here. These drive :mod:`kstrl.dampener` directly, so the bucket rules
and the on-disk format are pinned independently of how ``ks sense`` wires them
up; ``tests/test_sense_dampener_cli.py`` holds the command surface.
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


# --- the document on disk -----------------------------------------------


def test_write_baseline_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "sense-baseline.json"
    baseline = _baseline({"linter:E501": 2})

    dampener.write_baseline(path, baseline, force=False)

    assert dampener.read_baseline(path) == baseline
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == dampener.BASELINE_SCHEMA_VERSION == 1
    assert document["base_ref"] == "0123456789abcdef"
    assert document["sense_schema_version"] == 2


def test_baseline_keys_are_sorted_in_the_file_bytes(tmp_path: Path) -> None:
    """Assert on the FILE TEXT, not on a re-parse: ``json.loads`` into a dict
    would hide an unsorted write, and unsorted is a whole-file git diff on
    every regeneration."""
    path = tmp_path / "b.json"
    reversed_order = {"typecheck:arg-type": 1, "linter:F401": 1, "linter:E501": 1}

    dampener.write_baseline(
        path,
        _baseline(reversed_order, measured=("typecheck", "linter"), unmeasured=("z", "a")),
        force=False,
    )

    text = path.read_text(encoding="utf-8")
    assert text.index("linter:E501") < text.index("linter:F401") < text.index("typecheck:arg-type")
    assert text.index('"a"') < text.index('"z"')
    # Top-level key ORDER is part of the format: reordering it would produce a
    # whole-file diff on a run that changed nothing.
    assert list(json.loads(text)) == [
        "schema_version",
        "generated_at",
        "base_ref",
        "project",
        "passed",
        "sense_schema_version",
        "verify_digest",
        "measured_checks",
        "unmeasured_checks",
        "unmeasured_reasons",
        "signatures",
    ]


def test_write_refuses_an_existing_file_without_force(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    dampener.write_baseline(path, _baseline({"linter:E501": 1}), force=False)

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.write_baseline(path, _baseline({}), force=False)
    assert str(path) in str(excinfo.value)
    assert "--force" in str(excinfo.value)

    dampener.write_baseline(path, _baseline({}), force=True)
    assert dampener.read_baseline(path).signatures == {}


def test_missing_baseline_names_the_remedy(tmp_path: Path) -> None:
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(tmp_path / "absent.json")

    assert str(excinfo.value) == (
        f"no baseline at {tmp_path / 'absent.json'}; run ks sense --write-baseline first"
    )


def _valid_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-06T00:00:00Z",
        "base_ref": "abc",
        "project": PROJECT,
        "passed": False,
        "sense_schema_version": 2,
        "verify_digest": DIGEST,
        "measured_checks": ["linter"],
        "unmeasured_checks": [],
        "unmeasured_reasons": {},
        "signatures": {"linter:E501": 1},
    }


@pytest.mark.parametrize(
    ("mutate", "expected_in_message"),
    [
        (lambda d: [1, 2, 3], "JSON object"),
        (lambda d: d.pop("schema_version") and d, "schema_version"),
        (lambda d: {**d, "schema_version": 2}, "expected 1"),
        (lambda d: {**d, "schema_version": "1"}, "schema_version"),
        (lambda d: {**d, "passed": "false"}, "passed"),
        (lambda d: {**d, "base_ref": 7}, "base_ref"),
        (lambda d: {**d, "sense_schema_version": True}, "sense_schema_version"),
        (lambda d: {**d, "measured_checks": "linter"}, "measured_checks"),
        (lambda d: {**d, "measured_checks": ["linter", 3]}, "measured_checks'[1]"),
        (lambda d: {**d, "measured_checks": [""]}, "measured_checks'[0]"),
        (lambda d: {**d, "signatures": ["linter:E501"]}, "signatures"),
        (lambda d: {**d, "signatures": {"linter:E501": "1"}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"linter:E501": -1}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"linter:E501": True}}, "'linter:E501'"),
        (lambda d: {**d, "signatures": {"": 1}}, "non-empty string"),
    ],
)
def test_a_malformed_baseline_is_refused_and_names_what_is_wrong(
    mutate: Any,
    expected_in_message: str,
) -> None:
    """Never read leniently. A document read as ``{}`` makes every current
    signature new (or every baseline one vanish) with nothing failing, which
    is the mechanism silently gone."""
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(mutate(_valid_document()))

    assert expected_in_message in str(excinfo.value)


def test_a_baseline_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)
    assert "not JSON" in str(excinfo.value)


def test_a_baseline_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    """``UnicodeDecodeError`` is a ``ValueError`` and escapes a bare
    ``except OSError``, so it is caught explicitly beside it."""
    path = tmp_path / "b.json"
    path.write_bytes(b'{"schema_version": 1, "x": "\xff\xfe"}')

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)
    assert "cannot read the baseline" in str(excinfo.value)


def test_a_wrong_schema_version_names_the_remedy() -> None:
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document({**_valid_document(), "schema_version": 99})

    assert "--write-baseline --force" in str(excinfo.value)


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


@pytest.mark.parametrize("key", ["measured_checks", "unmeasured_checks", "signatures"])
def test_a_missing_collection_is_refused_not_read_as_empty(key: str) -> None:
    """The lenient half of fail-closed, stated separately because it is the
    one that reads as legal. A missing ``measured_checks`` read as ``[]``
    would put every baseline signature in ``unmeasured`` forever, which looks
    exactly like a repository whose sensors are all off."""
    document = _valid_document()
    del document[key]

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(document)
    assert key in str(excinfo.value)


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


# --- the baseline's identity --------------------------------------------


def _commands(lint: str = "ruff check .") -> Any:
    from kstrl.verify import ResolvedVerifyCommands

    return ResolvedVerifyCommands(test="pytest", typecheck="mypy .", lint=lint)


def test_the_digest_moves_with_the_commands_and_with_the_timeout() -> None:
    same = dampener.verify_digest(_commands(), 1800.0)

    assert dampener.verify_digest(_commands(), 1800.0) == same
    assert dampener.verify_digest(_commands("ruff check --fix ."), 1800.0) != same
    assert dampener.verify_digest(_commands(), 300.0) != same


def test_a_baseline_measured_differently_is_refused_naming_both_digests() -> None:
    """``bind_register``'s rule, applied to the baseline.

    ``docs/dampener.md`` already said a baseline and a comparison measured at
    different timeouts are not a comparison; before this the only mechanism
    behind that sentence was a literal 1800 typed into a workflow file.
    """
    baseline = _baseline({}, digest="aaaaaaaaaaaaaaaa")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.refuse_foreign_baseline(baseline, "bbbbbbbbbbbbbbbb")

    message = str(excinfo.value)
    assert "aaaaaaaaaaaaaaaa" in message
    assert "bbbbbbbbbbbbbbbb" in message
    assert "--write-baseline --force" in message


def test_a_baseline_measured_the_same_way_is_accepted() -> None:
    """The control. Without it the refusal above passes with the comparison
    inverted, which would refuse every legitimate run."""
    dampener.refuse_foreign_baseline(_baseline({}, digest=DIGEST), DIGEST)


def test_a_different_project_is_a_note_and_not_a_refusal() -> None:
    """A baseline copied between two checkouts of the same project is
    legitimate; between two different projects it is #260's mistake. Only a
    person can tell those apart, so this reports rather than refuses."""
    comparison = dampener.compare(
        _baseline({}, project="writers-room"),
        _baseline({}, project="kstrl"),
    )

    assert comparison.project_changed == ("writers-room", "kstrl")
    assert comparison.regressed is False
    human = dampener_report.render_human(comparison, _baseline({}), Path("b.json"))
    assert any("'writers-room'" in line and "'kstrl'" in line for line in human)


def test_a_hole_in_a_baseline_carries_the_reason_it_is_there() -> None:
    document = _valid_document()
    document["unmeasured_checks"] = ["dead_code"]

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document(document)

    assert "unmeasured_reasons" in str(excinfo.value)
    assert "dead_code" in str(excinfo.value)


def test_a_signature_count_of_zero_is_refused() -> None:
    """``to_document`` writes a Counter of occurrences, so zero is a shape it
    cannot produce. Accepting one put "was 0" in a report's fixed table."""
    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.Baseline.from_document({**_valid_document(), "signatures": {"linter:E501": 0}})

    assert "positive integer" in str(excinfo.value)


# --- reading a baseline the parser cannot parse -------------------------


def test_a_deeply_nested_baseline_is_refused_and_not_a_traceback(tmp_path: Path) -> None:
    """``RecursionError`` is a ``RuntimeError``, not a ``ValueError``.

    Round 1 of review on #357 measured 200000 nested arrays escaping
    ``except ValueError`` around ``json.loads``, so a document this function
    promises to refuse with exit 2 killed the command with a traceback and
    exit 1. The parser's error taxonomy belongs to the parser (#318).
    """
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(path)

    assert "RecursionError" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_a_directory_where_a_baseline_should_be_is_an_os_error(tmp_path: Path) -> None:
    """The other half of rule 3: the I/O is outside the guard, so widening the
    parse guard to ``Exception`` cannot swallow a disk failure and report it
    as malformed JSON."""
    directory = tmp_path / "b.json"
    directory.mkdir()

    with pytest.raises(dampener.BaselineError) as excinfo:
        dampener.read_baseline(directory)

    assert "cannot read the baseline" in str(excinfo.value)
    assert "is not JSON" not in str(excinfo.value)


# --- where a baseline path resolves --------------------------------------


def test_a_relative_explicit_path_resolves_under_root() -> None:
    """One rule for one flag. Passing the exact path ``--help`` advertises as
    the default, together with ``--root``, used to read a different file and
    report "no baseline at ..." for a file that exists."""
    root = Path("/elsewhere")

    assert dampener._baseline_path("scripts/kstrl/sense-baseline.json", root) == (
        root / "scripts/kstrl/sense-baseline.json"
    )
    assert dampener._baseline_path(dampener.OPTIONAL_VALUE_SENTINEL, root) == (
        root / dampener.DEFAULT_BASELINE_PATH
    )
    assert dampener._baseline_path("/tmp/b.json", root) == Path("/tmp/b.json")


# --- which project a baseline is OF -------------------------------------


def test_the_project_identity_survives_a_worktree(tmp_path: Path) -> None:
    """``owner/repo`` from ``origin``, in both URL shapes and through a
    worktree, because the directory name is exactly what a worktree changes.

    Measured rather than argued: every kstrl lane runs inside a git worktree
    named after an issue number, so a baseline written in one records ``227``
    as its directory name and every later comparison in an ordinary checkout
    reports a mismatch that means nothing.
    """
    from kstrl.git import get_origin_slug
    from tests.spine_utils import git as run_git

    repo = tmp_path / "some-issue-number"
    repo.mkdir()
    run_git("init", "-q", "-b", "main", cwd=repo)
    run_git("remote", "add", "origin", "https://github.com/0xfauzi/kstrl.git", cwd=repo)

    assert get_origin_slug(repo) == "0xfauzi/kstrl"

    run_git("remote", "set-url", "origin", "git@github.com:0xfauzi/kstrl.git", cwd=repo)
    assert get_origin_slug(repo) == "0xfauzi/kstrl"


def test_a_repository_with_no_remote_has_no_slug(tmp_path: Path) -> None:
    """The fallback's precondition. Without this the CLI's
    ``get_origin_slug(path) or path.name`` looks like belt over braces."""
    from kstrl.git import get_origin_slug
    from tests.spine_utils import git as run_git

    repo = tmp_path / "local-only"
    repo.mkdir()
    run_git("init", "-q", "-b", "main", cwd=repo)

    # A directory that is not a repository at all: git exits nonzero and this
    # returns None too. A path that does not EXIST is deliberately not covered:
    # `subprocess` raises FileNotFoundError for a missing cwd, which every
    # helper in kstrl/git.py lets out, and `ks sense` refuses a path that is
    # not a directory before any of them is reached.
    plain = tmp_path / "plain-directory"
    plain.mkdir()

    assert get_origin_slug(repo) is None
    assert get_origin_slug(plain) is None
