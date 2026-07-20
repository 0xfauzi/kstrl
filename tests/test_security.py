"""Tests for the Phase 2.5 security review module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.security import (
    SECURITY_PROMPT,
    SecurityConfig,
    SecurityFinding,
    SecurityMode,
    SecurityResult,
    _passes_threshold,
    parse_security_output,
    run_security_review,
)
from kstrl.ui.plain import PlainUI


class MockSecurityAgent:
    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-security"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()
        if self._output.strip():
            self._final_message = self._output

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_SECURITY_OUTPUT = json.dumps({
    "findings": [
        {
            "category": "injection",
            "severity": "critical",
            "location": "src/handler.py:42",
            "explanation": "subprocess.run with shell=True on user input",
            "suggestion": "Use shell=False and pass args as list",
        },
        {
            "category": "hardcoded_secret",
            "severity": "medium",
            "location": "src/auth.py:10",
            "explanation": "default API key string in source",
        },
    ],
    "exhaustively_searched": True,
})


# ---------------------------------------------------------------------------
# SecurityConfig
# ---------------------------------------------------------------------------


class TestSecurityConfigDefaults:
    def test_defaults(self) -> None:
        # R2.1: default aligned with the documented product default -
        # security review is an opt-in extra LLM call.
        c = SecurityConfig()
        assert c.mode == "skip"
        assert c.timeout_seconds == 600.0
        assert c.fail_threshold == "high"

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RALPH_SECURITY_MODE", "hard")
        monkeypatch.setenv("RALPH_SECURITY_FAIL_THRESHOLD", "critical")
        monkeypatch.setenv("RALPH_SECURITY_TIMEOUT", "300")
        c = SecurityConfig.from_env()
        assert c.mode == "hard"
        assert c.fail_threshold == "critical"
        assert c.timeout_seconds == 300.0


# ---------------------------------------------------------------------------
# parse_security_output
# ---------------------------------------------------------------------------


class TestParseSecurityOutput:
    def test_valid_output(self) -> None:
        result = parse_security_output(VALID_SECURITY_OUTPUT, "advisory")
        assert len(result.findings) == 2
        assert result.findings[0].severity == "critical"
        assert result.findings[1].category == "hardcoded_secret"
        assert result.exhaustively_searched is True

    def test_invalid_json_returns_failed_result(self) -> None:
        result = parse_security_output("not json", "hard")
        assert result.passed is False
        assert "Failed to parse" in result.overall_notes

    def test_invalid_category_dropped(self) -> None:
        output = json.dumps({"findings": [{
            "category": "made_up",
            "severity": "high",
            "location": "x:1",
            "explanation": "x",
        }]})
        result = parse_security_output(output, "advisory")
        assert result.findings == []

    def test_invalid_severity_dropped(self) -> None:
        output = json.dumps({"findings": [{
            "category": "injection",
            "severity": "showstopper",
            "location": "x:1",
            "explanation": "x",
        }]})
        result = parse_security_output(output, "advisory")
        assert result.findings == []

    def test_missing_explanation_dropped(self) -> None:
        output = json.dumps({"findings": [{
            "category": "injection",
            "severity": "high",
            "location": "x:1",
            "explanation": "",
        }]})
        result = parse_security_output(output, "advisory")
        assert result.findings == []

    def test_empty_findings_with_exhaustive_flag(self) -> None:
        output = json.dumps({
            "findings": [],
            "exhaustively_searched": True,
        })
        result = parse_security_output(output, "hard")
        assert result.findings == []
        assert result.exhaustively_searched is True


# ---------------------------------------------------------------------------
# _passes_threshold
# ---------------------------------------------------------------------------


class TestPassesThreshold:
    def _f(self, severity: str) -> SecurityFinding:
        return SecurityFinding(
            category="injection",
            severity=severity,
            location="x:1",
            explanation="x",
        )

    def test_skip_always_passes(self) -> None:
        assert _passes_threshold(
            [self._f("critical")], SecurityMode.SKIP.value, "high",
        )

    def test_advisory_always_passes(self) -> None:
        assert _passes_threshold(
            [self._f("critical")], SecurityMode.ADVISORY.value, "high",
        )

    def test_hard_passes_when_below_threshold(self) -> None:
        # threshold=high; medium is below
        assert _passes_threshold(
            [self._f("medium")], SecurityMode.HARD.value, "high",
        )

    def test_hard_fails_at_threshold(self) -> None:
        assert not _passes_threshold(
            [self._f("high")], SecurityMode.HARD.value, "high",
        )

    def test_hard_fails_above_threshold(self) -> None:
        assert not _passes_threshold(
            [self._f("critical")], SecurityMode.HARD.value, "high",
        )

    def test_hard_with_critical_only_threshold(self) -> None:
        # threshold=critical; high is below
        assert _passes_threshold(
            [self._f("high")], SecurityMode.HARD.value, "critical",
        )
        assert not _passes_threshold(
            [self._f("critical")], SecurityMode.HARD.value, "critical",
        )


# ---------------------------------------------------------------------------
# run_security_review
# ---------------------------------------------------------------------------


class TestRunSecurityReview:
    def _setup_repo(self, tmp_path: Path) -> Path:
        import subprocess
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"],
            check=True, capture_output=True,
        )
        (tmp_path / "stub").write_text("x")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "stub"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
            check=True, capture_output=True,
        )
        prd_path = tmp_path / "prd.json"
        prd_path.write_text('{"branchName": "test", "userStories": []}')
        return prd_path

    def test_skip_mode_short_circuits(self, tmp_path: Path) -> None:
        prd_path = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.SKIP.value)
        agent = MockSecurityAgent("should not be called")
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent, prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is True
        assert result.findings == []

    def test_advisory_passes_even_with_findings(self, tmp_path: Path) -> None:
        prd_path = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        agent = MockSecurityAgent(VALID_SECURITY_OUTPUT)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent, prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is True
        assert len(result.findings) == 2

    def test_hard_fails_on_critical(self, tmp_path: Path) -> None:
        prd_path = self._setup_repo(tmp_path)
        config = SecurityConfig(
            mode=SecurityMode.HARD.value, fail_threshold="high",
        )
        agent = MockSecurityAgent(VALID_SECURITY_OUTPUT)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent, prd_path, tmp_path, "main", config, ui,
        )
        # Critical finding exceeds threshold=high
        assert result.passed is False

    def test_hard_passes_with_only_low(self, tmp_path: Path) -> None:
        prd_path = self._setup_repo(tmp_path)
        output = json.dumps({"findings": [{
            "category": "information_disclosure",
            "severity": "low",
            "location": "x:1",
            "explanation": "stack trace in log",
        }]})
        agent = MockSecurityAgent(output)
        config = SecurityConfig(
            mode=SecurityMode.HARD.value, fail_threshold="high",
        )
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent, prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is True

    def _boom_agent(self) -> object:
        class _Boom:
            @property
            def name(self) -> str:
                return "boom"

            def run(
                self, prompt: str, cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                raise RuntimeError("agent exploded")
                yield  # pragma: no cover  (makes this an iterator)

            @property
            def final_message(self) -> str | None:
                return None

        return _Boom()

    def test_agent_crash_hard_mode_fails(self, tmp_path: Path) -> None:
        """Hard mode must surface infrastructure errors as a failure -
        otherwise a flaky API silently approves every diff."""
        prd_path = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            self._boom_agent(), prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "exploded" in result.overall_notes

    def test_agent_crash_advisory_mode_passes(self, tmp_path: Path) -> None:
        """Advisory mode should warn but not block."""
        prd_path = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            self._boom_agent(), prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is True
        assert result.infrastructure_error is True

    def test_parse_failure_hard_mode_fails(self, tmp_path: Path) -> None:
        """If the agent returns un-parseable output in hard mode, we
        must NOT silently overwrite passed=False via _passes_threshold
        on the (empty) findings list."""
        prd_path = self._setup_repo(tmp_path)
        agent = MockSecurityAgent("garbage that is not JSON at all")
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent, prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is False
        assert result.infrastructure_error is True

    def test_parse_failure_advisory_mode_passes(self, tmp_path: Path) -> None:
        prd_path = self._setup_repo(tmp_path)
        agent = MockSecurityAgent("garbage")
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent, prd_path, tmp_path, "main", config, ui,
        )
        assert result.passed is True
        assert result.infrastructure_error is True


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


class TestResultFormatting:
    def test_pr_body_with_findings(self) -> None:
        r = SecurityResult(
            passed=False,
            mode="hard",
            findings=[
                SecurityFinding(
                    category="injection",
                    severity="critical",
                    location="src/x.py:1-5",
                    explanation="shell=True",
                    suggestion="use list args",
                ),
            ],
        )
        body = r.as_pr_body_section()
        assert "Security Review" in body
        assert "1 critical" in body
        assert "injection" in body

    def test_pr_body_no_findings(self) -> None:
        r = SecurityResult(
            passed=True,
            mode="hard",
            exhaustively_searched=True,
        )
        body = r.as_pr_body_section()
        assert "No findings" in body
        assert "exhaustively" in body

    def test_retry_context_lists_findings(self) -> None:
        r = SecurityResult(
            passed=False,
            mode="hard",
            findings=[SecurityFinding(
                category="auth_bypass",
                severity="high",
                location="x:1",
                explanation="no auth check",
                suggestion="add @require_auth",
            )],
        )
        ctx = r.as_retry_context()
        assert "auth_bypass" in ctx
        assert "no auth check" in ctx
        assert "@require_auth" in ctx


def test_prompt_renders_with_placeholders() -> None:
    """The SECURITY_PROMPT must format cleanly with the three
    placeholders the runner provides (R5.3 added data_delimiter)."""
    rendered = SECURITY_PROMPT.format(
        prd_content="some prd",
        diff_content="some diff",
        data_delimiter="RALPH-DATA-test",
    )
    assert "some prd" in rendered
    assert "some diff" in rendered
    # Sanity: the schema example should be intact
    assert "findings" in rendered
