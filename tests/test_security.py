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
import os
from collections.abc import Iterator
from pathlib import Path

from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from tests.conftest import ReviewRepo, make_review_repo, with_observed_diffstat
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_acceptance_e2e import FAKE_GH
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import _repo, _spawn, _stack


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
        body = result.as_pr_body_section()
        assert "- [low] **new_dependency** at `package.json:6`" in body
        assert "  - adds left-pad 1.3.0, license WTFPL" in body

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


# --- the default: a run with no [security] setting reviews (#696 slice 9) ---

#: One agent for every role. The security reviewer's prompt names that role,
#: and the reply carries the diffstat of the six lines the engineer commits.
_ONE_AGENT = """#!/bin/sh
prompt=$(cat)
echo call >> "$AGENT_CALLS"
case "$prompt" in
*"adversarial application security reviewer"*)
  echo security >> "$AGENT_CALLS"
  cat <<'JSON'
{"observedDiffstat": {"files": 1, "insertions": 6, "deletions": 0},
 "findings": [{"category": "new_dependency", "severity": "low",
   "location": "package.json:4", "explanation": "adds left-pad 1.3.0"}],
 "exhaustively_searched": true}
JSON
  exit 0 ;;
esac
cat > package.json <<'JSON'
{
  "name": "demo",
  "dependencies": {
    "left-pad": "1.3.0"
  }
}
JSON
git add -A && git commit -q -m dependency >/dev/null 2>&1
echo '<promise>COMPLETE</promise>'
"""


@runs_a_stack
def test_a_default_run_asks_the_security_reviewer_and_lists_the_new_dependency(
    tmp_path: Path,
) -> None:
    """#696 slice 9: kstrl reads no lockfile, so the security reviewer is the
    only default dependency check. The real `ks factory` runs with no
    [security] setting in kstrl.toml and none in the environment. The stub
    engineer commits a package.json that adds left-pad. The reviewer is
    called, its "low" listing reaches the PR body beside the package name,
    and the run is neither blocked nor marked as unverified."""
    root = _repo(tmp_path, _stack({"tests": "true"}), confirm=False)
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    git_in(root, "remote", "add", "origin", str(origin))
    confirm_stack(root)
    git_in(root, "push", "-q", "-u", "origin", "main")
    toml = (root / "kstrl.toml").read_text(encoding="utf-8")
    assert "\n[security]\nmode" not in toml, "the scaffold must not set a security mode"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "gh", FAKE_GH)
    agent = write_executable(tmp_path / "agent.sh", _ONE_AGENT)
    calls = tmp_path / "agent.calls"
    body = tmp_path / "pr-body.md"

    code, out = _spawn(
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(agent)),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--contract-check", "skip"),
        ],
        root,
        {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "GH_BODY": str(body),
            "GH_HEAD": str(tmp_path / "pr-head"),
            "AGENT_CALLS": str(calls),
        },
    )

    assert code == 0, out
    # No [security] setting anywhere, so the startup notes do not claim that
    # kstrl.toml moved the mode off the built-in default.
    assert "[security] mode" not in out, out
    assert calls.read_text(encoding="utf-8").splitlines().count("security") == 1, out
    text = body.read_text(encoding="utf-8")
    assert "- [low] **new_dependency** at `package.json:4`" in text, text
    assert "adds left-pad 1.3.0" in text, text
    assert "UNVERIFIED COVERAGE" not in text, text
