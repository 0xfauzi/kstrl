"""Tests for timeout module."""

from __future__ import annotations

import subprocess

import pytest

from kstrl.timeout import TimeoutConfig, run_with_timeout


class TestTimeoutConfig:
    def test_defaults(self) -> None:
        config = TimeoutConfig()
        assert config.git_operation == 30.0
        assert config.agent_iteration == 1800.0
        assert config.component_total == 7200.0

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RALPH_TIMEOUT_GIT", "10")
        monkeypatch.setenv("RALPH_TIMEOUT_AGENT_ITERATION", "900")
        config = TimeoutConfig.from_env()
        assert config.git_operation == 10.0
        assert config.agent_iteration == 900.0


class TestRunWithTimeout:
    def test_success(self, tmp_path) -> None:
        result = run_with_timeout(["echo", "hello"], timeout=5.0, cwd=tmp_path)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_failure(self, tmp_path) -> None:
        result = run_with_timeout(["false"], timeout=5.0, cwd=tmp_path)
        assert result.returncode != 0

    def test_timeout_raises(self, tmp_path) -> None:
        with pytest.raises(subprocess.TimeoutExpired):
            run_with_timeout(["sleep", "10"], timeout=0.1, cwd=tmp_path)

    def test_shell_mode(self, tmp_path) -> None:
        result = run_with_timeout("echo hello", timeout=5.0, cwd=tmp_path, shell=True)
        assert result.returncode == 0
        assert "hello" in result.stdout
