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
the v2 format defined by :mod:`ralph_py.calibration`; compare against
a previous baseline with
``python -m ralph_py.calibration compare <old.json> <new.json>``.

Why this matters: the entire adversarial-roles design relies on the
LLM actually behaving adversarially under the framing. Without
measurement we have no idea whether the prompts catch real bugs.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ralph_py import calibration
from ralph_py.decompose import (
    DECOMPOSE_PROMPT,
    SpecIssue,
    _extract_agent_json,
    _parse_spec_issues,
    _select_agent_output,
)
from ralph_py.review import REVIEWER_PROMPT, ReviewResult, parse_review_output
from ralph_py.security import (
    SECURITY_PROMPT,
    SecurityMode,
    SecurityResult,
    parse_security_output,
)

CALIBRATION_ENABLED = os.environ.get("RALPH_RUN_CALIBRATION") == "1"
CALIBRATION_MODEL = os.environ.get("RALPH_CALIBRATION_MODEL", "haiku")
CALIBRATION_RUNS = int(
    os.environ.get(
        "RALPH_CALIBRATION_RUNS", str(calibration.DEFAULT_CALIBRATION_RUNS),
    )
)

FIXTURES_DIR = Path(__file__).parent / "adversarial_fixtures"
RESULTS_DIR = FIXTURES_DIR / "_results"

# Approximate verification stub: callers pass a placeholder pass/fail
# string into REVIEWER_PROMPT's `{verification_summary}` field.
_VERIFICATION_STUB = "- tests: PASS - 0 failures\n- typecheck: PASS\n- lint: PASS"


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


def _concern_fixtures() -> list[tuple[Path, dict]]:
    return _load_fixtures("concerns", ".diff")


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
    from ralph_py.agents import get_agent
    return get_agent(
        agent_cmd=None,
        model=CALIBRATION_MODEL,
        model_reasoning_effort=None,
        agent_type="claude-code",
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


def security_caught(
    result: SecurityResult, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the security matcher.

    A finding counts as catching the planted bug when its category
    matches, severity meets the threshold, and (optionally) location
    contains the expected path substring. First match wins; subsequent
    findings are ignored even if more severe -- this mirrors how the
    review path would gate (one matching finding is enough).
    """
    for finding in result.findings:
        if finding.category != requirement["category"]:
            continue
        if not _meets_severity(finding.severity, requirement["severity_at_least"]):
            continue
        if requirement.get("evidence_path_contains"):
            if requirement["evidence_path_contains"] not in finding.location:
                continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def reviewer_caught(
    result: ReviewResult, requirement: dict,
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the reviewer matcher.

    A concern counts as catching the planted issue when its category
    matches, and (optionally) severity is ``"fail"`` when the fixture
    demands ``severity_at_least == "fail"``, and (optionally) location
    contains the expected path substring.
    """
    for concern in result.concerns:
        if concern.category != requirement["category"]:
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
# Detection report (v2 format, built by ralph_py.calibration)
# ---------------------------------------------------------------------------


class _DetectionReport:
    """Accumulates one record per RUN (not per fixture) and delegates
    aggregation + the on-disk format to :mod:`ralph_py.calibration`."""

    def __init__(self) -> None:
        self.records: list[dict] = []

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

    def save(self) -> Path | None:
        if not self.records:
            return None
        date_str = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        report_data = calibration.build_report(
            self.records,
            model=CALIBRATION_MODEL,
            timestamp=date_str,
            runs_per_fixture=CALIBRATION_RUNS,
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


# ---------------------------------------------------------------------------
# Security role calibration
# ---------------------------------------------------------------------------


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _security_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_security_role_catches_planted_bug(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    diff_content = artifact.read_text(encoding="utf-8")
    prompt = SECURITY_PROMPT.format(
        prd_content="(synthetic fixture; PRD intentionally omitted)",
        diff_content=diff_content,
    )

    def run_once() -> tuple[bool, str]:
        agent = _get_calibration_agent()
        try:
            output = _collect(agent, prompt, tmp_path)
        except Exception as exc:  # noqa: BLE001
            raise _AgentUnavailable(str(exc)) from exc
        raw = _select_agent_output(agent, output)
        result = parse_security_output(raw, SecurityMode.ADVISORY.value)
        return security_caught(result, meta["must_detect"])

    _gate_on_consistency(
        "security", meta["fixture_id"], report, run_once,
        category=meta["must_detect"]["category"], cwe=meta.get("cwe"),
    )


# ---------------------------------------------------------------------------
# Reviewer role calibration
# ---------------------------------------------------------------------------


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _concern_fixtures(),
                         ids=lambda x: x.get("fixture_id", "unknown")
                         if isinstance(x, dict) else x.stem)
def test_reviewer_role_catches_planted_concern(
    artifact: Path, meta: dict, tmp_path: Path, report: _DetectionReport,
) -> None:
    diff_content = artifact.read_text(encoding="utf-8")
    prd_text = meta.get("prd", "(no PRD)")
    prompt = REVIEWER_PROMPT.format(
        prd_content=prd_text,
        diff_content=diff_content,
        verification_summary=_VERIFICATION_STUB,
    )

    def run_once() -> tuple[bool, str]:
        agent = _get_calibration_agent()
        try:
            output = _collect(agent, prompt, tmp_path)
        except Exception as exc:  # noqa: BLE001
            raise _AgentUnavailable(str(exc)) from exc
        raw = _select_agent_output(agent, output)
        result = parse_review_output(raw)
        return reviewer_caught(result, meta["must_detect"])

    _gate_on_consistency(
        "reviewer", meta["fixture_id"], report, run_once,
        category=meta["must_detect"]["category"],
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
    prompt = DECOMPOSE_PROMPT.format(
        project_name=meta["fixture_id"],
        spec_content=spec_content,
    )

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
    prompt = DECOMPOSE_PROMPT.format(
        project_name=meta["fixture_id"],
        spec_content=spec_content,
    )

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
            ("concerns", ".diff"),
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
        fixtures = list((FIXTURES_DIR / "security").glob("*.diff"))
        assert len(fixtures) == 5, "Expected 5 security fixtures"

    def test_concern_fixtures_count(self) -> None:
        fixtures = list((FIXTURES_DIR / "concerns").glob("*.diff"))
        assert len(fixtures) == 3, "Expected 3 concern fixtures"

    def test_spec_fixtures_count(self) -> None:
        fixtures = list((FIXTURES_DIR / "specs").glob("*.md"))
        # 3 original halting fixtures + 1 non-halting allowedPaths fixture
        assert len(fixtures) == 4, "Expected 4 spec fixtures"

    def test_security_meta_has_required_keys(self) -> None:
        for _artifact, meta in _security_fixtures():
            assert "fixture_id" in meta
            assert "must_detect" in meta
            assert "category" in meta["must_detect"]
            assert "severity_at_least" in meta["must_detect"]
            assert meta["must_detect"]["category"] in {
                "injection", "auth_bypass", "authz_bypass",
                "hardcoded_secret", "unsafe_deserialization",
                "broken_crypto", "predictable_randomness",
                "missing_input_validation", "race_condition", "ssrf",
                "xss", "open_redirect", "information_disclosure",
                "denial_of_service", "other",
            }

    def test_concern_meta_has_required_keys(self) -> None:
        for _artifact, meta in _concern_fixtures():
            assert "fixture_id" in meta
            assert "must_detect" in meta
            assert "category" in meta["must_detect"]
            assert "prd" in meta, "Reviewer fixtures need a PRD context"

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
        not transfer."""
        message = calibration.model_drift_message(RESULTS_DIR, CALIBRATION_MODEL)
        if message is not None:
            warnings.warn(
                message,
                calibration.CalibrationModelDriftWarning,
                stacklevel=1,
            )
