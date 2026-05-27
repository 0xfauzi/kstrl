"""E3: tests for the typed Finding dataclass and the as_findings()
hooks on ReviewResult / SecurityResult, plus the manifest roundtrip
that persists Component.findings to disk."""

from __future__ import annotations

import json
from pathlib import Path

from ralph_py.findings import Finding
from ralph_py.manifest import Component, Manifest
from ralph_py.review import (
    CriterionReview,
    ReviewConcern,
    ReviewResult,
    ReviewVerdict,
)
from ralph_py.security import SecurityFinding, SecurityMode, SecurityResult


def _make_component(comp_id: str = "comp-1") -> Component:
    return Component(
        id=comp_id,
        title="t",
        description="d",
        dependencies=[],
        prd_path="prd.json",
        branch_name="ralph/comp-1",
    )


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


class TestFinding:
    def test_round_trip_minimal(self) -> None:
        f = Finding(
            phase="review", category="dead_code", severity="fail",
            location="src/x.py:10", explanation="unused",
        )
        roundtripped = Finding.from_dict(f.to_dict())
        assert roundtripped == f

    def test_round_trip_full(self) -> None:
        f = Finding(
            phase="security", category="injection", severity="critical",
            location="src/db.py:42", explanation="raw sql",
            suggestion="parametrize", owasp="A03:2021-Injection",
            cwe="CWE-89", tags=("sql", "userinput"),
        )
        roundtripped = Finding.from_dict(f.to_dict())
        assert roundtripped == f

    def test_from_dict_tolerates_missing_keys(self) -> None:
        f = Finding.from_dict({"phase": "review", "category": "scope_creep"})
        assert f.phase == "review"
        assert f.category == "scope_creep"
        assert f.severity == ""
        assert f.tags == ()

    def test_from_dict_coerces_non_string_tags(self) -> None:
        # Defensive: a manifest.json corrupted to have a non-list tags
        # should not crash the loader.
        f = Finding.from_dict({"phase": "review", "tags": "not-a-list"})
        assert f.tags == ()

    def test_frozen(self) -> None:
        import dataclasses

        import pytest

        f = Finding(phase="review", category="x", severity="fail",
                    location="", explanation="")
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.severity = "advisory"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ReviewResult.as_findings
# ---------------------------------------------------------------------------


class TestReviewResultAsFindings:
    def test_pass_criteria_excluded(self) -> None:
        result = ReviewResult(
            passed=True, mode="hard",
            criteria=[
                CriterionReview(
                    criterion="A",
                    verdict=ReviewVerdict.PASS.value,
                    explanation="ok", suggestion="",
                ),
            ],
        )
        assert result.as_findings() == []

    def test_fail_and_advisory_criteria_typed(self) -> None:
        result = ReviewResult(
            passed=False, mode="hard",
            criteria=[
                CriterionReview(
                    criterion="must X",
                    verdict=ReviewVerdict.FAIL.value,
                    explanation="missing X", suggestion="add X",
                ),
                CriterionReview(
                    criterion="should Y",
                    verdict=ReviewVerdict.ADVISORY.value,
                    explanation="weak Y", suggestion="",
                ),
            ],
        )
        findings = result.as_findings()
        assert len(findings) == 2
        assert findings[0].phase == "review"
        assert findings[0].category == "prd_criterion"
        assert findings[0].severity == "fail"
        assert "must X" in findings[0].explanation
        assert findings[0].suggestion == "add X"
        assert findings[1].severity == "advisory"

    def test_concerns_typed(self) -> None:
        result = ReviewResult(
            passed=False, mode="hard",
            concerns=[
                ReviewConcern(
                    category="scope_creep", severity="fail",
                    location="src/logger.py",
                    explanation="unrelated logger added",
                    suggestion="remove",
                ),
            ],
        )
        findings = result.as_findings()
        assert len(findings) == 1
        f = findings[0]
        assert f.phase == "review"
        assert f.category == "scope_creep"
        assert f.severity == "fail"
        assert f.location == "src/logger.py"
        assert f.suggestion == "remove"


# ---------------------------------------------------------------------------
# SecurityResult.as_findings
# ---------------------------------------------------------------------------


class TestSecurityResultAsFindings:
    def test_findings_enriched_with_owasp_cwe(self) -> None:
        result = SecurityResult(
            passed=False, mode=SecurityMode.HARD.value,
            findings=[
                SecurityFinding(
                    category="injection", severity="critical",
                    location="src/db.py:7", explanation="raw sql",
                    suggestion="parametrize",
                ),
            ],
        )
        findings = result.as_findings()
        assert len(findings) == 1
        f = findings[0]
        assert f.phase == "security"
        assert f.category == "injection"
        assert f.severity == "critical"
        assert f.location == "src/db.py:7"
        # The Phase D5 SECURITY_CATEGORY_MAP should fill these.
        assert "A03" in f.owasp
        assert f.cwe.startswith("CWE-")

    def test_empty_findings(self) -> None:
        result = SecurityResult(
            passed=True, mode=SecurityMode.ADVISORY.value, findings=[],
        )
        assert result.as_findings() == []

    def test_unknown_category_still_serializes(self) -> None:
        result = SecurityResult(
            passed=False, mode=SecurityMode.HARD.value,
            findings=[
                SecurityFinding(
                    category="some_new_thing", severity="medium",
                    location="x.py", explanation="...", suggestion="",
                ),
            ],
        )
        findings = result.as_findings()
        assert len(findings) == 1
        # No OWASP / CWE entry yet, but the finding still surfaces.
        assert findings[0].category == "some_new_thing"


# ---------------------------------------------------------------------------
# Manifest roundtrip
# ---------------------------------------------------------------------------


class TestManifestFindingsRoundtrip:
    def test_findings_persisted_to_json(self, tmp_path: Path) -> None:
        comp = _make_component()
        comp.findings = [
            Finding(
                phase="review", category="dead_code", severity="fail",
                location="src/a.py:1-5", explanation="unused",
            ),
            Finding(
                phase="security", category="injection", severity="critical",
                location="src/b.py:7", explanation="raw sql",
                owasp="A03:2021-Injection", cwe="CWE-89",
            ),
        ]
        manifest = Manifest(
            version="1", spec_file="", project_name="p",
            base_branch="main", single_pr=False, components=[comp],
        )
        path = tmp_path / "manifest.json"
        manifest.save(path)

        # Sanity-check the on-disk JSON shape.
        raw = json.loads(path.read_text())
        assert raw["components"][0]["findings"][0]["category"] == "dead_code"
        assert raw["components"][0]["findings"][1]["owasp"] == "A03:2021-Injection"

        loaded = Manifest.load(path)
        assert loaded.components[0].findings == comp.findings

    def test_findings_default_empty_on_legacy_manifest(
        self, tmp_path: Path,
    ) -> None:
        """A manifest.json written before this PR has no `findings` key.
        Loading it should produce an empty list, not raise."""
        legacy = {
            "version": "1", "specFile": "", "projectName": "p",
            "baseBranch": "main", "singlePr": False,
            "components": [{
                "id": "c1", "title": "t", "description": "d",
                "dependencies": [], "prdPath": "p.json",
                "branchName": "ralph/c1",
            }],
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(legacy))
        loaded = Manifest.load(path)
        assert loaded.components[0].findings == []

    def test_findings_in_validate_schema_optional(self) -> None:
        legacy = {
            "version": "1", "specFile": "", "projectName": "p",
            "baseBranch": "main", "singlePr": False,
            "components": [{
                "id": "c1", "title": "t", "description": "d",
                "dependencies": [], "prdPath": "p.json",
                "branchName": "ralph/c1",
                "findings": [],
            }],
        }
        assert Manifest.validate_schema(legacy) == []

    def test_malformed_finding_entry_skipped(self, tmp_path: Path) -> None:
        # A non-dict entry in findings (corruption) should be skipped, not
        # crash the load.
        legacy = {
            "version": "1", "specFile": "", "projectName": "p",
            "baseBranch": "main", "singlePr": False,
            "components": [{
                "id": "c1", "title": "t", "description": "d",
                "dependencies": [], "prdPath": "p.json",
                "branchName": "ralph/c1",
                "findings": ["not-a-dict", {"phase": "review", "category": "x"}],
            }],
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(legacy))
        loaded = Manifest.load(path)
        assert len(loaded.components[0].findings) == 1
        assert loaded.components[0].findings[0].category == "x"
