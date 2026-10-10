"""R7.1: cross-model review rotation, through the review runners and
run_factory.

Two surfaces the roadmap item names. The ``model:<id>`` identity tag:
a ``run_review`` or ``run_security_review`` on a real review repo puts
the reviewer's identity on the result and on every Finding it yields,
including the infrastructure finding a crashed or discarded review
leaves behind (#266), and ``component_pr_body`` names it. The
homogeneity warning and the probe (#262): a real ``run_factory`` with a
custom engineer command warns once per enabled reviewer phase and
journals the selection; a run that will never review pays for no probe,
a run that will review pays for one, and an enabled autonomy ladder
keeps the probe because every bundle restores hard review.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import (
    FactoryConfig,
    FactoryResult,
    run_factory,
)
from kstrl.findings import finding_model
from kstrl.manifest import Component, Manifest
from kstrl.observability import read_progress_events
from kstrl.pr_body import component_pr_body
from kstrl.review import (
    ReviewMode,
    run_review,
)
from kstrl.security import (
    SecurityConfig,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify_model import CheckResult, VerificationResult
from tests.conftest import ReviewRepo
from tests.helpers.agent_probe import set_cli_availability, stub_probe
from tests.helpers.stack_confirmation import in_process_stack


class MockAgent:
    """Predetermined-output agent with a real ``name`` identity."""

    def __init__(self, output: str, name: str = "codex (gpt-5)"):
        self._output = output
        self._name = name
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return self._name

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


class CrashingAgent(MockAgent):
    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        raise RuntimeError("agent exploded")
        yield ""  # pragma: no cover


# #266: a canned reviewer reply must carry the repo's REAL diffstat, or
# the coverage check discards the verdict and ``as_findings()`` leads
# with an infrastructure finding - so a test about CONCERN tagging would
# assert against a finding that is not a concern. ``ReviewRepo`` builds
# the matching envelope; only the payload under test is spelled out here.

_REVIEW_STORIES: list[object] = [
    {
        "storyId": "US-001",
        "storyTitle": "Test",
        "criteria": [
            {
                "criterion": "AC1",
                "verdict": "fail",
                "explanation": "not implemented",
                "suggestion": "implement it",
            }
        ],
    }
]

_REVIEW_CONCERNS: list[object] = [
    {
        "category": "dead_code",
        "severity": "advisory",
        "location": "a.py:1",
        "explanation": "unused helper",
        "suggestion": "remove",
    }
]

_SECURITY_FINDINGS: list[object] = [
    {
        "category": "hardcoded_secret",
        "severity": "high",
        "location": "b.py:3",
        "explanation": "API key in source",
        "suggestion": "move to env",
    }
]


def _write_prd(tmp_path: Path) -> Path:
    prd_path = tmp_path / "prd.json"
    prd_path.write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Test",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": True,
                        "notes": "",
                    }
                ],
            }
        )
    )
    return prd_path


class TestModelTagEndToEnd:
    def test_review_findings_carry_model_tag(self, review_repo: ReviewRepo) -> None:
        prd_path = _write_prd(review_repo.path)
        agent = MockAgent(
            review_repo.review_json(
                stories=_REVIEW_STORIES,
                concerns=_REVIEW_CONCERNS,
            )
        )
        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            VerificationResult(
                passed=True,
                checks=[CheckResult("test_suite", True, "ok")],
            ),
            ReviewMode.HARD,
            PlainUI(no_color=True),
        )
        assert result.reviewer_model == "codex (gpt-5)"
        findings = result.as_findings()
        assert findings
        for f in findings:
            assert "model:codex (gpt-5)" in f.tags
            assert finding_model(f) == "codex (gpt-5)"
        # Journal shape: record_run serializes findings via to_dict.
        assert "model:codex (gpt-5)" in findings[0].to_dict()["tags"]
        # PR body names the reviewer model.
        assert "**Reviewer model**: codex (gpt-5)" in result.as_pr_body_section()

    def test_review_crash_still_attributes_reviewer(self, review_repo: ReviewRepo) -> None:
        prd_path = _write_prd(review_repo.path)
        agent = CrashingAgent("", name="codex (gpt-5)")
        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            VerificationResult(passed=True, checks=[]),
            ReviewMode.HARD,
            PlainUI(no_color=True),
        )
        assert result.infrastructure_error is True
        assert result.reviewer_model == "codex (gpt-5)"
        (finding,) = result.as_findings()
        assert finding.is_infrastructure_error
        assert finding_model(finding) == "codex (gpt-5)"

    def test_security_findings_carry_model_tag(self, review_repo: ReviewRepo) -> None:
        agent = MockAgent(
            review_repo.security_json(findings=_SECURITY_FINDINGS),
            name="codex",
        )
        result = run_security_review(
            agent,
            review_repo.path / "missing-prd.json",
            review_repo.path,
            review_repo.base_branch,
            SecurityConfig(mode="advisory"),
            PlainUI(no_color=True),
        )
        assert result.reviewer_model == "codex"
        findings = result.as_findings()
        assert findings
        for f in findings:
            assert finding_model(f) == "codex"
        assert "**Reviewer model**: codex" in result.as_pr_body_section()

    def test_unverified_coverage_still_attributes_reviewer(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """#266 replaced the chunk-merge path this used to guard. The
        property is the same one R7.1 cares about: a result that did NOT
        come back clean must still name the model that produced it, or
        the journal cannot attribute the miss to a family.

        A reviewer reporting a diffstat that is not git's is the new way
        a review can be discarded, so that is the path checked here."""
        prd_path = _write_prd(review_repo.path)
        agent = MockAgent(
            review_repo.review_json(
                observedDiffstat={"files": 99, "insertions": 99, "deletions": 99},
            ),
            name="codex (gpt-5)",
        )
        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            VerificationResult(passed=True, checks=[]),
            ReviewMode.HARD,
            PlainUI(no_color=True),
        )
        assert result.infrastructure_error is True
        assert result.reviewer_model == "codex (gpt-5)"
        findings = result.as_findings()
        # The synthetic infra finding leads, and the reviewer's own
        # concerns follow it rather than being dropped (#266). Every one
        # of them has to name the model, or the journal cannot attribute
        # the outcome to a family.
        assert findings[0].is_infrastructure_error
        assert len(findings) > 1
        for finding in findings:
            assert finding_model(finding) == "codex (gpt-5)"

    def test_pr_body_names_reviewer_model(self, review_repo: ReviewRepo) -> None:
        prd_path = _write_prd(review_repo.path)
        agent = MockAgent(
            review_repo.review_json(
                stories=_REVIEW_STORIES,
                concerns=_REVIEW_CONCERNS,
            )
        )
        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            VerificationResult(passed=True, checks=[]),
            ReviewMode.HARD,
            PlainUI(no_color=True),
        )
        comp = Component(
            id="comp-a",
            title="Comp A",
            description="does things",
            dependencies=[],
            prd_path="prd.json",
            branch_name="kstrl/factory/comp-a",
        )
        comp.review_findings = result.as_pr_body_section()
        comp.findings = result.as_findings()
        manifest = Manifest(
            version="1",
            spec_file="",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[comp],
        )
        body = component_pr_body(
            comp, manifest, review_repo.path, isolation="none: ran on the host", requirements=()
        )
        assert "**Reviewer model**: codex (gpt-5)" in body


class RecordingUI(PlainUI):
    def __init__(self) -> None:
        super().__init__(no_color=True)
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        super().warn(message)


def _run_empty_factory(
    tmp_path: Path,
    config: FactoryConfig,
    *,
    base: KstrlConfig | None = None,
    ui: RecordingUI | None = None,
) -> FactoryResult:
    """run_factory over a zero-component manifest.

    Every test in TestHomogeneityWarningFires wants the run-level
    selection and nothing else, so no components is the whole point: the
    reviewer selection is resolved and announced before any component is
    scheduled.
    """
    return run_factory(
        Manifest(
            version="1",
            spec_file="",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[],
        ),
        config,
        base if base is not None else KstrlConfig(agent_type="claude-code"),
        ui if ui is not None else RecordingUI(),
        tmp_path,
        manifest_path=tmp_path / "manifest.json",
    )


class TestHomogeneityWarningFires:
    def test_run_factory_warns_once_and_journals_selection(
        self,
        tmp_path: Path,
    ) -> None:
        """A real run_factory invocation with a custom engineer command
        (family unknowable, so the warning fires regardless of which
        CLIs this machine has) prints the homogeneity warning for both
        enabled reviewer phases and journals the selection event."""
        ui = RecordingUI()
        result = _run_empty_factory(
            tmp_path,
            FactoryConfig(
                review_mode=ReviewMode.HARD.value,
                security_config=SecurityConfig(mode="hard"),
                create_prs=False,
                project_stack=in_process_stack(),
            ),
            base=KstrlConfig(agent_cmd="./fake-engineer.sh"),
            ui=ui,
        )
        assert result.exit_code == 0
        homogeneity = [w for w in ui.warnings if "Homogeneity risk" in w]
        assert len(homogeneity) == 2  # once per enabled phase, per run
        events = read_progress_events(tmp_path / ".kstrl" / "progress.jsonl")
        selections = [e for e in events if e["event"] == "adversarial_agent_selected"]
        assert {e["data"]["phase"] for e in selections} == {
            "review",
            "security",
        }
        assert all(e["data"]["homogeneous"] for e in selections)
        assert all(e["data"]["source"] == "same-family-fallback" for e in selections)

    def test_no_warning_when_reviewer_phases_disabled(
        self,
        tmp_path: Path,
    ) -> None:
        ui = RecordingUI()
        _run_empty_factory(
            tmp_path,
            FactoryConfig(
                review_mode=ReviewMode.SKIP.value,
                security_config=SecurityConfig(mode="skip"),
                create_prs=False,
            ),
            base=KstrlConfig(agent_cmd="./fake-engineer.sh"),
            ui=ui,
        )
        assert not [w for w in ui.warnings if "Homogeneity risk" in w]

    def test_skip_mode_run_does_not_pay_for_a_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#262 review: resolving is free, probing is not.

        A run that will never dispatch an adversarial call must not buy
        an answer it cannot use. Measured, that answer costs 4 to 6
        seconds, and up to $0.174 when the claude fallback attempt runs.
        """
        # Both CLIs on PATH, so the rotation reaches the probe on a
        # runner that has neither installed.
        set_cli_availability(monkeypatch, claude=True, codex=True)
        seen = stub_probe(monkeypatch, [json.dumps({"type": "turn.completed"})])

        _run_empty_factory(
            tmp_path,
            FactoryConfig(
                review_mode=ReviewMode.SKIP.value,
                security_config=SecurityConfig(mode="skip"),
                create_prs=False,
            ),
        )

        assert seen == []

    def test_a_run_that_will_review_does_pay_for_a_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The other half of the gate: it must not be a way to never probe.
        # Both CLIs on PATH, so the rotation reaches the probe on a
        # runner that has neither installed.
        set_cli_availability(monkeypatch, claude=True, codex=True)
        seen = stub_probe(monkeypatch, [json.dumps({"type": "turn.completed"})])

        _run_empty_factory(
            tmp_path,
            FactoryConfig(
                review_mode=ReviewMode.HARD.value,
                security_config=SecurityConfig(mode="skip"),
                create_prs=False,
            ),
        )

        assert len(seen) == 1

    def test_skip_mode_still_probes_when_the_ladder_can_restore_review(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every autonomy bundle sets review_mode="hard", and the ladder
        resolves AFTER the pipeline is already holding this selection.
        So with the ladder enabled, review_mode="skip" is not proof that
        review will not run, and the probe must not be skipped on it.
        """
        # Both CLIs on PATH, so the rotation reaches the probe on a
        # runner that has neither installed.
        set_cli_availability(monkeypatch, claude=True, codex=True)
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\n")
        seen = stub_probe(monkeypatch, [json.dumps({"type": "turn.completed"})])

        _run_empty_factory(
            tmp_path,
            FactoryConfig(
                review_mode=ReviewMode.SKIP.value,
                security_config=SecurityConfig(mode="skip"),
                create_prs=False,
            ),
        )

        assert len(seen) == 1
