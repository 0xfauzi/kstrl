"""Claude Code CLI agent for Ralph."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path


COMPLETION_MARKER = "<promise>COMPLETE</promise>"


class ClaudeCodeAgent:
    """Agent that uses the Claude Code CLI (claude --print).

    Uses --output-format stream-json --verbose to stream real-time events
    so tool calls (file reads, edits, bash commands) are visible during
    execution rather than only showing the final text response.
    """

    def __init__(self, model: str | None = None, effort: str | None = None):
        """Initialize Claude Code agent.

        Args:
            model: Model name to pass to claude --model
            effort: Reasoning effort level (low, medium, high, max)
        """
        self._model = model
        self._effort = effort
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

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        """Run claude --print with prompt piped to stdin.

        Uses stream-json output format to capture tool calls in real-time.
        Yields human-readable lines describing what the agent is doing.
        """
        self._final_message = None
        accumulated_text: list[str] = []
        start = time.monotonic()

        cmd = [
            "claude", "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self._model:
            cmd.extend(["--model", self._model])
        if self._effort:
            cmd.extend(["--effort", self._effort])

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
                for raw_line in proc.stdout:
                    if timeout and (time.monotonic() - start) > timeout:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        yield f"ERROR: agent timed out after {timeout}s"
                        return

                    raw_line = raw_line.rstrip("\n")
                    if not raw_line.strip():
                        continue

                    # Parse stream-json events
                    for display_line in _parse_stream_event(raw_line):
                        if display_line.strip():
                            accumulated_text.append(display_line)
                        yield display_line

            proc.wait()

            # Set final_message to the last result text
            if accumulated_text:
                self._final_message = accumulated_text[-1]

        except FileNotFoundError:
            yield "ERROR: claude CLI not found in PATH"

    @property
    def final_message(self) -> str | None:
        """Return last non-empty output line."""
        return self._final_message


def _parse_stream_event(raw_line: str) -> Iterator[str]:
    """Parse a single stream-json event line into human-readable output.

    Extracts tool calls, tool results, and text content from the JSON
    event stream so the ralph UI can display agent progress in real-time.
    """
    try:
        evt = json.loads(raw_line)
    except json.JSONDecodeError:
        # Not JSON - yield as-is (shouldn't happen with stream-json)
        yield raw_line
        return

    event_type = evt.get("type", "")

    if event_type == "assistant":
        # Assistant message with content blocks (tool_use or text)
        message = evt.get("message", {})
        for block in message.get("content", []):
            block_type = block.get("type", "")
            if block_type == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})
                yield from _format_tool_use(tool_name, tool_input)
            elif block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    yield text

    elif event_type == "tool_result":
        # Tool result - show a summary, not the full content
        content = evt.get("content", "")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    # Only show first 200 chars of tool results to avoid flooding
                    if len(text) > 200:
                        text = text[:200] + "..."
                    if text.strip():
                        yield text
        elif isinstance(content, str) and content.strip():
            text = content[:200] + "..." if len(content) > 200 else content
            yield text

    # Skip result (duplicates assistant text), system, rate_limit_event, etc.


def _format_tool_use(tool_name: str, tool_input: dict) -> Iterator[str]:
    """Format a tool_use block into a concise human-readable line."""
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        # Show just the filename, not the full path
        short = path.split("/")[-1] if "/" in path else path
        yield f"[Read] {short}"

    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        short = path.split("/")[-1] if "/" in path else path
        yield f"[Edit] {short}"

    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        short = path.split("/")[-1] if "/" in path else path
        yield f"[Write] {short}"

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        # Truncate long commands
        if len(command) > 120:
            command = command[:120] + "..."
        yield f"[Bash] {command}"

    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        yield f"[Glob] {pattern}"

    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        yield f"[Grep] {pattern}"

    elif tool_name == "TodoWrite":
        # Skip noisy todo updates
        pass

    else:
        # Generic tool display
        summary = json.dumps(tool_input)
        if len(summary) > 100:
            summary = summary[:100] + "..."
        yield f"[{tool_name}] {summary}"
