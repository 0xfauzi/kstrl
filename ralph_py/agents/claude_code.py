"""Claude Code CLI agent for Ralph."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path


class ClaudeCodeAgent:
    """Agent that uses the Claude Code CLI (claude --print)."""

    _supports_stream_json: bool | None = None

    def __init__(self, model: str | None = None):
        """Initialize Claude Code agent.

        Args:
            model: Model name to pass to claude --model
        """
        self._model = model
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        if self._model:
            return f"claude-code ({self._model})"
        return "claude-code"

    @classmethod
    def is_available(cls) -> bool:
        """Check if claude CLI is available."""
        return shutil.which("claude") is not None

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        """Run claude --print with prompt piped to stdin.

        Yields output lines as they arrive.
        """
        self._final_message = None
        last_non_empty_line: str | None = None

        cmd = ["claude", "--print"]
        if self._model:
            cmd.extend(["--model", self._model])

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
            )

            if proc.stdin:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass

            if proc.stdout:
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line.strip():
                        last_non_empty_line = line
                    yield line

            proc.wait()

            if last_non_empty_line:
                self._final_message = last_non_empty_line

        except FileNotFoundError:
            yield "ERROR: claude CLI not found in PATH"

    @property
    def final_message(self) -> str | None:
        """Return last non-empty output line."""
        return self._final_message
