"""Tests for Claude Code agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ralph_py.agents.claude_code import ClaudeCodeAgent


class TestClaudeCodeAgent:
    """Tests for ClaudeCodeAgent."""

    def test_name_default(self) -> None:
        agent = ClaudeCodeAgent()
        assert agent.name == "claude-code"

    def test_name_with_model(self) -> None:
        agent = ClaudeCodeAgent(model="claude-sonnet-4-5")
        assert "claude-sonnet-4-5" in agent.name

    def test_is_available_found(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/claude"):
            assert ClaudeCodeAgent.is_available() is True

    def test_is_available_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            assert ClaudeCodeAgent.is_available() is False

    def test_run_yields_output(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter(["hello\n", "world\n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            lines = list(agent.run("test prompt", cwd=tmp_path))

        assert lines == ["hello", "world"]
        assert agent.final_message == "world"

    def test_run_builds_correct_command(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            agent = ClaudeCodeAgent(model="claude-sonnet-4-5")
            list(agent.run("test", cwd=tmp_path))

        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "claude" == cmd[0]
        assert "--print" in cmd
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-sonnet-4-5"
        assert "--output-format" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--effort" not in cmd  # no effort by default
        assert call_args[1]["cwd"] == tmp_path

    def test_run_includes_effort_flag(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            agent = ClaudeCodeAgent(model="sonnet", effort="max")
            list(agent.run("test", cwd=tmp_path))

        cmd = mock_popen.call_args[0][0]
        assert "--effort" in cmd
        effort_idx = cmd.index("--effort")
        assert cmd[effort_idx + 1] == "max"

    def test_run_omits_effort_when_empty(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            agent = ClaudeCodeAgent(model="sonnet", effort=None)
            list(agent.run("test", cwd=tmp_path))

        cmd = mock_popen.call_args[0][0]
        assert "--effort" not in cmd

    def test_run_handles_broken_pipe(self, tmp_path: Path) -> None:
        mock_stdin = MagicMock()
        mock_stdin.write.side_effect = BrokenPipeError

        mock_proc = MagicMock()
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = iter(["partial output\n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            lines = list(agent.run("test", cwd=tmp_path))

        assert lines == ["partial output"]

    def test_run_handles_not_found(self, tmp_path: Path) -> None:
        with patch("subprocess.Popen", side_effect=FileNotFoundError):
            agent = ClaudeCodeAgent()
            lines = list(agent.run("test", cwd=tmp_path))

        assert len(lines) == 1
        assert "not found" in lines[0]

    def test_final_message_none_before_run(self) -> None:
        agent = ClaudeCodeAgent()
        assert agent.final_message is None

    def test_final_message_skips_empty_lines(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter(["content\n", "\n", "  \n"])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            list(agent.run("test", cwd=tmp_path))

        assert agent.final_message == "content"

    def test_result_event_breaks_stdout_loop(self, tmp_path: Path) -> None:
        """When a result event arrives, stop reading stdout and set final_message."""
        import json

        assistant_event = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working..."}]},
        })
        result_event = json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "All done",
            "duration_ms": 1000,
        })
        # Lines after result should never be read
        unreachable = "THIS SHOULD NOT BE YIELDED\n"

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter([
            assistant_event + "\n",
            result_event + "\n",
            unreachable,
        ])
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            lines = list(agent.run("test", cwd=tmp_path))

        # Should see the assistant text but not the unreachable line
        assert "working..." in lines
        assert "THIS SHOULD NOT BE YIELDED" not in lines
        assert agent.final_message == "All done"

    def test_proc_wait_timeout_terminates(self, tmp_path: Path) -> None:
        """If proc.wait times out after result, terminate the process."""
        import json
        import subprocess

        result_event = json.dumps({
            "type": "result",
            "result": "done",
        })

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter([result_event + "\n"])
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired("claude", 10),  # first wait times out
            0,  # wait after terminate succeeds
        ]
        mock_proc.terminate = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            agent = ClaudeCodeAgent()
            list(agent.run("test", cwd=tmp_path))

        mock_proc.terminate.assert_called_once()
        assert agent.final_message == "done"
