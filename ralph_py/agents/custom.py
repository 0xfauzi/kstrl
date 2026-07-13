"""Custom command agent for Ralph."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

from ralph_py.agents.proc import DeadlineStreamer, timeout_message


class CustomAgent:
    """Agent that runs a custom shell command."""

    def __init__(self, command: str):
        """Initialize with command string.

        Args:
            command: Shell command to run. Prompt is piped to stdin.
        """
        self._command = command
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        return f"custom ({self._command})"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        """Run command with prompt piped to stdin.

        Yields output lines as they arrive. When ``timeout`` is set and the
        command hangs (with or without output), its process group is killed
        and a timeout error line is yielded last.
        """
        self._final_message = None

        use_bash = shutil.which("bash") is not None
        if use_bash:
            cmd: str | list[str] = ["bash", "-lc", self._command]
        else:
            # Fallback to /bin/sh when bash is unavailable.
            cmd = self._command

        streamer = DeadlineStreamer(
            cmd,
            shell=not use_bash,
            cwd=cwd,
            stdin_text=prompt,
            timeout=timeout,
        )

        output_lines: list[str] = []
        for line in streamer.lines():
            output_lines.append(line)
            yield line

        if streamer.timed_out:
            # Killed mid-run: partial output is not a trustworthy final
            # message, so leave it unset.
            yield timeout_message(timeout)
            return

        streamer.finish()

        # Store last output as "final message" for consistency
        if output_lines:
            self._final_message = output_lines[-1]

    @property
    def final_message(self) -> str | None:
        """Return last output line."""
        return self._final_message
