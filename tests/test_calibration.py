"""Calibration runner: measures whether each adversarial role catches
its planted bugs.

This test suite is OPT-IN: it makes real LLM calls and is therefore
gated behind ``RALPH_RUN_CALIBRATION=1``. Without that env var the
tests are skipped so default CI (and local `uv run pytest`) doesn't
burn API tokens.

When run, the suite iterates over fixtures in
``tests/adversarial_fixtures/{security,concerns,specs}/``, feeds each
fixture's diff or spec to the corresponding role prompt against a
fast model (Haiku-class), and gates on detection over
``RALPH_CALIBRATION_RUNS`` runs per fixture (default 3, R5.1): a
fixture passes when a majority of its completed runs catch the
planted issue (``calibration.FIXTURE_DETECTION_THRESHOLD``), so
single-run LLM variance is reported as consistency instead of
failing the suite, while a fixture that misses most runs is a
regression and fails. Results (per-fixture consistency, per-role and
per-category detection rates, the model id) are written to
``tests/adversarial_fixtures/_results/baseline-<UTC-date>.json`` in
the v2 format defined by :mod:`kstrl.calibration`; compare against
a previous baseline with
``python -m kstrl.calibration compare <old.json> <new.json>``.

R5.2 adds two axes on top of the R5.1 detection gate:
- HARD positives (``security/`` ``difficulty: hard``: multi-hop authz,
  second-order injection, TOCTOU, timing oracle) are recorded under role
  ``security_hard`` and MEASURED, not gated -- they are designed to be
  missable, so a miss is the hardness signal, not a regression.
- NEGATIVE fixtures (``security_negative/`` and ``concerns_negative/``):
  clean-but-nontrivial diffs graded on whether a forbidden
  (``must_not_flag``) category is raised. The per-role false-positive
  rate lands in the report's ``false_positive_analysis`` block (the
  metric that keeps hard-mode halts credible).
Context realism (R5.2): every fixture carries a real PRD and a
production-shaped verification summary (see ``render_verification``); the
old all-PASS stub with fictional check names is gone.

Why this matters: the entire adversarial-roles design relies on the
LLM actually behaving adversarially under the framing. Without
measurement we have no idea whether the prompts catch real bugs -- or
how often they cry wolf on clean code.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kstrl import calibration
from kstrl.decompose import (
    SpecIssue,
    _extract_agent_json,
    _parse_spec_issues,
    _select_agent_output,
    build_decompose_prompt,
    generate_data_delimiter,
)
from kstrl.review import (
    REVIEWER_PROMPT,
    VALID_CONCERN_CATEGORIES,
    CriterionReview,
    ReviewConcern,
    ReviewResult,
    ReviewVerdict,
    parse_review_output,
)
from kstrl.security import (
    VALID_CATEGORIES,
    SecurityFinding,
    SecurityMode,
    SecurityResult,
    _build_security_prompt,
    parse_security_output,
)

CALIBRATION_ENABLED = os.environ.get("RALPH_RUN_CALIBRATION") == "1"
CALIBRATION_MODEL = os.environ.get("RALPH_CALIBRATION_MODEL", "haiku")
CALIBRATION_RUNS = int(
    os.environ.get(
        "RALPH_CALIBRATION_RUNS", str(calibration.DEFAULT_CALIBRATION_RUNS),
    )
)
# R7.1: optional reviewer-family override so the user can capture
# same-family vs cross-family baselines with the same tooling. Applies
# to the reviewer and security roles only; the architect keeps the base
# calibration agent. The report's model label carries the override so
# baseline comparisons surface the family change as a cross-model
# warning instead of hiding it.
REVIEWER_AGENT_TYPE, REVIEWER_MODEL = calibration.reviewer_override_from_env(
    os.environ,
)
REPORT_MODEL_LABEL = calibration.reviewer_override_label(
    CALIBRATION_MODEL, REVIEWER_AGENT_TYPE, REVIEWER_MODEL,
)

FIXTURES_DIR = Path(__file__).parent / "adversarial_fixtures"
RESULTS_DIR = FIXTURES_DIR / "_results"

# False-positive ceiling (R5.2). A negative role's fp_rate must be at or
# below this to keep hard-mode halts credible. FP is measured per-fixture
# by majority vote over the N runs, mirroring the detection gate: a
# fixture counts as a false positive when a majority of its completed
# runs flag a forbidden (must_not_flag) category. The rate joins the v2
# detection report as a ``false_positive_analysis`` block (see
# _DetectionReport.save). Detection thresholds live in kstrl.calibration
# (R5.1); this is the FP counterpart, kept test-side since the negative
# fixtures are an R5.2 addition.
FP_RATE_MAX = 0.34

# Realistic mechanical-verification context. The old ``_VERIFICATION_STUB``
# used check names the harness never emits ("tests", "lint"); the real
# reviewer prompt is fed one ``- <check.name>: <PASS|FAIL> - <message>``
# line per check by review.build_review_prompt, where the names come from
# verify.py (test_suite / typecheck / linter / diff_scope / self_critique).
# A clean, passing diff produces this.
_DEFAULT_VERIFICATION: tuple[dict[str, object], ...] = (
    {"name": "test_suite", "passed": True, "message": "Tests passed"},
    {"name": "typecheck", "passed": True, "message": "Typecheck passed"},
    {"name": "linter", "passed": True, "message": "Linter passed"},
    {"name": "diff_scope", "passed": True, "message": "all files within scope"},
    {"name": "self_critique", "passed": True, "message": "failure modes listed"},
)


def render_verification(meta: dict) -> str:
    """Render a fixture's verification context into the exact shape the
    factory feeds Phase 2 review: one ``- <name>: <PASS|FAIL> - <message>``
    line per check (see review.build_review_prompt).

    Uses the fixture's ``verification`` list when present, so a fixture
    can encode a realistic mixed pass/fail context; otherwise falls back
    to a production-shaped all-pass default. This replaces the old
    all-PASS ``_VERIFICATION_STUB`` (R5.2 context realism) whose check
    names matched no real verifier, so measured reviewer detection now
    transfers to production.
    """
    checks: tuple[dict[str, object], ...] | list[dict] = (
        meta.get("verification") or _DEFAULT_VERIFICATION
    )
    lines: list[str] = []
    for check in checks:
        status = "PASS" if check.get("passed", True) else "FAIL"
        name = str(check.get("name", "check"))
        message = str(check.get("message", ""))
        lines.append(f"- {name}: {status} - {message}")
    return "\n".join(lines)


# Skip per-test (not module-level) so structural sanity tests in
# TestFixtureStructure still run without the env var.
_skip_unless_calibrating = pytest.mark.skipif(
    not CALIBRATION_ENABLED,
    reason="Calibration suite requires RALPH_RUN_CALIBRATION=1 (makes real LLM calls)",
)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_fixtures(subdir: str, suffix: str) -> list[tuple[Path, dict]]:
    """Return list of (artifact_path, meta_dict) for each fixture."""
    base = FIXTURES_DIR / subdir
    fixtures: list[tuple[Path, dict]] = []
    for artifact in sorted(base.glob(f"*{suffix}")):
        meta_path = artifact.with_suffix(".meta.json")
        if not meta_path.exists():
            # Try alternate: <stem>.meta.json regardless of artifact suffix
            meta_path = artifact.parent / f"{artifact.stem}.meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        fixtures.append((artifact, meta))
    return fixtures


def _security_fixtures() -> list[tuple[Path, dict]]:
    return _load_fixtures("security", ".diff")


def _security_positive_easy_fixtures() -> list[tuple[Path, dict]]:
    """Textbook-trivial planted bugs (catastrophe detectors). A miss here
    is a real regression, so these go through the N-run consistency gate."""
    return [
        (a, m) for a, m in _security_fixtures() if m.get("difficulty") != "hard"
    ]


def _security_positive_hard_fixtures() -> list[tuple[Path, dict]]:
    """The R5.2 subtle planted bugs (multi-hop authz, second-order
    injection, TOCTOU, timing oracle). Designed to be missable, so
    detection is MEASURED (recorded), not gated (see the hard-positive
    test docstring)."""
    return [
        (a, m) for a, m in _security_fixtures() if m.get("difficulty") == "hard"
    ]


def _security_negative_fixtures() -> list[tuple[Path, dict]]:
    """Clean-but-nontrivial security diffs (R5.2) for measuring the
    security reviewer's false-positive rate."""
    return _load_fixtures("security_negative", ".diff")


def _concern_fixtures() -> list[tuple[Path, dict]]:
    return _load_fixtures("concerns", ".diff")


def _concern_negative_fixtures() -> list[tuple[Path, dict]]:
    """Clean-but-nontrivial diffs (R5.2) for measuring reviewer false
    positives: used-helper refactor, thorough tests, correct error
    handling, intentional public API."""
    return _load_fixtures("concerns_negative", ".diff")


def _spec_fixtures() -> list[tuple[Path, dict]]:
    return _load_fixtures("specs", ".md")


def _halting_spec_fixtures() -> list[tuple[Path, dict]]:
    """Spec fixtures that grade architect spec-issue detection
    (``must_detect`` schema). Excludes non-halting fixtures whose
    grading is on ``allowedPaths`` emission, not issue detection."""
    return [
        (artifact, meta) for artifact, meta in _spec_fixtures()
        if "must_detect" in meta
    ]


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


def _get_calibration_agent():
    """Return an agent suitable for calibration (fast, cheap)."""
    from kstrl.agents import get_agent
    return get_agent(
        agent_cmd=None,
        model=CALIBRATION_MODEL,
        model_reasoning_effort=None,
        agent_type="claude-code",
    )


def _get_reviewer_calibration_agent():
    """Agent for the reviewer and security roles: the base calibration
    agent unless the R7.1 reviewer-family override
    (RALPH_CALIBRATION_REVIEWER_AGENT_TYPE / _MODEL) selects another
    family for the same-family vs cross-family baseline comparison."""
    from kstrl.agents import get_agent
    return get_agent(
        agent_cmd=None,
        model=REVIEWER_MODEL or CALIBRATION_MODEL,
        model_reasoning_effort=None,
        agent_type=REVIEWER_AGENT_TYPE or "claude-code",
    )


def _collect(agent, prompt: str, cwd: Path) -> list[str]:
    output: list[str] = []
    for line in agent.run(prompt, cwd=cwd, timeout=300.0):
        output.append(line)
    return output


# ---------------------------------------------------------------------------
# Severity ordering shared with security.py
# ---------------------------------------------------------------------------


_SEV_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _meets_severity(actual: str, threshold: str) -> bool:
    return _SEV_RANK.get(actual, 0) >= _SEV_RANK.get(threshold, 0)


# ---------------------------------------------------------------------------
# Matcher helpers (F5-matchers)
#
# Extracted as top-level functions so they can be unit-tested against
# synthetic inputs in tests/test_calibration_matchers.py without
# requiring a real LLM call. The integration tests below feed the same
# helpers with output produced by a live agent under
# RALPH_RUN_CALIBRATION=1. Identical matcher means: a passing unit
# test = the matcher works against the assumptions encoded in the
# fixture meta; a passing integration test = the matcher AND the LLM
# both held up.
# ---------------------------------------------------------------------------


def _acceptable_categories(requirement: dict) -> set[str]:
    """Categories that count as a hit for a positive fixture.

    Supports either a single ``category`` (legacy fixtures) or a
    ``category_any_of`` list. The list form exists for the R5.2 subtle
    bugs whose genuine finding could be labelled under more than one
    taxonomy bucket -- e.g. a signature-comparison timing oracle is
    defensibly ``broken_crypto`` OR ``information_disclosure`` OR
    ``auth_bypass``. Accepting any of them measures *whether the model
    saw the flaw*, not whether it guessed our preferred label; the
    ``evidence_path_contains`` gate still pins the finding to the right
    file so this does not become a free pass.
    """
    if "category_any_of" in requirement:
        return {str(c) for c in requirement["category_any_of"]}
    return {str(requirement["category"])}


def security_caught(
    result: SecurityResult, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the security matcher.

    A finding counts as catching the planted bug when its category is
    among the acceptable categories, severity meets the threshold, and
    (optionally) location contains the expected path substring. First
    match wins; subsequent findings are ignored even if more severe --
    this mirrors how the review path would gate (one matching finding
    is enough).
    """
    acceptable = _acceptable_categories(requirement)
    for finding in result.findings:
        if finding.category not in acceptable:
            continue
        if not _meets_severity(finding.severity, requirement["severity_at_least"]):
            continue
        if requirement.get("evidence_path_contains"):
            if requirement["evidence_path_contains"] not in finding.location:
                continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def security_false_positive(
    result: SecurityResult, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(is_false_positive, detail)`` for a security NEGATIVE
    fixture.

    ``requirement`` is the fixture's ``must_not_flag`` block:
    ``{"categories": [...], "severity_at_least": "high"}``. A false
    positive is any finding whose category is forbidden AND whose
    severity meets the floor (default ``high`` -- the level at which a
    security finding is actionable enough to erode a hard-mode halt).
    Sub-floor findings are ignored so a low-severity nit does not
    inflate the FP rate.
    """
    forbidden = {str(c) for c in requirement.get("categories", [])}
    floor = str(requirement.get("severity_at_least", "high"))
    for finding in result.findings:
        if finding.category not in forbidden:
            continue
        if not _meets_severity(finding.severity, floor):
            continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def reviewer_caught(
    result: ReviewResult, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the reviewer matcher.

    A concern counts as catching the planted issue when its category is
    among the acceptable categories, and (optionally) severity is
    ``"fail"`` when the fixture demands ``severity_at_least == "fail"``,
    and (optionally) location contains the expected path substring.
    """
    acceptable = _acceptable_categories(requirement)
    for concern in result.concerns:
        if concern.category not in acceptable:
            continue
        if requirement.get("severity_at_least") == "fail":
            if concern.severity != "fail":
                continue
        if requirement.get("evidence_path_contains"):
            if requirement["evidence_path_contains"] not in concern.location:
                continue
        return True, (
            f"{concern.severity} {concern.category} at {concern.location}"
        )
    return False, ""


def reviewer_false_positive(
    result: ReviewResult, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(is_false_positive, detail)`` for a reviewer NEGATIVE
    fixture.

    ``requirement`` is the fixture's ``must_not_flag`` block. A false
    positive is a concern whose category is forbidden. When the floor is
    ``fail`` (the default), only *blocking* concerns count: an advisory
    concern does not halt the PR, so it does not erode halt credibility.
    Any other floor also counts advisory concerns.
    """
    forbidden = {str(c) for c in requirement.get("categories", [])}
    floor = str(requirement.get("severity_at_least", "fail"))
    for concern in result.concerns:
        if concern.category not in forbidden:
            continue
        if floor == "fail" and concern.severity != "fail":
            continue
        return True, (
            f"{concern.severity} {concern.category} at {concern.location}"
        )
    return False, ""


def architect_caught(
    issues: list[SpecIssue], requirement: dict,
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the architect spec-issue matcher.

    Catches when: total issue count >= ``spec_issues_min``, every kind
    in ``must_include_kind`` is present *up to the documented synonym
    map* (``calibration.KIND_SYNONYM_GROUPS`` - R5.1: the recorded
    baselines show the model paraphrases within the spec-silence
    family, so an exact-label demand grades taxonomy vocabulary, not
    detection), and (when ``blocker_or_major == True``) at least one
    issue is blocker or major severity. Whether the exact labels were
    used is reported in the detail as a non-gating signal.
    """
    min_count = requirement.get("spec_issues_min", 1)
    required_kinds = list(requirement.get("must_include_kind", []))
    must_be_major_or_blocker = requirement.get("blocker_or_major", False)
    actual_kinds = {i.kind for i in issues}
    caught = (
        len(issues) >= min_count
        and calibration.required_kinds_satisfied(required_kinds, actual_kinds)
        and (
            not must_be_major_or_blocker
            or any(i.severity in {"blocker", "major"} for i in issues)
        )
    )
    exact = calibration.exact_kinds_present(required_kinds, actual_kinds)
    detail = (
        f"got {len(issues)} issues (exact_kind_match={exact}): "
        f"{[(i.severity, i.kind) for i in issues]}"
    )
    return caught, detail


def architect_allowed_paths_caught(
    decompose_output: dict, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the architect ``allowedPaths``
    emission matcher.

    Grades a non-halting architect decomposition on whether each emitted
    component carries a sensible ``allowedPaths`` per the v1.2.0
    DECOMPOSE_PROMPT rule #12. The requirement dict supports:

    - ``non_halting`` (bool): the architect must emit at least one
      component. Halting on a non-halting fixture is a regression.
    - ``every_component_has_allowed_paths`` (bool): every emitted
      component must carry the field.
    - ``excludes_harness_internals`` (list[str]): no allowedPaths
      entry across any component may contain any of these substrings.
    - ``includes_test_root_prefix`` (str): every component's
      ``allowedPaths`` must contain at least one entry starting with
      this prefix.
    - ``includes_feature_subtree`` (bool): each component's
      ``allowedPaths`` must include ``scripts/ralph/feature/<id>/``.
    """
    components = decompose_output.get("components", [])

    if requirement.get("non_halting") and not components:
        return False, "architect halted (returned components=[]); expected components"

    if requirement.get("every_component_has_allowed_paths"):
        for c in components:
            if not isinstance(c, dict):
                continue
            ap = c.get("allowedPaths")
            if not ap:
                return False, (
                    f"component {c.get('id', '?')} missing allowedPaths "
                    f"(got {ap!r})"
                )

    forbidden = requirement.get("excludes_harness_internals", [])
    if forbidden:
        for c in components:
            if not isinstance(c, dict):
                continue
            for entry in c.get("allowedPaths", []) or []:
                if not isinstance(entry, str):
                    continue
                for forbid in forbidden:
                    if forbid in entry:
                        return False, (
                            f"component {c.get('id', '?')} allowedPaths "
                            f"contains forbidden harness internal: "
                            f"{entry!r} matches {forbid!r}"
                        )

    test_root = requirement.get("includes_test_root_prefix")
    if test_root:
        for c in components:
            if not isinstance(c, dict):
                continue
            entries = c.get("allowedPaths", []) or []
            if not any(
                isinstance(e, str) and e.startswith(test_root) for e in entries
            ):
                return False, (
                    f"component {c.get('id', '?')} missing test-root "
                    f"prefix {test_root!r} in allowedPaths={entries!r}"
                )

    if requirement.get("includes_feature_subtree"):
        for c in components:
            if not isinstance(c, dict):
                continue
            comp_id = c.get("id", "")
            expected = f"scripts/ralph/feature/{comp_id}/"
            entries = c.get("allowedPaths", []) or []
            if not any(
                isinstance(e, str) and expected in e for e in entries
            ):
                return False, (
                    f"component {comp_id} missing feature subtree "
                    f"{expected!r} in allowedPaths={entries!r}"
                )

    summary = ", ".join(
        f"{c.get('id', '?')}={c.get('allowedPaths', [])}" for c in components
    )
    return True, f"{len(components)} components, all gate-clean: {summary}"


# ---------------------------------------------------------------------------
# Detection report (v2 format, built by kstrl.calibration) + R5.2 FP block
# ---------------------------------------------------------------------------


def build_fp_summary(
    fp_records: list[dict], *, fixture_threshold: float | None = None,
) -> dict:
    """Aggregate per-run NEGATIVE-fixture records into a false-positive
    report block (R5.2).

    Mirrors the detection side: a fixture is a false positive when a
    majority of its completed runs flag a forbidden category
    (``fixture_threshold``, default ``calibration.FIXTURE_DETECTION_THRESHOLD``);
    a role's ``fp_rate`` is the fraction of its fixtures that are false
    positives. Infrastructure errors are excluded from the per-fixture
    denominator. Pure (no I/O) so the math is unit-testable without a
    live model. Each record is
    ``{"role","fixture_id","false_positive","error","detail"}``.
    """
    threshold = (
        calibration.FIXTURE_DETECTION_THRESHOLD
        if fixture_threshold is None else fixture_threshold
    )
    grouped: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for rec in fp_records:
        key = (str(rec["role"]), str(rec["fixture_id"]))
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(rec)

    roles: dict[str, dict] = {}
    for role, fixture_id in order:
        runs = grouped[(role, fixture_id)]
        errored = sum(1 for r in runs if bool(r.get("error")))
        flagged = sum(
            1 for r in runs
            if bool(r.get("false_positive")) and not bool(r.get("error"))
        )
        completed = len(runs) - errored
        fp_consistency = calibration.consistency(flagged, completed)
        is_fp = completed > 0 and fp_consistency >= threshold
        block = roles.setdefault(role, {
            "fixtures_total": 0,
            "fixtures_false_positive": 0,
            "fixtures": [],
        })
        block["fixtures_total"] += 1
        if is_fp:
            block["fixtures_false_positive"] += 1
        block["fixtures"].append({
            "fixture_id": fixture_id,
            "runs_total": len(runs),
            "runs_errored": errored,
            "runs_flagged": flagged,
            "fp_consistency": fp_consistency,
            "false_positive": is_fp,
        })

    for block in roles.values():
        total = block["fixtures_total"]
        rate = block["fixtures_false_positive"] / total if total else 0.0
        block["fp_rate"] = rate
        block["meets_threshold"] = rate <= FP_RATE_MAX

    return {"fp_rate_max": FP_RATE_MAX, "roles": roles}


class _DetectionReport:
    """Accumulates one record per RUN (not per fixture) and delegates
    detection aggregation + the on-disk format to
    :mod:`kstrl.calibration`. Negative-fixture (false-positive) runs
    are kept separately (R5.2) and injected as a ``false_positive_analysis``
    block at save time."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.fp_records: list[dict] = []

    def record(
        self,
        role: str,
        fixture_id: str,
        caught: bool,
        detail: str = "",
        *,
        category: str | None = None,
        cwe: str | None = None,
        error: bool = False,
    ) -> None:
        self.records.append({
            "role": role,
            "fixture_id": fixture_id,
            "category": category,
            "cwe": cwe,
            "caught": caught,
            "error": error,
            "detail": detail,
        })

    def record_fp(
        self,
        role: str,
        fixture_id: str,
        false_positive: bool,
        detail: str = "",
        *,
        error: bool = False,
    ) -> None:
        """Record one RUN of a NEGATIVE fixture (R5.2)."""
        self.fp_records.append({
            "role": role,
            "fixture_id": fixture_id,
            "false_positive": false_positive,
            "error": error,
            "detail": detail,
        })

    def save(self) -> Path | None:
        if not self.records and not self.fp_records:
            return None
        date_str = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        if self.records:
            report_data: dict = calibration.build_report(
                self.records,
                model=REPORT_MODEL_LABEL,
                timestamp=date_str,
                runs_per_fixture=CALIBRATION_RUNS,
            )
        else:
            report_data = {
                "format_version": calibration.REPORT_FORMAT_VERSION,
                "model": REPORT_MODEL_LABEL,
                "timestamp": date_str,
                "runs_per_fixture": CALIBRATION_RUNS,
                "summary": {},
                "fixtures": [],
            }
        # R5.2: inject the false-positive analysis test-side. Negative
        # fixtures are an R5.2 addition and kstrl.calibration owns only
        # the detection format (out of this session's scope), so the FP
        # block is layered on the returned dict, not baked into build_report.
        if self.fp_records:
            report_data = dict(report_data)
            report_data["false_positive_analysis"] = build_fp_summary(
                self.fp_records,
            )
        return calibration.save_report(report_data, RESULTS_DIR)


@pytest.fixture(scope="module")
def report() -> Iterator[_DetectionReport]:
    r = _DetectionReport()
    yield r
    saved = r.save()
    if saved is not None:
        print(f"\nCalibration report saved to {saved}")


# ---------------------------------------------------------------------------
# N-run threshold gate (R5.1)
#
# Each fixture test runs the role CALIBRATION_RUNS times and gates on
# consistency (fraction of completed runs that caught the planted
# issue) instead of hard-asserting a single run. Agent-infrastructure
# errors (the CLI is unavailable, the process dies) are excluded from
# the denominator; unparseable model output is a completed miss - a
# role that emits garbage failed to behave, which is exactly what
# calibration measures.
# ---------------------------------------------------------------------------


class _AgentUnavailable(Exception):
    """The agent could not run at all (infrastructure, not behavior)."""


def _gate_on_consistency(
    role: str,
    fixture_id: str,
    report: _DetectionReport,
    run_once: Callable[[], tuple[bool, str]],
    *,
    category: str | None = None,
    cwe: str | None = None,
) -> None:
    """Run ``run_once`` CALIBRATION_RUNS times, record every run, then
    assert the fixture's consistency meets the codified threshold."""
    detected = 0
    errored = 0
    details: list[str] = []
    for run_index in range(CALIBRATION_RUNS):
        try:
            caught, detail = run_once()
            error = False
        except _AgentUnavailable as exc:
            caught, detail, error = False, f"agent error: {exc}", True
            errored += 1
        if caught:
            detected += 1
        details.append(f"run {run_index + 1}: caught={caught} {detail}")
        report.record(
            role, fixture_id, caught, detail,
            category=category, cwe=cwe, error=error,
        )
    completed = CALIBRATION_RUNS - errored
    if completed == 0:
        pytest.skip(f"agent unavailable for all {CALIBRATION_RUNS} runs")
    observed = calibration.consistency(detected, completed)
    assert observed >= calibration.FIXTURE_DETECTION_THRESHOLD, (
        f"{role} missed planted issue {fixture_id} in most runs: "
        f"consistency {detected}/{completed} = {observed:.2f} < "
        f"{calibration.FIXTURE_DETECTION_THRESHOLD}\n" + "\n".join(details)
    )


def _measure_detection(
    role: str,
    fixture_id: str,
    report: _DetectionReport,
    run_once: Callable[[], tuple[bool, str]],
    *,
    category: str | None = None,
    cwe: str | None = None,
) -> None:
    """Run ``run_once`` CALIBRATION_RUNS times and RECORD each run WITHOUT
    gating (R5.2 hard positives).

    Unlike ``_gate_on_consistency``, a miss is not a failure: the hard
    positives are designed to be missable, so a miss is the hardness
    signal we want to measure. The per-role ``detection_rate`` in the
    report is the acceptance metric (see the hard-positive test
    docstring). Skips only when every run errored (infrastructure)."""
    errored = 0
    for _ in range(CALIBRATION_RUNS):
        try:
            caught, detail = run_once()
            error = False
        except _AgentUnavailable as exc:
            caught, detail, error = False, f"agent error: {exc}", True
            errored += 1
        report.record(
            role, fixture_id, caught, detail,
            category=category, cwe=cwe, error=error,
        )
    if errored == CALIBRATION_RUNS:
        pytest.skip(f"agent unavailable for all {CALIBRATION_RUNS} runs")


def _measure_false_positives(
    role: str,
    fixture_id: str,
    report: _DetectionReport,
    run_once: Callable[[], tuple[bool, str]],
) -> None:
    """Run ``run_once`` CALIBRATION_RUNS times and record whether a
    forbidden category was flagged (R5.2 negatives).

    Measurement only, no gate: a single false positive is data for the
    aggregate ``fp_rate`` (in the report's ``false_positive_analysis``
    block), not a failure of this harness. Skips only when every run
    errored."""
    errored = 0
    for _ in range(CALIBRATION_RUNS):
        try:
            is_fp, detail = run_once()
            error = False
        except _AgentUnavailable as exc:
            is_fp, detail, error = False, f"agent error: {exc}", True
            errored += 1
        report.record_fp(role, fixture_id, is_fp, detail, error=error)
    if errored == CALIBRATION_RUNS:
        pytest.skip(f"agent unavailable for all {CALIBRATION_RUNS} runs")


# ---------------------------------------------------------------------------
# Security role calibration
# ---------------------------------------------------------------------------


def _security_run_once(
    meta: dict, diff_content: str, tmp_path: Path,
) -> SecurityResult:
    """One security-reviewer run over a fixture diff with its real PRD."""
    prd_text = meta.get("prd", "(synthetic fixture; PRD intentionally omitted)")
    # R5.3: build through the harness path so calibration exercises the
    # per-run data delimiters exactly as production does.
    prompt = _build_security_prompt(prd_text, diff_content)
    # R7.1: security is a reviewer role - it honors the family override.
    agent = _get_reviewer_calibration_agent()
    try:
        output = _collect(agent, prompt, tmp_path)
    except Exception as exc:  # noqa: BLE001
        raise _AgentUnavailable(str(exc)) from exc
    raw = _select_agent_output(agent, output)
    return parse_security_output(raw, SecurityMode.ADVISORY.value)


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _security_positive_easy_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_security_role_catches_planted_bug(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    """Catastrophe detector: the textbook-trivial bugs (sec-01..05) must
    be caught in a majority of runs. A miss is a real regression."""
    diff_content = artifact.read_text(encoding="utf-8")

    def run_once() -> tuple[bool, str]:
        result = _security_run_once(meta, diff_content, tmp_path)
        return security_caught(result, meta["must_detect"])

    _gate_on_consistency(
        "security", meta["fixture_id"], report, run_once,
        category=meta["must_detect"].get("category"), cwe=meta.get("cwe"),
    )


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _security_positive_hard_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_security_role_hard_positive(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    """Measure detection on the R5.2 HARD positives (multi-hop authz,
    second-order injection, TOCTOU, timing oracle).

    These are DESIGNED to be missable, so unlike the catastrophe
    detectors they are recorded under role ``security_hard`` WITHOUT the
    consistency gate: a miss is the hardness signal, not a regression.
    The acceptance check (PR body) reads
    ``summary.security_hard.detection_rate`` from the saved baseline: if
    it is 1.0 on the first baseline run, the fixtures are too easy and
    need another iteration."""
    diff_content = artifact.read_text(encoding="utf-8")

    def run_once() -> tuple[bool, str]:
        result = _security_run_once(meta, diff_content, tmp_path)
        return security_caught(result, meta["must_detect"])

    _measure_detection(
        "security_hard", meta["fixture_id"], report, run_once,
        category=meta["must_detect"].get("category"), cwe=meta.get("cwe"),
    )


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _security_negative_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_security_role_no_false_positive(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    """Measure the security reviewer's false-positive rate on clean-but-
    nontrivial diffs (R5.2). Records whether a forbidden category is
    flagged at/above the floor; the aggregate ``fp_rate`` in the report's
    ``false_positive_analysis`` block is the signal, not this test."""
    diff_content = artifact.read_text(encoding="utf-8")

    def run_once() -> tuple[bool, str]:
        result = _security_run_once(meta, diff_content, tmp_path)
        return security_false_positive(result, meta["must_not_flag"])

    _measure_false_positives(
        "security_negative", meta["fixture_id"], report, run_once,
    )


# ---------------------------------------------------------------------------
# Reviewer role calibration
# ---------------------------------------------------------------------------


def _reviewer_run_once(
    meta: dict, diff_content: str, tmp_path: Path,
) -> ReviewResult:
    """One reviewer run over a fixture diff with a real PRD + production-
    shaped verification context (R5.2)."""
    prd_text = meta.get("prd", "(no PRD)")
    # R5.3: build_review_prompt needs a PRD file on disk, so calibration
    # formats the template directly but with a real per-run delimiter.
    prompt = REVIEWER_PROMPT.format(
        prd_content=prd_text,
        diff_content=diff_content,
        verification_summary=render_verification(meta),
        data_delimiter=generate_data_delimiter(),
    )
    # R7.1: the reviewer role honors the family override.
    agent = _get_reviewer_calibration_agent()
    try:
        output = _collect(agent, prompt, tmp_path)
    except Exception as exc:  # noqa: BLE001
        raise _AgentUnavailable(str(exc)) from exc
    raw = _select_agent_output(agent, output)
    return parse_review_output(raw)


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _concern_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_reviewer_role_catches_planted_concern(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    diff_content = artifact.read_text(encoding="utf-8")

    def run_once() -> tuple[bool, str]:
        result = _reviewer_run_once(meta, diff_content, tmp_path)
        return reviewer_caught(result, meta["must_detect"])

    _gate_on_consistency(
        "reviewer", meta["fixture_id"], report, run_once,
        category=meta["must_detect"].get("category"),
    )


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _concern_negative_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_reviewer_role_no_false_positive(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    """Measure the reviewer's false-positive rate on clean-but-nontrivial
    diffs (R5.2): used-helper refactor, thorough tests, correct error
    handling, intentional public API. Records whether a forbidden
    category is raised as a blocking concern; the aggregate ``fp_rate``
    in the report is the signal, not this test."""
    diff_content = artifact.read_text(encoding="utf-8")

    def run_once() -> tuple[bool, str]:
        result = _reviewer_run_once(meta, diff_content, tmp_path)
        return reviewer_false_positive(result, meta["must_not_flag"])

    _measure_false_positives(
        "reviewer_negative", meta["fixture_id"], report, run_once,
    )


# ---------------------------------------------------------------------------
# Architect (PRD red-team) role calibration
# ---------------------------------------------------------------------------


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _halting_spec_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_architect_role_flags_vague_spec(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    spec_content = artifact.read_text(encoding="utf-8")
    prompt = build_decompose_prompt(meta["fixture_id"], spec_content)

    def run_once() -> tuple[bool, str]:
        agent = _get_calibration_agent()
        try:
            output = _collect(agent, prompt, tmp_path)
        except Exception as exc:  # noqa: BLE001
            raise _AgentUnavailable(str(exc)) from exc
        try:
            data = _extract_agent_json(agent, output)
        except ValueError as exc:
            # Unparseable output is a completed behavioral miss, not
            # an infrastructure error.
            return False, f"json parse: {exc}"
        issues = _parse_spec_issues(data)
        return architect_caught(issues, meta["must_detect"])

    _gate_on_consistency(
        "architect", meta["fixture_id"], report, run_once,
        category="spec_issues",
    )


# ---------------------------------------------------------------------------
# Architect (PRD red-team) -- allowedPaths emission quality
# ---------------------------------------------------------------------------


def _allowed_paths_fixtures() -> list[tuple[Path, dict]]:
    """Return spec fixtures whose meta has ``must_emit_allowed_paths``.

    These are non-halting fixtures: specs clear enough that the
    architect should decompose them, used to grade whether the
    emitted ``allowedPaths`` are sensible per DECOMPOSE_PROMPT
    v1.2.0 rule #12.
    """
    return [
        (artifact, meta) for artifact, meta in _spec_fixtures()
        if "must_emit_allowed_paths" in meta
    ]


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _allowed_paths_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_architect_emits_sensible_allowed_paths(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    """Grade architect output on the allowedPaths emission rule.

    This is the calibration counterpart to ``decompose._validate_decompose_output``
    -- the validator gates the *shape* (presence + non-emptyness +
    types), but only a real LLM call can verify that the architect
    emits *sensible* scopes (includes test root, excludes harness
    internals, names the feature subtree). Without a non-halting
    fixture the v1.2.0 prompt's rule #12 is unmeasured.
    """
    spec_content = artifact.read_text(encoding="utf-8")
    prompt = build_decompose_prompt(meta["fixture_id"], spec_content)

    def run_once() -> tuple[bool, str]:
        agent = _get_calibration_agent()
        try:
            output = _collect(agent, prompt, tmp_path)
        except Exception as exc:  # noqa: BLE001
            raise _AgentUnavailable(str(exc)) from exc
        try:
            data = _extract_agent_json(agent, output)
        except ValueError as exc:
            return False, f"json parse: {exc}"
        return architect_allowed_paths_caught(
            data, meta["must_emit_allowed_paths"],
        )

    _gate_on_consistency(
        "architect_allowed_paths", meta["fixture_id"], report, run_once,
        category="allowed_paths",
    )


# ---------------------------------------------------------------------------
# Static sanity (always runs, even without calibration env var)
# ---------------------------------------------------------------------------


class TestFixtureStructure:
    """These run unconditionally - cheap structural checks on the fixture
    library itself. If a fixture file lacks its metadata partner, this
    fails early instead of mid-calibration."""

    @pytest.mark.parametrize(
        "subdir,suffix",
        [
            ("security", ".diff"),
            ("security_negative", ".diff"),
            ("concerns", ".diff"),
            ("concerns_negative", ".diff"),
            ("specs", ".md"),
        ],
    )
    def test_every_fixture_has_metadata(
        self, subdir: str, suffix: str,
    ) -> None:
        # Remove the module-level skip for this static check
        for artifact in (FIXTURES_DIR / subdir).glob(f"*{suffix}"):
            meta_path = artifact.with_suffix(".meta.json")
            if not meta_path.exists():
                meta_path = artifact.parent / f"{artifact.stem}.meta.json"
            assert meta_path.exists(), (
                f"Fixture {artifact} has no .meta.json partner"
            )

    def test_security_fixtures_count(self) -> None:
        # 5 planted-vuln + 1 R5.3 injection-efficacy + 4 R5.2 hard positives
        fixtures = list((FIXTURES_DIR / "security").glob("*.diff"))
        assert len(fixtures) == 10, "Expected 10 security fixtures"

    def test_security_hard_positive_count(self) -> None:
        assert len(_security_positive_hard_fixtures()) == 4, (
            "R5.2 requires at least 4 hard security positives"
        )

    def test_required_hard_scenarios_present(self) -> None:
        """The four R5.2 hard scenarios must each be present."""
        ids = {m["fixture_id"] for _a, m in _security_positive_hard_fixtures()}
        for required in (
            "sec-06-multihop-authz",
            "sec-07-second-order-injection",
            "sec-08-toctou-race",
            "sec-09-timing-oracle",
        ):
            assert required in ids, f"missing required hard fixture {required}"

    def test_security_negative_count(self) -> None:
        assert len(_security_negative_fixtures()) >= 3, (
            "R5.2 requires >= 3 security negative fixtures"
        )

    def test_reviewer_negative_count(self) -> None:
        assert len(_concern_negative_fixtures()) >= 3, (
            "R5.2 requires >= 3 reviewer negative fixtures"
        )

    def test_concern_fixtures_count(self) -> None:
        # 3 planted-concern fixtures + 1 R5.3 injection-efficacy fixture
        fixtures = list((FIXTURES_DIR / "concerns").glob("*.diff"))
        assert len(fixtures) == 4, "Expected 4 concern fixtures"

    def test_spec_fixtures_count(self) -> None:
        fixtures = list((FIXTURES_DIR / "specs").glob("*.md"))
        # 3 original halting fixtures + 1 non-halting allowedPaths fixture
        assert len(fixtures) == 4, "Expected 4 spec fixtures"

    def test_security_meta_has_required_keys(self) -> None:
        for artifact, meta in _security_fixtures():
            assert "fixture_id" in meta
            assert "prd" in meta, f"{artifact} security fixture needs a PRD"
            assert "must_detect" in meta
            md = meta["must_detect"]
            assert "severity_at_least" in md
            for cat in _acceptable_categories(md):
                assert cat in VALID_CATEGORIES, (
                    f"{meta['fixture_id']} category {cat!r} not in taxonomy"
                )

    def test_concern_meta_has_required_keys(self) -> None:
        for _artifact, meta in _concern_fixtures():
            assert "fixture_id" in meta
            assert "must_detect" in meta
            assert "prd" in meta, "Reviewer fixtures need a PRD context"
            for cat in _acceptable_categories(meta["must_detect"]):
                assert cat in VALID_CONCERN_CATEGORIES, (
                    f"{meta['fixture_id']} category {cat!r} not in taxonomy"
                )

    def test_negative_meta_has_required_keys(self) -> None:
        cases = [
            (_security_negative_fixtures(), VALID_CATEGORIES),
            (_concern_negative_fixtures(), VALID_CONCERN_CATEGORIES),
        ]
        for fixtures, taxonomy in cases:
            for _artifact, meta in fixtures:
                assert "fixture_id" in meta
                assert meta.get("kind") == "negative"
                assert "prd" in meta
                assert "must_not_flag" in meta, (
                    f"{meta['fixture_id']} negative fixture needs must_not_flag"
                )
                mnf = meta["must_not_flag"]
                assert mnf.get("categories"), "must_not_flag needs categories"
                assert "severity_at_least" in mnf
                for cat in mnf["categories"]:
                    assert cat in taxonomy, (
                        f"{meta['fixture_id']} forbidden category {cat!r} "
                        "not in taxonomy"
                    )

    def test_verification_renders_for_every_fixture(self) -> None:
        """Every fixture renders to the production ``- name: STATUS -
        message`` verification shape via render_verification (R5.2)."""
        all_fixtures = (
            _security_fixtures()
            + _security_negative_fixtures()
            + _concern_fixtures()
            + _concern_negative_fixtures()
        )
        for _artifact, meta in all_fixtures:
            rendered = render_verification(meta)
            assert rendered, f"{meta['fixture_id']} rendered empty verification"
            for line in rendered.splitlines():
                assert line.startswith("- ")
                assert ": PASS - " in line or ": FAIL - " in line

    def test_spec_meta_has_required_keys(self) -> None:
        for _artifact, meta in _spec_fixtures():
            assert "fixture_id" in meta
            # A spec fixture must carry exactly one grading schema:
            # either ``must_detect`` (halting fixtures that grade
            # spec-issue detection) or ``must_emit_allowed_paths``
            # (non-halting fixtures that grade allowedPaths quality).
            assert "must_detect" in meta or "must_emit_allowed_paths" in meta, (
                f"{meta['fixture_id']} has neither must_detect nor "
                "must_emit_allowed_paths grading"
            )
            if "must_detect" in meta:
                assert "spec_issues_min" in meta["must_detect"]
            if "must_emit_allowed_paths" in meta:
                req = meta["must_emit_allowed_paths"]
                # Required keys for the new schema.
                assert "non_halting" in req
                assert "every_component_has_allowed_paths" in req

    def test_warns_when_calibration_model_differs_from_newest_baseline(
        self,
    ) -> None:
        """R5.5 / H2-extended: calibration re-runs on model change, not
        just prompt change. This structural check WARNS (never fails)
        when the configured calibration model differs from the model
        recorded in the newest baseline, because every recorded
        detection rate was measured against that older model and does
        not transfer. Compares the full R7.1 label (base model plus any
        reviewer-family override) so a standing override does not warn
        against its own baselines - and a dropped override does."""
        message = calibration.model_drift_message(
            RESULTS_DIR, REPORT_MODEL_LABEL,
        )
        if message is not None:
            warnings.warn(
                message,
                calibration.CalibrationModelDriftWarning,
                stacklevel=1,
            )


class TestMatchersResolveOnFixtures:
    """Prove that every fixture's grading requirement is internally
    consistent with the matcher that grades it: a synthetic finding built
    to satisfy the meta is accepted, and an empty result is rejected. This
    runs without a live model, so a typo in a fixture's category/severity/
    path is caught structurally instead of silently scoring every run as a
    miss (the exact class of bug the F5 baseline surfaced)."""

    def _floor_severity(self, requirement: dict, default: str) -> str:
        floor = requirement.get("severity_at_least", default)
        return "high" if floor == "fail" else str(floor)

    @pytest.mark.parametrize(
        "artifact,meta",
        _security_fixtures(),
        ids=lambda x: x.get("fixture_id", "?") if isinstance(x, dict) else x.stem,
    )
    def test_security_positive_matcher_resolves(
        self, artifact: Path, meta: dict,
    ) -> None:
        md = meta["must_detect"]
        category = sorted(_acceptable_categories(md))[0]
        location = md.get("evidence_path_contains", "src/x.py")
        finding = SecurityFinding(
            category=category,
            severity=self._floor_severity(md, "high"),
            location=location,
            explanation="synthetic",
        )
        result = SecurityResult(
            passed=False, mode=SecurityMode.HARD.value, findings=[finding],
        )
        caught, _ = security_caught(result, md)
        assert caught, f"{meta['fixture_id']} matcher rejects a valid finding"

        empty = SecurityResult(
            passed=True, mode=SecurityMode.HARD.value, findings=[],
        )
        assert not security_caught(empty, md)[0]

    @pytest.mark.parametrize(
        "artifact,meta",
        _security_negative_fixtures(),
        ids=lambda x: x.get("fixture_id", "?") if isinstance(x, dict) else x.stem,
    )
    def test_security_negative_matcher_resolves(
        self, artifact: Path, meta: dict,
    ) -> None:
        mnf = meta["must_not_flag"]
        forbidden = SecurityFinding(
            category=str(mnf["categories"][0]),
            severity=str(mnf.get("severity_at_least", "high")),
            location="src/x.py",
            explanation="synthetic",
        )
        flagged = SecurityResult(
            passed=False, mode=SecurityMode.HARD.value, findings=[forbidden],
        )
        assert security_false_positive(flagged, mnf)[0], (
            f"{meta['fixture_id']} FP matcher misses a forbidden finding"
        )

        clean = SecurityResult(
            passed=True, mode=SecurityMode.HARD.value, findings=[],
        )
        assert not security_false_positive(clean, mnf)[0]

    @pytest.mark.parametrize(
        "artifact,meta",
        _concern_fixtures(),
        ids=lambda x: x.get("fixture_id", "?") if isinstance(x, dict) else x.stem,
    )
    def test_reviewer_positive_matcher_resolves(
        self, artifact: Path, meta: dict,
    ) -> None:
        md = meta["must_detect"]
        category = sorted(_acceptable_categories(md))[0]
        severity = "fail" if md.get("severity_at_least") == "fail" else "advisory"
        concern = ReviewConcern(
            category=category,
            severity=severity,
            location=md.get("evidence_path_contains", "src/x.py"),
            explanation="synthetic",
        )
        caught, _ = reviewer_caught(self._review_with(concern), md)
        assert caught, f"{meta['fixture_id']} matcher rejects a valid concern"
        assert not reviewer_caught(self._review_with(), md)[0]

    @pytest.mark.parametrize(
        "artifact,meta",
        _concern_negative_fixtures(),
        ids=lambda x: x.get("fixture_id", "?") if isinstance(x, dict) else x.stem,
    )
    def test_reviewer_negative_matcher_resolves(
        self, artifact: Path, meta: dict,
    ) -> None:
        mnf = meta["must_not_flag"]
        severity = "fail" if mnf.get("severity_at_least") == "fail" else "advisory"
        concern = ReviewConcern(
            category=str(mnf["categories"][0]),
            severity=severity,
            location="src/x.py",
            explanation="synthetic",
        )
        assert reviewer_false_positive(self._review_with(concern), mnf)[0], (
            f"{meta['fixture_id']} FP matcher misses a forbidden concern"
        )
        assert not reviewer_false_positive(self._review_with(), mnf)[0]

    @staticmethod
    def _review_with(*concerns: ReviewConcern) -> ReviewResult:
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
