"""F5-matchers: unit tests for the calibration matcher helpers.

These tests exercise ``security_caught`` / ``reviewer_caught`` /
``architect_caught`` against synthetic inputs *without* calling any LLM.
Their job is to catch the kind of latent bug that the F5 baseline run
surfaced -- the security matcher used to reference a nonexistent
``finding.evidence`` attribute and had never been executed end-to-end
until a real calibration run lit it up.

Why this matters: the calibration suite is gated behind
``RALPH_RUN_CALIBRATION=1`` and requires real LLM tokens, so its
matcher code is naturally under-tested. Unit-testing the matchers
guarantees they at least *evaluate correctly* against the input shape
they expect, even when nobody runs the LLM-driven integration.

Tests are organized by role: TestSecurityMatcher / TestReviewerMatcher /
TestArchitectMatcher, plus TestKindSynonyms for the R5.1 synonym map
that de-brittles the architect's ``must_include_kind`` matching.
"""

from __future__ import annotations

from ralph_py import calibration
from ralph_py.decompose import SpecIssue
from ralph_py.review import (
    CriterionReview,
    ReviewConcern,
    ReviewResult,
    ReviewVerdict,
)
from ralph_py.security import SecurityFinding, SecurityMode, SecurityResult
from tests.test_calibration import (
    architect_caught,
    build_fp_summary,
    render_verification,
    reviewer_caught,
    reviewer_false_positive,
    security_caught,
    security_false_positive,
)


def _security_result(*findings: SecurityFinding) -> SecurityResult:
    return SecurityResult(
        passed=False,
        mode=SecurityMode.HARD.value,
        findings=list(findings),
    )


def _review_result(*concerns: ReviewConcern) -> ReviewResult:
    return ReviewResult(
        passed=False,
        mode="hard",
        criteria=[
            CriterionReview(
                criterion="placeholder",
                verdict=ReviewVerdict.PASS.value,
                explanation="ok",
            ),
        ],
        concerns=list(concerns),
    )


# ---------------------------------------------------------------------------
# Security matcher
# ---------------------------------------------------------------------------


class TestSecurityMatcher:
    def test_matches_when_category_severity_and_path_all_match(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="critical",
                location="src/users.py:11-13",
                explanation="raw sql in get_user",
            ),
        )
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
            "evidence_path_contains": "src/users.py",
        })
        assert caught
        assert "critical injection at src/users.py:11-13" in detail

    def test_no_match_when_category_differs(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="xss", severity="critical",
                location="src/users.py:11", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught

    def test_no_match_when_severity_below_threshold(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="medium",
                location="src/users.py:11", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught

    def test_no_match_when_location_does_not_contain_expected_path(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="critical",
                location="src/other.py:1", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
            "evidence_path_contains": "src/users.py",
        })
        assert not caught

    def test_path_optional_when_not_required(self) -> None:
        """``evidence_path_contains`` is optional. Omitting it means
        the matcher accepts any location."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="critical",
                location="anywhere.py", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert caught

    def test_first_matching_finding_wins(self) -> None:
        """When multiple findings match, the first one is returned. This
        mirrors how a real gate would decide -- one matching finding is
        enough to flag the planted bug."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="high",
                location="src/users.py:11", explanation="first",
            ),
            SecurityFinding(
                category="injection", severity="critical",
                location="src/users.py:42", explanation="second, more severe",
            ),
        )
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert caught
        # First match returned, NOT the more-severe second one.
        assert "high injection at src/users.py:11" in detail

    def test_skips_non_matching_finding_and_keeps_searching(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="xss", severity="critical",
                location="src/other.py:1", explanation="distractor",
            ),
            SecurityFinding(
                category="injection", severity="critical",
                location="src/users.py:11", explanation="the planted bug",
            ),
        )
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert caught
        assert "src/users.py:11" in detail

    def test_no_match_against_empty_findings(self) -> None:
        result = _security_result()
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught
        assert detail == ""

    def test_unknown_severity_in_finding_treated_as_minimal(self) -> None:
        """A SecurityFinding with a severity string not in
        ``{critical, high, medium, low}`` (e.g., the LLM hallucinated
        ``severe`` or ``major``) is treated as rank 0 and therefore
        cannot meet a non-trivial threshold."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="severe",  # not in taxonomy
                location="src/users.py:1", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught


# ---------------------------------------------------------------------------
# Reviewer matcher
# ---------------------------------------------------------------------------


class TestReviewerMatcher:
    def test_matches_concern_with_correct_category_and_severity(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="fail",
                location="src/parser.py:15-31",
                explanation="unused branch",
            ),
        )
        caught, detail = reviewer_caught(result, {
            "category": "dead_code",
            "severity_at_least": "fail",
        })
        assert caught
        assert "fail dead_code" in detail

    def test_no_match_when_category_differs(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="scope_creep", severity="fail",
                location="src/parser.py", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {
            "category": "dead_code",
            "severity_at_least": "fail",
        })
        assert not caught

    def test_no_match_when_fail_required_but_concern_is_advisory(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="advisory",
                location="src/parser.py", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {
            "category": "dead_code",
            "severity_at_least": "fail",
        })
        assert not caught

    def test_advisory_severity_accepted_when_no_floor(self) -> None:
        """When the fixture does not require ``severity_at_least=fail``,
        an advisory concern is acceptable."""
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="advisory",
                location="src/parser.py", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {"category": "dead_code"})
        assert caught

    def test_path_filter_applied(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="test_quality", severity="fail",
                location="tests/test_other.py:1",
                explanation="distractor",
            ),
            ReviewConcern(
                category="test_quality", severity="fail",
                location="tests/test_calculator.py:6-8",
                explanation="tautological",
            ),
        )
        caught, detail = reviewer_caught(result, {
            "category": "test_quality",
            "severity_at_least": "fail",
            "evidence_path_contains": "test_calculator.py",
        })
        assert caught
        assert "test_calculator.py" in detail

    def test_no_match_against_empty_concerns(self) -> None:
        result = _review_result()
        caught, detail = reviewer_caught(result, {"category": "dead_code"})
        assert not caught
        assert detail == ""


# ---------------------------------------------------------------------------
# Architect matcher
# ---------------------------------------------------------------------------


class TestArchitectMatcher:
    def test_matches_when_count_kinds_and_severity_all_satisfied(self) -> None:
        issues = [
            SpecIssue(
                kind="missing_detail", severity="blocker",
                summary="no auth story",
            ),
            SpecIssue(
                kind="undefined_failure_mode", severity="major",
                summary="missing error path",
            ),
        ]
        caught, detail = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["missing_detail", "undefined_failure_mode"],
            "blocker_or_major": True,
        })
        assert caught
        assert "2 issues" in detail

    def test_no_match_when_count_below_minimum(self) -> None:
        issues = [
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": [],
        })
        assert not caught

    def test_paraphrased_kind_within_synonym_family_is_a_hit(self) -> None:
        """This is the spec-01 case from the F5 baseline -- 8 blocker
        issues, the planted failure-mode issue reported as
        ``missing_detail`` instead of ``undefined_failure_mode``. The
        strict matcher graded taxonomy vocabulary and failed; under the
        R5.1 synonym map the paraphrase is a hit."""
        issues = [
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
            SpecIssue(kind="ambiguity", severity="blocker", summary="..."),
        ]
        caught, detail = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["undefined_failure_mode", "missing_detail"],
        })
        assert caught
        # The exact-label signal stays visible in the detail (non-gating).
        assert "exact_kind_match=False" in detail

    def test_spec_02_unstated_assumption_paraphrase_is_a_hit(self) -> None:
        """The other recorded matcher artifact: spec-02 requires
        ``unstated_assumption`` and the model reports it as
        ``missing_detail`` (baselines 20260527-191337 / -195157)."""
        issues = [
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
            SpecIssue(kind="missing_detail", severity="major", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["missing_detail", "unstated_assumption"],
            "blocker_or_major": True,
        })
        assert caught

    def test_no_match_when_required_kind_outside_synonym_family(self) -> None:
        """Kinds outside the spec-silence family still demand an exact
        label: a required ``contradiction`` is NOT satisfied by any
        number of missing_detail / ambiguity issues."""
        issues = [
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
            SpecIssue(kind="ambiguity", severity="blocker", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["contradiction"],
        })
        assert not caught

    def test_exact_kind_match_reported_true_when_labels_exact(self) -> None:
        issues = [
            SpecIssue(
                kind="undefined_failure_mode", severity="blocker", summary="...",
            ),
        ]
        caught, detail = architect_caught(issues, {
            "spec_issues_min": 1,
            "must_include_kind": ["undefined_failure_mode"],
        })
        assert caught
        assert "exact_kind_match=True" in detail

    def test_no_match_when_blocker_or_major_required_but_only_minor(self) -> None:
        issues = [
            SpecIssue(kind="ambiguity", severity="minor", summary="..."),
            SpecIssue(kind="ambiguity", severity="minor", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["ambiguity"],
            "blocker_or_major": True,
        })
        assert not caught

    def test_matches_when_blocker_or_major_satisfied_by_either(self) -> None:
        major_only = [
            SpecIssue(kind="ambiguity", severity="major", summary="..."),
            SpecIssue(kind="ambiguity", severity="minor", summary="..."),
        ]
        caught, _ = architect_caught(major_only, {
            "spec_issues_min": 2,
            "must_include_kind": ["ambiguity"],
            "blocker_or_major": True,
        })
        assert caught

    def test_required_kinds_field_optional(self) -> None:
        """When ``must_include_kind`` is empty, the matcher passes
        regardless of which kinds appear."""
        issues = [
            SpecIssue(kind="other", severity="blocker", summary="..."),
            SpecIssue(kind="ambiguity", severity="major", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 1,
        })
        assert caught

    def test_no_match_against_empty_issues(self) -> None:
        caught, detail = architect_caught([], {"spec_issues_min": 1})
        assert not caught
        assert "got 0 issues" in detail

    def test_default_min_count_is_one(self) -> None:
        """When ``spec_issues_min`` is omitted, at least one issue is
        still required."""
        issues = [SpecIssue(kind="ambiguity", severity="major", summary="...")]
        caught, _ = architect_caught(issues, {})
        assert caught

        empty: list[SpecIssue] = []
        caught_empty, _ = architect_caught(empty, {})
        assert not caught_empty


# ---------------------------------------------------------------------------
# Kind synonym map (R5.1)
# ---------------------------------------------------------------------------


class TestKindSynonyms:
    """Unit tests for ``calibration.KIND_SYNONYM_GROUPS`` and its helpers.

    The map exists because the boundary inside the "spec is silent
    about X" family (missing_detail / unstated_assumption /
    undefined_failure_mode) is a judgment call the model makes
    differently run to run; every architect miss in the recorded
    baselines is a paraphrase within that family.
    """

    def test_family_members_accept_each_other(self) -> None:
        family = {"missing_detail", "unstated_assumption", "undefined_failure_mode"}
        for required in family:
            for actual in family:
                assert calibration.required_kinds_satisfied([required], [actual]), (
                    f"{required} should be satisfied by {actual}"
                )

    def test_non_family_kinds_only_match_themselves(self) -> None:
        for kind in ("ambiguity", "contradiction", "out_of_scope_creep", "other"):
            assert calibration.acceptable_kinds(kind) == frozenset({kind})
            assert not calibration.required_kinds_satisfied(
                [kind], ["missing_detail"],
            )
            assert calibration.required_kinds_satisfied([kind], [kind])

    def test_ambiguity_not_satisfied_by_silence_family(self) -> None:
        """Ambiguity is about vague language that IS present, not
        absence: it deliberately stays outside the synonym family."""
        assert not calibration.required_kinds_satisfied(
            ["ambiguity"], ["missing_detail", "unstated_assumption"],
        )

    def test_all_required_kinds_must_be_satisfied(self) -> None:
        assert not calibration.required_kinds_satisfied(
            ["missing_detail", "contradiction"], ["missing_detail"],
        )
        assert calibration.required_kinds_satisfied(
            ["missing_detail", "contradiction"],
            ["unstated_assumption", "contradiction"],
        )

    def test_empty_required_kinds_always_satisfied(self) -> None:
        assert calibration.required_kinds_satisfied([], [])
        assert calibration.required_kinds_satisfied([], ["anything"])

    def test_exact_kinds_present_ignores_synonyms(self) -> None:
        assert not calibration.exact_kinds_present(
            ["undefined_failure_mode"], ["missing_detail"],
        )
        assert calibration.exact_kinds_present(
            ["undefined_failure_mode"], ["undefined_failure_mode"],
        )

    def test_synonym_groups_stay_within_decompose_taxonomy(self) -> None:
        """Guard against a synonym group drifting away from the
        DECOMPOSE taxonomy: every member must be a valid spec-issue
        kind that ``_parse_spec_issues`` would accept."""
        from ralph_py.decompose import _VALID_KINDS

        for group in calibration.KIND_SYNONYM_GROUPS:
            assert group <= _VALID_KINDS, (
                f"synonym group {sorted(group)} contains kinds outside "
                f"the DECOMPOSE taxonomy {sorted(_VALID_KINDS)}"
            )


# ---------------------------------------------------------------------------
# category_any_of (R5.2): subtle fixtures whose finding could be labelled
# under more than one taxonomy bucket
# ---------------------------------------------------------------------------


class TestCategoryAnyOf:
    def test_security_matches_any_listed_category(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="information_disclosure", severity="high",
                location="src/webhooks/signing.py:20", explanation="timing",
            ),
        )
        caught, detail = security_caught(result, {
            "category_any_of": ["broken_crypto", "information_disclosure"],
            "severity_at_least": "medium",
        })
        assert caught
        assert "information_disclosure" in detail

    def test_security_rejects_category_outside_the_list(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="xss", severity="critical",
                location="src/webhooks/signing.py:20", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category_any_of": ["broken_crypto", "information_disclosure"],
            "severity_at_least": "medium",
        })
        assert not caught

    def test_security_any_of_still_honors_severity_floor(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="broken_crypto", severity="low",
                location="src/webhooks/signing.py:20", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category_any_of": ["broken_crypto", "information_disclosure"],
            "severity_at_least": "high",
        })
        assert not caught

    def test_security_any_of_still_honors_path_filter(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="broken_crypto", severity="high",
                location="src/other.py:1", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category_any_of": ["broken_crypto"],
            "severity_at_least": "high",
            "evidence_path_contains": "src/webhooks/signing.py",
        })
        assert not caught

    def test_reviewer_matches_any_listed_category(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="scope_creep", severity="fail",
                location="src/x.py:1", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {
            "category_any_of": ["dead_code", "scope_creep"],
            "severity_at_least": "fail",
        })
        assert caught


# ---------------------------------------------------------------------------
# False-positive matchers (R5.2 negatives)
# ---------------------------------------------------------------------------


class TestSecurityFalsePositive:
    def test_forbidden_category_at_floor_is_fp(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="high",
                location="src/search/query.py:40", explanation="...",
            ),
        )
        is_fp, detail = security_false_positive(result, {
            "categories": ["injection"],
            "severity_at_least": "medium",
        })
        assert is_fp
        assert "injection" in detail

    def test_forbidden_category_below_floor_is_not_fp(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="low",
                location="src/search/query.py:40", explanation="nit",
            ),
        )
        is_fp, _ = security_false_positive(result, {
            "categories": ["injection"],
            "severity_at_least": "high",
        })
        assert not is_fp

    def test_non_forbidden_category_is_not_fp(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="other", severity="critical",
                location="src/search/query.py:40", explanation="style",
            ),
        )
        is_fp, _ = security_false_positive(result, {
            "categories": ["injection"],
            "severity_at_least": "medium",
        })
        assert not is_fp

    def test_empty_findings_is_not_fp(self) -> None:
        is_fp, detail = security_false_positive(_security_result(), {
            "categories": ["injection"],
            "severity_at_least": "medium",
        })
        assert not is_fp
        assert detail == ""

    def test_default_floor_is_high(self) -> None:
        """With no severity_at_least, a medium forbidden finding is below
        the default 'high' floor and does not count."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="medium",
                location="src/x.py:1", explanation="...",
            ),
        )
        assert not security_false_positive(result, {"categories": ["injection"]})[0]


class TestReviewerFalsePositive:
    def test_blocking_forbidden_concern_is_fp(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="fail",
                location="src/sandbox/config.py:15", explanation="...",
            ),
        )
        is_fp, detail = reviewer_false_positive(result, {
            "categories": ["dead_code", "scope_creep"],
            "severity_at_least": "fail",
        })
        assert is_fp
        assert "dead_code" in detail

    def test_advisory_forbidden_concern_not_fp_when_floor_is_fail(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="advisory",
                location="src/sandbox/config.py:15", explanation="...",
            ),
        )
        is_fp, _ = reviewer_false_positive(result, {
            "categories": ["dead_code"],
            "severity_at_least": "fail",
        })
        assert not is_fp

    def test_advisory_counts_when_floor_is_not_fail(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="advisory",
                location="src/sandbox/config.py:15", explanation="...",
            ),
        )
        is_fp, _ = reviewer_false_positive(result, {
            "categories": ["dead_code"],
            "severity_at_least": "advisory",
        })
        assert is_fp

    def test_non_forbidden_concern_not_fp(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="test_quality", severity="fail",
                location="tests/test_x.py:1", explanation="...",
            ),
        )
        is_fp, _ = reviewer_false_positive(result, {
            "categories": ["dead_code"],
            "severity_at_least": "fail",
        })
        assert not is_fp

    def test_empty_concerns_not_fp(self) -> None:
        is_fp, detail = reviewer_false_positive(_review_result(), {
            "categories": ["dead_code"],
            "severity_at_least": "fail",
        })
        assert not is_fp
        assert detail == ""


# ---------------------------------------------------------------------------
# FP summary math (R5.2): per-run negative records -> per-role fp_rate.
# Mirrors the detection side: a fixture is a false positive by majority
# vote over its completed runs; role fp_rate = fp_fixtures / total.
# ---------------------------------------------------------------------------


def _fp_runs(role: str, fixture_id: str, flags: list[bool],
             errors: list[bool] | None = None) -> list[dict]:
    errs = errors or [False] * len(flags)
    return [
        {"role": role, "fixture_id": fixture_id,
         "false_positive": f, "error": e, "detail": ""}
        for f, e in zip(flags, errs, strict=True)
    ]


class TestBuildFpSummary:
    def test_majority_flag_marks_fixture_false_positive(self) -> None:
        records = _fp_runs("security_negative", "n1", [True, True, False])
        summary = build_fp_summary(records)
        role = summary["roles"]["security_negative"]
        assert role["fixtures_total"] == 1
        assert role["fixtures_false_positive"] == 1
        assert role["fp_rate"] == 1.0
        fx = role["fixtures"][0]
        assert fx["runs_flagged"] == 2
        assert fx["false_positive"] is True

    def test_minority_flag_is_not_false_positive(self) -> None:
        records = _fp_runs("security_negative", "n1", [True, False, False])
        role = build_fp_summary(records)["roles"]["security_negative"]
        assert role["fixtures_false_positive"] == 0
        assert role["fp_rate"] == 0.0

    def test_fp_rate_is_fraction_of_fixtures(self) -> None:
        records = (
            _fp_runs("security_negative", "n1", [True, True])
            + _fp_runs("security_negative", "n2", [False, False])
            + _fp_runs("security_negative", "n3", [False, False])
            + _fp_runs("security_negative", "n4", [False, False])
        )
        role = build_fp_summary(records)["roles"]["security_negative"]
        assert role["fixtures_total"] == 4
        assert role["fixtures_false_positive"] == 1
        assert role["fp_rate"] == 0.25

    def test_errored_runs_excluded_from_denominator(self) -> None:
        # one real flag, two infra errors -> completed=1, flagged=1 -> FP
        records = _fp_runs(
            "reviewer_negative", "n1", [True, False, False],
            errors=[False, True, True],
        )
        role = build_fp_summary(records)["roles"]["reviewer_negative"]
        fx = role["fixtures"][0]
        assert fx["runs_errored"] == 2
        assert fx["fp_consistency"] == 1.0
        assert fx["false_positive"] is True

    def test_all_errored_fixture_is_not_false_positive(self) -> None:
        records = _fp_runs(
            "reviewer_negative", "n1", [False, False],
            errors=[True, True],
        )
        role = build_fp_summary(records)["roles"]["reviewer_negative"]
        assert role["fixtures_false_positive"] == 0

    def test_meets_threshold_flag(self) -> None:
        clean = build_fp_summary(
            _fp_runs("n", "a", [False, False])
            + _fp_runs("n", "b", [False, False])
        )["roles"]["n"]
        assert clean["fp_rate"] == 0.0
        assert clean["meets_threshold"] is True

        noisy = build_fp_summary(
            _fp_runs("n", "a", [True, True])
            + _fp_runs("n", "b", [True, True])
        )["roles"]["n"]
        assert noisy["fp_rate"] == 1.0
        assert noisy["meets_threshold"] is False

    def test_empty_records_yield_empty_roles(self) -> None:
        summary = build_fp_summary([])
        assert summary["roles"] == {}
        assert "fp_rate_max" in summary


# ---------------------------------------------------------------------------
# Verification rendering (R5.2 context realism)
# ---------------------------------------------------------------------------


class TestRenderVerification:
    def test_default_when_absent_is_production_shaped(self) -> None:
        rendered = render_verification({})
        lines = rendered.splitlines()
        assert lines
        assert all(line.startswith("- ") for line in lines)
        assert any("test_suite: PASS - " in line for line in lines)
        # The old stub used check names the harness never emits.
        assert "tests: PASS" not in rendered

    def test_uses_fixture_supplied_checks(self) -> None:
        meta = {"verification": [
            {"name": "test_suite", "passed": False, "message": "2 failed"},
            {"name": "typecheck", "passed": True, "message": "ok"},
        ]}
        rendered = render_verification(meta)
        assert "- test_suite: FAIL - 2 failed" in rendered
        assert "- typecheck: PASS - ok" in rendered
