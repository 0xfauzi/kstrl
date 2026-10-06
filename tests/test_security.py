"""The Phase 2.5 security review, driven through ``run_security_review``
on a real git repo with a real change on it (#266).

Every test here builds a review repo, hands ``run_security_review`` a
scripted agent and asserts the result the factory would gate on: skip
short-circuits, advisory passes with findings, hard mode fails at or
above ``fail_threshold`` (#524), an agent crash or unparseable reply is
an infrastructure error that fails hard mode and passes advisory, and
findings with an unknown category, an unknown severity or an empty
explanation are dropped while the well-formed one beside them is kept.
``MockSecurityAgent`` is shared with ``tests/test_security_fail_count.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from tests.conftest import ReviewRepo, make_review_repo, with_observed_diffstat


class MockSecurityAgent:
    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-security"

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()
        if self._output.strip():
            self._final_message = self._output

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_SECURITY_OUTPUT = json.dumps(
    {
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
    }
)

MALFORMED_FINDINGS_OUTPUT = json.dumps(
    {
        "findings": [
            {
                "category": "made_up",
                "severity": "high",
                "location": "x:1",
                "explanation": "unknown category",
            },
            {
                "category": "injection",
                "severity": "showstopper",
                "location": "x:2",
                "explanation": "unknown severity",
            },
            {
                "category": "injection",
                "severity": "high",
                "location": "x:3",
                "explanation": "",
            },
            {
                "category": "injection",
                "severity": "low",
                "location": "src/handler.py:42",
                "explanation": "the one well-formed finding",
            },
        ],
        "exhaustively_searched": True,
    }
)


class TestRunSecurityReview:
    def _setup_repo(self, tmp_path: Path) -> ReviewRepo:
        """#266: the security reviewer reads the worktree it runs in, so
        the fixture has to be a repo with a real change on it - not just
        an initialised repo whose HEAD is the base. A zero-length change
        would make a reviewer reply that reports nothing look correct."""
        repo = make_review_repo(tmp_path / "repo")
        (repo.path / "prd.json").write_text('{"branchName": "test", "userStories": []}')
        return repo

    def test_skip_mode_short_circuits(self, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.SKIP.value)
        agent = MockSecurityAgent("should not be called")
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is True
        assert result.findings == []

    def test_advisory_passes_even_with_findings(self, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        agent = MockSecurityAgent(with_observed_diffstat(VALID_SECURITY_OUTPUT, repo))
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is True
        assert len(result.findings) == 2

    def test_hard_fails_on_critical(self, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        config = SecurityConfig(
            mode=SecurityMode.HARD.value,
            fail_threshold="high",
        )
        agent = MockSecurityAgent(with_observed_diffstat(VALID_SECURITY_OUTPUT, repo))
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        # Critical finding exceeds threshold=high
        assert result.passed is False

    def test_hard_passes_with_only_low(self, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        output = json.dumps(
            {
                "findings": [
                    {
                        "category": "information_disclosure",
                        "severity": "low",
                        "location": "x:1",
                        "explanation": "stack trace in log",
                    }
                ]
            }
        )
        agent = MockSecurityAgent(with_observed_diffstat(output, repo))
        config = SecurityConfig(
            mode=SecurityMode.HARD.value,
            fail_threshold="high",
        )
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is True

    def test_malformed_findings_are_dropped_and_the_valid_one_kept(self, tmp_path: Path) -> None:
        """An unknown category, an unknown severity and an empty
        explanation each drop their finding; the well-formed finding
        beside them survives, and with only a low left the hard gate
        passes rather than counting the dropped high."""
        repo = self._setup_repo(tmp_path)
        agent = MockSecurityAgent(with_observed_diffstat(MALFORMED_FINDINGS_OUTPUT, repo))
        config = SecurityConfig(
            mode=SecurityMode.HARD.value,
            fail_threshold="high",
        )
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is True
        assert result.infrastructure_error is False
        assert [(f.severity, f.explanation) for f in result.findings] == [
            ("low", "the one well-formed finding")
        ]
        assert result.exhaustively_searched is True

    def test_a_new_dependency_is_listed_in_the_pr_body_and_does_not_fail_hard_mode(
        self, tmp_path: Path
    ) -> None:
        """#696 slice 9: kstrl reads no lockfile, so the security reviewer
        is asked to list every dependency a change adds, the listing is
        kept and reaches the PR body, and a listing at "low" does not
        fail hard mode at the default threshold."""
        repo = self._setup_repo(tmp_path)
        output = json.dumps(
            {
                "findings": [
                    {
                        "category": "new_dependency",
                        "severity": "low",
                        "location": "package.json:6",
                        "explanation": "adds left-pad 1.3.0, license WTFPL",
                    }
                ],
                "exhaustively_searched": True,
            }
        )
        prompts: list[str] = []

        class _Recording(MockSecurityAgent):
            def run(
                self,
                prompt: str,
                cwd: Path | None = None,
                timeout: float | None = None,
            ) -> Iterator[str]:
                prompts.append(prompt)
                yield from super().run(prompt, cwd, timeout)

        agent = _Recording(with_observed_diffstat(output, repo))
        config = SecurityConfig(mode=SecurityMode.HARD.value, fail_threshold="high")
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            PlainUI(no_color=True),
        )
        assert len(prompts) == 1 and '"new_dependency"' in prompts[0]
        assert result.passed is True
        assert result.infrastructure_error is False
        assert [(f.category, f.severity) for f in result.findings] == [("new_dependency", "low")]
        assert "- [low] **new_dependency** at `package.json:6`" in result.as_pr_body_section()

    def _boom_agent(self) -> object:
        class _Boom:
            @property
            def name(self) -> str:
                return "boom"

            def run(
                self,
                prompt: str,
                cwd: Path | None = None,
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
        repo = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            self._boom_agent(),
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "exploded" in result.overall_notes

    def test_agent_crash_advisory_mode_passes(self, tmp_path: Path) -> None:
        """Advisory mode should warn but not block."""
        repo = self._setup_repo(tmp_path)
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            self._boom_agent(),
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is True
        assert result.infrastructure_error is True

    def test_parse_failure_hard_mode_fails(self, tmp_path: Path) -> None:
        """If the agent returns un-parseable output in hard mode, we
        must NOT silently overwrite passed=False from the fail count
        on the (empty) findings list."""
        repo = self._setup_repo(tmp_path)
        agent = MockSecurityAgent("garbage that is not JSON at all")
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is False
        assert result.infrastructure_error is True

    def test_parse_failure_advisory_mode_passes(self, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        agent = MockSecurityAgent("garbage")
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        ui = PlainUI(no_color=True)
        result = run_security_review(
            agent,
            repo.path / "prd.json",
            repo.path,
            repo.base_branch,
            config,
            ui,
        )
        assert result.passed is True
        assert result.infrastructure_error is True
