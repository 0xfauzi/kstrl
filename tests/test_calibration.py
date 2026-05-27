"""Calibration runner: measures whether each adversarial role catches
its planted bugs.

This test suite is OPT-IN: it makes real LLM calls and is therefore
gated behind ``RALPH_RUN_CALIBRATION=1``. Without that env var the
tests are skipped so default CI (and local `uv run pytest`) doesn't
burn API tokens.

When run, the suite iterates over fixtures in
``tests/adversarial_fixtures/{security,concerns,specs}/``, feeds each
fixture's diff or spec to the corresponding role prompt against a
fast model (Haiku-class), and asserts the planted issue is caught.
Per-role detection rates are written to
``tests/adversarial_fixtures/_results/baseline-<UTC-date>.json`` so
future prompt edits can be compared against the baseline.

Why this matters: the entire adversarial-roles design relies on the
LLM actually behaving adversarially under the framing. Without
measurement we have no idea whether the prompts catch real bugs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ralph_py.decompose import (
    DECOMPOSE_PROMPT,
    _extract_agent_json,
    _parse_spec_issues,
    _select_agent_output,
)
from ralph_py.review import REVIEWER_PROMPT, parse_review_output
from ralph_py.security import (
    SECURITY_PROMPT,
    SecurityMode,
    parse_security_output,
)


CALIBRATION_ENABLED = os.environ.get("RALPH_RUN_CALIBRATION") == "1"
CALIBRATION_MODEL = os.environ.get("RALPH_CALIBRATION_MODEL", "haiku")

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
# Detection rate report
# ---------------------------------------------------------------------------


class _DetectionReport:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def record(
        self, role: str, fixture_id: str, caught: bool, detail: str = "",
    ) -> None:
        self.results.append({
            "role": role,
            "fixture_id": fixture_id,
            "caught": caught,
            "detail": detail,
        })

    def save(self) -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"baseline-{date_str}.json"
        summary: dict[str, dict] = {}
        for entry in self.results:
            role = entry["role"]
            s = summary.setdefault(role, {"total": 0, "caught": 0})
            s["total"] += 1
            if entry["caught"]:
                s["caught"] += 1
        for role, counts in summary.items():
            counts["detection_rate"] = (
                counts["caught"] / counts["total"] if counts["total"] else 0.0
            )
        out.write_text(json.dumps({
            "model": CALIBRATION_MODEL,
            "timestamp": date_str,
            "summary": summary,
            "fixtures": self.results,
        }, indent=2))
        return out


@pytest.fixture(scope="module")
def report() -> Iterator[_DetectionReport]:
    r = _DetectionReport()
    yield r
    saved = r.save()
    print(f"\nCalibration report saved to {saved}")


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

    agent = _get_calibration_agent()
    output: list[str] = []
    try:
        output = _collect(agent, prompt, tmp_path)
    except Exception as exc:  # noqa: BLE001
        report.record("security", meta["fixture_id"], False, f"agent error: {exc}")
        pytest.skip(f"agent unavailable: {exc}")

    raw = _select_agent_output(agent, output)
    result = parse_security_output(raw, SecurityMode.ADVISORY.value)

    requirement = meta["must_detect"]
    caught = False
    detail = ""
    for finding in result.findings:
        if finding.category != requirement["category"]:
            continue
        if not _meets_severity(finding.severity, requirement["severity_at_least"]):
            continue
        if requirement.get("evidence_path_contains"):
            if not any(
                requirement["evidence_path_contains"] in ev
                for ev in finding.evidence
            ):
                continue
        caught = True
        detail = f"{finding.severity} {finding.category} at {finding.location}"
        break

    report.record("security", meta["fixture_id"], caught, detail)
    assert caught, (
        f"Security role missed planted bug {meta['fixture_id']}: "
        f"expected category={requirement['category']}, "
        f"got findings={[f.category for f in result.findings]}"
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

    agent = _get_calibration_agent()
    output: list[str] = []
    try:
        output = _collect(agent, prompt, tmp_path)
    except Exception as exc:  # noqa: BLE001
        report.record("reviewer", meta["fixture_id"], False, f"agent error: {exc}")
        pytest.skip(f"agent unavailable: {exc}")

    raw = _select_agent_output(agent, output)
    result = parse_review_output(raw)

    requirement = meta["must_detect"]
    caught = False
    detail = ""
    for concern in result.concerns:
        if concern.category != requirement["category"]:
            continue
        if requirement.get("severity_at_least") == "fail":
            if concern.severity != "fail":
                continue
        if requirement.get("evidence_path_contains"):
            if requirement["evidence_path_contains"] not in concern.location:
                continue
        caught = True
        detail = f"{concern.severity} {concern.category} at {concern.location}"
        break

    report.record("reviewer", meta["fixture_id"], caught, detail)
    assert caught, (
        f"Reviewer missed planted concern {meta['fixture_id']}: "
        f"expected category={requirement['category']}, "
        f"got concerns={[(c.category, c.severity) for c in result.concerns]}"
    )


# ---------------------------------------------------------------------------
# Architect (PRD red-team) role calibration
# ---------------------------------------------------------------------------


@_skip_unless_calibrating
@pytest.mark.parametrize("artifact,meta", _spec_fixtures(),
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

    agent = _get_calibration_agent()
    output: list[str] = []
    try:
        output = _collect(agent, prompt, tmp_path)
    except Exception as exc:  # noqa: BLE001
        report.record("architect", meta["fixture_id"], False, f"agent error: {exc}")
        pytest.skip(f"agent unavailable: {exc}")

    try:
        data = _extract_agent_json(agent, output)
    except ValueError as exc:
        report.record("architect", meta["fixture_id"], False, f"json parse: {exc}")
        pytest.fail(f"Architect output did not parse for {meta['fixture_id']}: {exc}")
        return  # unreachable but satisfies the type-checker

    issues = _parse_spec_issues(data)

    requirement = meta["must_detect"]
    min_count = requirement.get("spec_issues_min", 1)
    required_kinds = set(requirement.get("must_include_kind", []))
    must_be_major_or_blocker = requirement.get("blocker_or_major", False)

    caught = (
        len(issues) >= min_count
        and (not required_kinds or required_kinds.issubset({i.kind for i in issues}))
        and (
            not must_be_major_or_blocker
            or any(i.severity in {"blocker", "major"} for i in issues)
        )
    )

    detail = f"got {len(issues)} issues: {[(i.severity, i.kind) for i in issues]}"
    report.record("architect", meta["fixture_id"], caught, detail)
    assert caught, (
        f"Architect missed planted vagueness in {meta['fixture_id']}: {detail}"
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
        assert len(fixtures) == 3, "Expected 3 spec fixtures"

    def test_security_meta_has_required_keys(self) -> None:
        for artifact, meta in _security_fixtures():
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
        for artifact, meta in _concern_fixtures():
            assert "fixture_id" in meta
            assert "must_detect" in meta
            assert "category" in meta["must_detect"]
            assert "prd" in meta, "Reviewer fixtures need a PRD context"

    def test_spec_meta_has_required_keys(self) -> None:
        for artifact, meta in _spec_fixtures():
            assert "fixture_id" in meta
            assert "must_detect" in meta
            assert "spec_issues_min" in meta["must_detect"]


