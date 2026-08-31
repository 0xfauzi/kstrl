"""R7.1: cross-model review rotation.

Covers the three test surfaces the roadmap item names:
- the default-selection matrix (both CLIs present / one absent /
  explicit override / custom engineer command),
- the ``model:<id>`` identity tag flowing from a review run onto every
  Finding, into the PR body, and into the journal serialization,
- the homogeneity warning firing (resolver-level and through a real
  ``run_factory`` invocation),
plus the calibration reviewer-override helpers that make the
same-family vs cross-family baseline comparison capturable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl import calibration
from kstrl.config import KstrlConfig
from kstrl.factory import (
    AdversarialAgentSelection,
    FactoryConfig,
    FactoryResult,
    resolve_adversarial_selection,
    run_factory,
)
from kstrl.findings import (
    Finding,
    finding_model,
    render_findings_markdown,
    tag_finding_with_attempt,
    tag_finding_with_model,
)
from kstrl.manifest import Component, Manifest
from kstrl.observability import read_progress_events
from kstrl.pr import _generate_pr_body
from kstrl.review import (
    ReviewMode,
    run_review,
)
from kstrl.security import (
    SecurityConfig,
    SecurityResult,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult
from tests.conftest import ReviewRepo
from tests.helpers.agent_probe import set_cli_availability, stub_probe


def _resolve(
    phase: str = "review",
    *,
    explicit_cmd: str | None = None,
    explicit_type: str | None = None,
    explicit_model: str | None = None,
    fallback_cmd: str | None = None,
    fallback_type: str | None = None,
    fallback_model: str | None = None,
    fallback_reasoning: str | None = None,
    engineer_cmd: str | None = None,
    engineer_type: str | None = None,
    claude_available: bool = True,
    codex_available: bool = True,
) -> AdversarialAgentSelection:
    """Call the resolver with availability always injected: these tests
    must never depend on which CLIs the test machine has installed."""
    return resolve_adversarial_selection(
        phase,
        explicit_cmd=explicit_cmd,
        explicit_type=explicit_type,
        explicit_model=explicit_model,
        fallback_cmd=fallback_cmd,
        fallback_type=fallback_type,
        fallback_model=fallback_model,
        fallback_reasoning=fallback_reasoning,
        engineer_cmd=engineer_cmd,
        engineer_type=engineer_type,
        claude_available=claude_available,
        codex_available=codex_available,
    )


class TestSelectionMatrix:
    """Default-selection matrix for resolve_adversarial_selection."""

    def test_claude_engineer_defaults_to_codex_when_available(self) -> None:
        sel = _resolve(engineer_type="claude-code", fallback_type="claude-code")
        assert sel.source == "cross-family-default"
        assert sel.agent_type == "codex"
        assert sel.agent_cmd is None
        assert sel.model is None
        assert sel.identity == "codex"
        assert sel.warning is None

    def test_auto_engineer_resolves_to_claude_then_crosses_to_codex(self) -> None:
        # agent_type None ("auto") with claude installed is a claude
        # engineer, so the reviewer crosses to codex.
        sel = _resolve(engineer_type=None, fallback_type=None)
        assert sel.source == "cross-family-default"
        assert sel.agent_type == "codex"

    def test_codex_engineer_defaults_to_claude_when_available(self) -> None:
        sel = _resolve(engineer_type="codex", fallback_type="codex")
        assert sel.source == "cross-family-default"
        assert sel.agent_type == "claude-code"
        assert sel.identity == "claude-code"
        assert sel.warning is None

    def test_claude_engineer_falls_back_when_codex_absent(self) -> None:
        sel = _resolve(
            engineer_type="claude-code",
            fallback_type="claude-code",
            codex_available=False,
        )
        assert sel.source == "same-family-fallback"
        assert sel.agent_type == "claude-code"
        assert sel.warning is not None
        assert "codex CLI is not available" in sel.warning
        assert "Self-preference bias" in sel.warning

    def test_claude_engineer_downgrades_when_codex_is_installed_but_dead(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#262: PATH said yes, the CLI could not run a turn.

        The whole point of the probe. Before it, this run selected codex
        for review on the strength of the binary existing, paid the full
        engineer bill, and only then failed every adversarial dispatch.
        A dead cross CLI now takes the same route a missing one takes.
        """
        stub_probe(
            monkeypatch,
            [json.dumps({"type": "turn.failed", "error": {"message": "usage limit reached"}})],
        )

        sel = _resolve(engineer_type="claude-code", fallback_type="claude-code")

        assert sel.source == "same-family-fallback"
        assert sel.agent_type == "claude-code"
        assert sel.warning is not None
        assert "codex CLI is installed but cannot run a turn" in sel.warning
        assert "(usage limit reached)" in sel.warning
        assert "Self-preference bias" in sel.warning
        # "Install codex" is useless advice to someone who has it.
        assert "Install the codex CLI" not in sel.warning

    def test_live_cross_family_cli_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(monkeypatch, [json.dumps({"type": "turn.completed"})])

        sel = _resolve(engineer_type="claude-code", fallback_type="claude-code")

        assert sel.source == "cross-family-default"
        assert sel.agent_type == "codex"
        assert sel.warning is None

    def test_absent_cross_family_cli_is_never_probed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A missing binary is already an answer; spending a probe turn
        # to confirm it would be money for nothing.
        seen = stub_probe(monkeypatch, [])

        _resolve(
            engineer_type="claude-code",
            fallback_type="claude-code",
            codex_available=False,
        )

        assert seen == []

    def test_review_and_security_share_one_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = stub_probe(monkeypatch, [json.dumps({"type": "turn.completed"})])

        _resolve(engineer_type="claude-code", fallback_type="claude-code")
        _resolve("security", engineer_type="claude-code", fallback_type="claude-code")

        assert len(seen) == 1

    def test_codex_engineer_falls_back_when_claude_absent(self) -> None:
        # auto-detect with claude missing resolves the engineer to
        # codex; the cross family (claude-code) is the missing one.
        sel = _resolve(
            engineer_type=None,
            fallback_type=None,
            claude_available=False,
        )
        assert sel.source == "same-family-fallback"
        assert sel.warning is not None
        assert "claude-code CLI is not available" in sel.warning

    def test_explicit_model_wins_over_cross_family_default(self) -> None:
        # Both CLIs present, but the operator pinned a review model:
        # explicit config always wins, silently (no homogeneity nag for
        # a deliberate choice).
        sel = _resolve(
            explicit_model="opus",
            fallback_type="claude-code",
            engineer_type="claude-code",
        )
        assert sel.source == "explicit"
        assert sel.agent_type == "claude-code"
        assert sel.model == "opus"
        assert sel.identity == "claude-code (opus)"
        assert sel.warning is None

    def test_explicit_agent_cmd_wins_and_identity_is_custom(self) -> None:
        sel = _resolve(
            explicit_cmd="./my-reviewer.sh",
            fallback_type="claude-code",
            engineer_type="claude-code",
        )
        assert sel.source == "explicit"
        assert sel.agent_cmd == "./my-reviewer.sh"
        assert sel.identity == "custom (./my-reviewer.sh)"
        assert sel.warning is None

    def test_explicit_type_pins_the_family(self) -> None:
        sel = _resolve(
            explicit_type="claude-code",
            fallback_type="claude-code",
            engineer_type="claude-code",
        )
        assert sel.source == "explicit"
        assert sel.agent_type == "claude-code"

    def test_custom_engineer_cmd_warns_family_unknown(self) -> None:
        # A custom engineer command has an unknown family even with both
        # CLIs installed: heterogeneity cannot be established, so the
        # fallback fires WITH the warning.
        sel = _resolve(
            engineer_cmd="./fake-engineer.sh",
            fallback_cmd="./fake-engineer.sh",
            fallback_type=None,
        )
        assert sel.source == "same-family-fallback"
        assert sel.agent_cmd == "./fake-engineer.sh"
        assert sel.warning is not None
        assert "custom agent command" in sel.warning
        assert "Self-preference bias" in sel.warning

    def test_security_fallback_keeps_engineer_cmd_and_model(self) -> None:
        # The security phase's historical fallback inherits the
        # engineer's cmd/model/reasoning; the same-family fallback must
        # preserve that exactly (only the warning is new).
        sel = _resolve(
            "security",
            engineer_type="claude-code",
            fallback_type="claude-code",
            fallback_model="opus",
            fallback_reasoning="high",
            codex_available=False,
        )
        assert sel.source == "same-family-fallback"
        assert sel.model == "opus"
        assert sel.reasoning == "high"
        assert sel.identity == "claude-code (opus)"

    def test_cross_family_default_does_not_inherit_reasoning(self) -> None:
        # Effort strings do not transfer across families.
        sel = _resolve(
            "security",
            engineer_type="claude-code",
            fallback_type="claude-code",
            fallback_model="opus",
            fallback_reasoning="high",
        )
        assert sel.source == "cross-family-default"
        assert sel.model is None
        assert sel.reasoning is None


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

    def test_security_clean_result_still_names_reviewer(self) -> None:
        result = SecurityResult(
            passed=True,
            mode="hard",
            reviewer_model="codex (gpt-5)",
        )
        assert "**Reviewer model**: codex (gpt-5)" in result.as_pr_body_section()

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

    def test_model_tag_is_idempotent_and_composes_with_attempt(self) -> None:
        f = Finding.from_review_concern(
            category="dead_code",
            severity="advisory",
            location="a.py:1",
            explanation="x",
        )
        tagged = tag_finding_with_model(f, "codex")
        again = tag_finding_with_model(tagged, "claude-code")
        assert again.tags.count("model:codex") == 1
        assert "model:claude-code" not in again.tags
        # Empty identity is a no-op, never a fabricated "model:" tag.
        assert tag_finding_with_model(f, "") == f
        with_attempt = tag_finding_with_attempt(tagged, 2)
        assert finding_model(with_attempt) == "codex"
        assert "attempt:2" in with_attempt.tags

    def test_render_findings_markdown_names_reviewer_model(self) -> None:
        f = tag_finding_with_model(
            Finding.infrastructure_error("security", "boom"),
            "codex (gpt-5)",
        )
        rendered = render_findings_markdown([f])
        assert "Reviewer model: codex (gpt-5)" in rendered

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
            spec_file="spec.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[comp],
        )
        body = _generate_pr_body(comp, manifest)
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
            spec_file="spec.md",
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
                security_config=None,
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
                security_config=None,
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
                security_config=None,
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
                security_config=None,
                create_prs=False,
            ),
        )

        assert len(seen) == 1


class TestCalibrationReviewerOverride:
    def test_override_from_env_reads_both_vars(self) -> None:
        env = {
            "KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE": "codex",
            "KSTRL_CALIBRATION_REVIEWER_MODEL": "gpt-5",
        }
        assert calibration.reviewer_override_from_env(env) == ("codex", "gpt-5")

    def test_override_from_env_treats_empty_as_unset(self) -> None:
        env = {
            "KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE": "",
            "KSTRL_CALIBRATION_REVIEWER_MODEL": "",
        }
        assert calibration.reviewer_override_from_env(env) == (None, None)
        assert calibration.reviewer_override_from_env({}) == (None, None)

    def test_label_plain_without_override(self) -> None:
        assert calibration.reviewer_override_label("haiku", None, None) == "haiku"

    def test_label_encodes_override(self) -> None:
        assert (
            calibration.reviewer_override_label("haiku", "codex", "gpt-5")
            == "haiku+reviewer:codex/gpt-5"
        )
        assert calibration.reviewer_override_label("haiku", "codex", None) == "haiku+reviewer:codex"

    def test_cross_family_baselines_compare_with_warning_not_failure(
        self,
    ) -> None:
        """The whole point of the label: comparing a same-family baseline
        against a cross-family one warns (deltas measure the family
        change) instead of silently pretending both measured the same
        configuration."""
        old = calibration.Baseline(
            path=None,
            model="haiku",
            timestamp="t1",
            format_version=2,
            runs_per_fixture=3,
            fixtures=(
                calibration.FixtureStats(
                    role="security",
                    fixture_id="sec-01",
                    category="injection",
                    cwe="CWE-89",
                    runs_total=3,
                    runs_errored=0,
                    runs_detected=3,
                ),
            ),
        )
        new = calibration.Baseline(
            path=None,
            model=calibration.reviewer_override_label(
                "haiku",
                "codex",
                "gpt-5",
            ),
            timestamp="t2",
            format_version=2,
            runs_per_fixture=3,
            fixtures=old.fixtures,
        )
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed
        assert any("comparing across models" in w for w in comparison.warnings)
