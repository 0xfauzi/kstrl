"""R2.6: HITL checkpoint semantics + verification subprocess hygiene.

Three defect classes, three test groups:

1. E6 checkpoint: Reject must mark the component FAILED immediately
   (cascade-skipping dependents) with zero further agent calls and zero
   re-prompts; Retry is a separate explicit choice that consumes a retry.
2. Env scrub: every verification subprocess runs under an allowlist env,
   so agent-authored test code can never read ANTHROPIC_API_KEY /
   OPENAI_API_KEY / any other harness secret - while `uv run` still
   functions under the scrub.
3. Process groups: verification subprocesses launch with
   ``start_new_session=True`` and a timeout kills the whole group, so a
   test that backgrounds a server cannot leak it across iterations.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_py.config import RalphConfig
from ralph_py.factory import ComponentResult, FactoryConfig, run_factory
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import (
    VerifyConfig,
    check_test_suite,
    run_scrubbed,
    scrubbed_subprocess_env,
)


class ScriptedUI(PlainUI):
    """PlainUI that reports as interactive and answers with scripted choices."""

    def __init__(self, choices: list[int]) -> None:
        super().__init__(no_color=True)
        self._choices = choices
        self.choose_calls: list[str] = []

    def can_prompt(self) -> bool:
        return True

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        self.choose_calls.append(header)
        return self._choices.pop(0)


def _scaffold(
    tmp_path: Path, comp_ids: list[str], deps: dict[str, list[str]],
) -> tuple[Manifest, RalphConfig]:
    scaffold = tmp_path / "scripts" / "ralph"
    scaffold.mkdir(parents=True)
    (scaffold / "prompt.md").write_text("p")
    (scaffold / "prd.json").write_text(
        '{"branchName": "t", "userStories": []}'
    )
    components: list[Component] = []
    for cid in comp_ids:
        feature_dir = scaffold / "feature" / cid
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "t",
            "userStories": [{
                "id": "US-1", "title": "t", "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
        components.append(Component(
            id=cid, title=cid, description="",
            dependencies=deps.get(cid, []),
            prd_path=f"scripts/ralph/feature/{cid}/prd.json",
            branch_name=f"ralph/{cid}",
        ))
    manifest = Manifest(
        version="1", spec_file="s", project_name="t",
        base_branch="main", single_pr=False, components=components,
    )
    base = RalphConfig(
        prompt_file=scaffold / "prompt.md",
        prd_file=scaffold / "prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )
    return manifest, base


def _checkpoint_config(max_retries: int = 3) -> FactoryConfig:
    return FactoryConfig(
        use_worktrees=False, create_prs=True, max_parallel=1,
        review_mode="skip", pause_before_pr_merge=True,
        max_retries=max_retries, retry_delay=0.0,
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=10.0,
        ),
    )


class TestHitlCheckpoint:
    def test_reject_fails_immediately_zero_further_agent_calls(
        self, tmp_path: Path,
    ) -> None:
        """Reject = FAILED now: one agent run total, one prompt total,
        dependents cascade-skipped, no retry consumed."""
        manifest, base = _scaffold(
            tmp_path, ["comp-a", "comp-b"], {"comp-b": ["comp-a"]},
        )
        ui = ScriptedUI(choices=[1])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ) as fake_agent, patch(
            "ralph_py.pr.is_gh_available", return_value=False,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _checkpoint_config(), base, ui, tmp_path,
            )
        assert fake_agent.call_count == 1
        assert len(ui.choose_calls) == 1
        assert result.failed == ["comp-a"]
        assert "comp-b" in result.skipped
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert comp_a.error == "Rejected at HITL checkpoint"
        assert comp_a.retries == 0

    def test_retry_choice_consumes_a_retry_and_reruns(
        self, tmp_path: Path,
    ) -> None:
        """Retry is explicit: the agent runs again, one retry is spent,
        and a second Approve completes the component."""
        manifest, base = _scaffold(tmp_path, ["comp-a"], {})
        ui = ScriptedUI(choices=[2, 0])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ) as fake_agent, patch(
            "ralph_py.pr.is_gh_available", return_value=False,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _checkpoint_config(max_retries=1), base, ui,
                tmp_path,
            )
        assert fake_agent.call_count == 2
        assert len(ui.choose_calls) == 2
        assert "comp-a" in result.completed
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.retries == 1

    def test_approve_proceeds_without_retry_or_failure(
        self, tmp_path: Path,
    ) -> None:
        manifest, base = _scaffold(tmp_path, ["comp-a"], {})
        ui = ScriptedUI(choices=[0])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ) as fake_agent, patch(
            "ralph_py.pr.is_gh_available", return_value=False,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _checkpoint_config(), base, ui, tmp_path,
            )
        assert fake_agent.call_count == 1
        assert "comp-a" in result.completed
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.retries == 0


class TestScrubbedEnv:
    def test_allowlist_passes_and_secrets_drop(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
        # Allowed prefix carrying a sensitive name must still be dropped.
        monkeypatch.setenv("UV_PUBLISH_TOKEN", "uv-secret")
        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.setenv("UV_CACHE_DIR", "/tmp/uvcache")
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        env = scrubbed_subprocess_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert "UV_PUBLISH_TOKEN" not in env
        assert env["LC_ALL"] == "C"
        assert env["UV_CACHE_DIR"] == "/tmp/uvcache"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert "PATH" in env
        assert "HOME" in env
        forbidden = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
        assert not any(
            frag in name for name in env for frag in forbidden
        )

    def test_subprocess_sees_no_api_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-secret")
        result = run_scrubbed(
            [
                sys.executable, "-c",
                "import os, json; print(json.dumps(dict(os.environ)))",
            ],
            cwd=tmp_path, timeout=30,
        )
        assert result.returncode == 0
        child_env = json.loads(result.stdout)
        assert "ANTHROPIC_API_KEY" not in child_env
        assert "OPENAI_API_KEY" not in child_env
        assert not any("API_KEY" in name for name in child_env)

    @pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
    def test_uv_run_functions_under_scrub(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """uv itself must keep working with only the allowlist env, and
        the interpreter it launches must not see the harness's keys."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-secret")
        code = "import os, json; print(json.dumps(dict(os.environ)))"
        result = run_scrubbed(
            f'uv run --no-project python -c "{code}"',
            cwd=tmp_path, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        child_env = json.loads(result.stdout.strip().splitlines()[-1])
        assert not any("API_KEY" in name for name in child_env)


def _assert_process_dies(pid: int, deadline_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} still alive after group kill")


class TestProcessGroupKill:
    def test_child_runs_in_its_own_process_group(
        self, tmp_path: Path,
    ) -> None:
        result = run_scrubbed(
            [
                sys.executable, "-c",
                "import os; print(os.getpgrp() == os.getpid())",
            ],
            cwd=tmp_path, timeout=30,
        )
        assert result.stdout.strip() == "True"

    def test_timeout_kills_backgrounded_grandchild(
        self, tmp_path: Path,
    ) -> None:
        """A backgrounded grandchild (the leaked-server shape) dies with
        the group when the deadline fires."""
        pid_file = tmp_path / "grandchild.pid"
        cmd = f"sleep 300 & echo $! > {pid_file}; wait"
        with pytest.raises(subprocess.TimeoutExpired):
            run_scrubbed(cmd, cwd=tmp_path, timeout=1.0, term_grace=1.0)
        pid = int(pid_file.read_text().strip())
        _assert_process_dies(pid)

    def test_check_test_suite_timeout_kills_grandchild(
        self, tmp_path: Path,
    ) -> None:
        """Same guarantee through a real verification entry point."""
        pid_file = tmp_path / "server.pid"
        cmd = f"sleep 300 & echo $! > {pid_file}; wait"
        result = check_test_suite(tmp_path, command=cmd, timeout=1.0)
        assert result.passed is False
        assert "timed out" in result.message
        pid = int(pid_file.read_text().strip())
        _assert_process_dies(pid)
