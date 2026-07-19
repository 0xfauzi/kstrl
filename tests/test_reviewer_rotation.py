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

from ralph_py import calibration
from ralph_py.config import RalphConfig
from ralph_py.factory import (
    AdversarialAgentSelection,
    FactoryConfig,
    resolve_adversarial_selection,
    run_factory,
)
from ralph_py.findings import (
    Finding,
    finding_model,
    render_findings_markdown,
    tag_finding_with_attempt,
    tag_finding_with_model,
)
from ralph_py.manifest import Component, Manifest
from ralph_py.observability import read_progress_events
from ralph_py.pr import _generate_pr_body
from ralph_py.review import (
    ReviewMode,
    ReviewResult,
    merge_review_results,
    run_review,
)
from ralph_py.security import (
    SecurityConfig,
    SecurityResult,
    merge_security_results,
    run_security_review,
)
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import CheckResult, VerificationResult


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
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


class CrashingAgent(MockAgent):
    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        raise RuntimeError("agent exploded")
        yield ""  # pragma: no cover


REVIEW_OUTPUT_WITH_CONCERN = json.dumps({
    "stories": [{
        "storyId": "US-001",
        "storyTitle": "Test",
        "criteria": [{
            "criterion": "AC1",
            "verdict": "fail",
            "explanation": "not implemented",
            "suggestion": "implement it",
        }],
    }],
    "concerns": [{
        "category": "dead_code",
        "severity": "advisory",
        "location": "a.py:1",
        "explanation": "unused helper",
        "suggestion": "remove",
    }],
    "exhaustively_searched": True,
    "overallNotes": "",
})

SECURITY_OUTPUT_WITH_FINDING = json.dumps({
    "findings": [{
        "category": "hardcoded_secret",
        "severity": "high",
        "location": "b.py:3",
        "explanation": "API key in source",
        "suggestion": "move to env",
    }],
    "exhaustively_searched": True,
    "overallNotes": "",
})


def _write_prd(tmp_path: Path) -> Path:
    prd_path = tmp_path / "prd.json"
    prd_path.write_text(json.dumps({
        "branchName": "test",
        "userStories": [{
            "id": "US-001", "title": "Test",
            "acceptanceCriteria": ["AC1"], "priority": 1,
            "passes": True, "notes": "",
        }],
    }))
    return prd_path


class TestModelTagEndToEnd:
    def test_review_findings_carry_model_tag(self, tmp_path: Path) -> None:
        prd_path = _write_prd(tmp_path)
        agent = MockAgent(REVIEW_OUTPUT_WITH_CONCERN)
        result = run_review(
            agent, prd_path, tmp_path, "main",
            VerificationResult(
                passed=True, checks=[CheckResult("test_suite", True, "ok")],
            ),
            ReviewMode.HARD, PlainUI(no_color=True),
            diff_content="+change\n",
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

    def test_review_crash_still_attributes_reviewer(self, tmp_path: Path) -> None:
        prd_path = _write_prd(tmp_path)
        agent = CrashingAgent("", name="codex (gpt-5)")
        result = run_review(
            agent, prd_path, tmp_path, "main",
            VerificationResult(passed=True, checks=[]),
            ReviewMode.HARD, PlainUI(no_color=True),
            diff_content="+change\n",
        )
        assert result.infrastructure_error is True
        assert result.reviewer_model == "codex (gpt-5)"
        (finding,) = result.as_findings()
        assert finding.is_infrastructure_error
        assert finding_model(finding) == "codex (gpt-5)"

    def test_security_findings_carry_model_tag(self, tmp_path: Path) -> None:
        agent = MockAgent(SECURITY_OUTPUT_WITH_FINDING, name="codex")
        result = run_security_review(
            agent, tmp_path / "missing-prd.json", tmp_path, "main",
            SecurityConfig(mode="advisory"), PlainUI(no_color=True),
            diff_content="+change\n",
        )
        assert result.reviewer_model == "codex"
        findings = result.as_findings()
        assert findings
        for f in findings:
            assert finding_model(f) == "codex"
        assert "**Reviewer model**: codex" in result.as_pr_body_section()

    def test_security_clean_result_still_names_reviewer(self) -> None:
        result = SecurityResult(
            passed=True, mode="hard", reviewer_model="codex (gpt-5)",
        )
        assert "**Reviewer model**: codex (gpt-5)" in result.as_pr_body_section()

    def test_merges_carry_reviewer_model(self) -> None:
        chunks_r = [
            ReviewResult(passed=True, mode="hard", reviewer_model="codex"),
            ReviewResult(passed=True, mode="hard", reviewer_model="codex"),
        ]
        assert merge_review_results(chunks_r, "hard").reviewer_model == "codex"
        chunks_s = [
            SecurityResult(passed=True, mode="hard", reviewer_model="codex"),
            SecurityResult(passed=True, mode="hard", reviewer_model="codex"),
        ]
        assert (
            merge_security_results(chunks_s, "hard").reviewer_model == "codex"
        )

    def test_model_tag_is_idempotent_and_composes_with_attempt(self) -> None:
        f = Finding.from_review_concern(
            category="dead_code", severity="advisory",
            location="a.py:1", explanation="x",
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
            Finding.infrastructure_error("security", "boom"), "codex (gpt-5)",
        )
        rendered = render_findings_markdown([f])
        assert "Reviewer model: codex (gpt-5)" in rendered

    def test_pr_body_names_reviewer_model(self, tmp_path: Path) -> None:
        prd_path = _write_prd(tmp_path)
        agent = MockAgent(REVIEW_OUTPUT_WITH_CONCERN)
        result = run_review(
            agent, prd_path, tmp_path, "main",
            VerificationResult(passed=True, checks=[]),
            ReviewMode.HARD, PlainUI(no_color=True),
            diff_content="+change\n",
        )
        comp = Component(
            id="comp-a", title="Comp A", description="does things",
            dependencies=[], prd_path="prd.json",
            branch_name="ralph/factory/comp-a",
        )
        comp.review_findings = result.as_pr_body_section()
        comp.findings = result.as_findings()
        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="p",
            base_branch="main", single_pr=False, components=[comp],
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


class TestHomogeneityWarningFires:
    def test_run_factory_warns_once_and_journals_selection(
        self, tmp_path: Path,
    ) -> None:
        """A real run_factory invocation with a custom engineer command
        (family unknowable, so the warning fires regardless of which
        CLIs this machine has) prints the homogeneity warning for both
        enabled reviewer phases and journals the selection event."""
        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="p",
            base_branch="main", single_pr=False, components=[],
        )
        config = FactoryConfig(
            review_mode=ReviewMode.HARD.value,
            security_config=SecurityConfig(mode="hard"),
            create_prs=False,
        )
        base = RalphConfig(agent_cmd="./fake-engineer.sh")
        ui = RecordingUI()
        result = run_factory(
            manifest, config, base, ui, tmp_path,
            manifest_path=tmp_path / "manifest.json",
        )
        assert result.exit_code == 0
        homogeneity = [w for w in ui.warnings if "Homogeneity risk" in w]
        assert len(homogeneity) == 2  # once per enabled phase, per run
        events = read_progress_events(tmp_path / ".ralph" / "progress.jsonl")
        selections = [
            e for e in events if e["event"] == "adversarial_agent_selected"
        ]
        assert {e["data"]["phase"] for e in selections} == {
            "review", "security",
        }
        assert all(e["data"]["homogeneous"] for e in selections)
        assert all(
            e["data"]["source"] == "same-family-fallback" for e in selections
        )

    def test_no_warning_when_reviewer_phases_disabled(
        self, tmp_path: Path,
    ) -> None:
        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="p",
            base_branch="main", single_pr=False, components=[],
        )
        config = FactoryConfig(
            review_mode=ReviewMode.SKIP.value,
            security_config=None,
            create_prs=False,
        )
        base = RalphConfig(agent_cmd="./fake-engineer.sh")
        ui = RecordingUI()
        run_factory(
            manifest, config, base, ui, tmp_path,
            manifest_path=tmp_path / "manifest.json",
        )
        assert not [w for w in ui.warnings if "Homogeneity risk" in w]


class TestCalibrationReviewerOverride:
    def test_override_from_env_reads_both_vars(self) -> None:
        env = {
            "RALPH_CALIBRATION_REVIEWER_AGENT_TYPE": "codex",
            "RALPH_CALIBRATION_REVIEWER_MODEL": "gpt-5",
        }
        assert calibration.reviewer_override_from_env(env) == ("codex", "gpt-5")

    def test_override_from_env_treats_empty_as_unset(self) -> None:
        env = {
            "RALPH_CALIBRATION_REVIEWER_AGENT_TYPE": "",
            "RALPH_CALIBRATION_REVIEWER_MODEL": "",
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
        assert (
            calibration.reviewer_override_label("haiku", "codex", None)
            == "haiku+reviewer:codex"
        )

    def test_cross_family_baselines_compare_with_warning_not_failure(
        self,
    ) -> None:
        """The whole point of the label: comparing a same-family baseline
        against a cross-family one warns (deltas measure the family
        change) instead of silently pretending both measured the same
        configuration."""
        old = calibration.Baseline(
            path=None, model="haiku", timestamp="t1",
            format_version=2, runs_per_fixture=3,
            fixtures=(calibration.FixtureStats(
                role="security", fixture_id="sec-01",
                category="injection", cwe="CWE-89",
                runs_total=3, runs_errored=0, runs_detected=3,
            ),),
        )
        new = calibration.Baseline(
            path=None,
            model=calibration.reviewer_override_label(
                "haiku", "codex", "gpt-5",
            ),
            timestamp="t2", format_version=2, runs_per_fixture=3,
            fixtures=old.fixtures,
        )
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed
        assert any("comparing across models" in w for w in comparison.warnings)
