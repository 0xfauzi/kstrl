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
        assert cmd == ["claude", "--print", "--model", "claude-sonnet-4-5"]
        assert call_args[1]["cwd"] == tmp_path

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
