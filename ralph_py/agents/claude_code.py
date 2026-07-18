"""Claude Code CLI agent for Ralph."""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ralph_py.agents.base import UsageRecord
from ralph_py.agents.proc import DeadlineStreamer, timeout_message

logger = logging.getLogger(__name__)

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
        self._saw_result: bool = False
        self._usage_records: list[UsageRecord] = []

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
        When ``timeout`` is set and the CLI hangs (with or without output),
        its process group is killed and a timeout error line is yielded last.
        """
        self._final_message = None
        self._saw_result = False
        accumulated_text: list[str] = []
        started = time.monotonic()
        result_event_line: str | None = None

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
            streamer = DeadlineStreamer(
                cmd, cwd=cwd, stdin_text=prompt, timeout=timeout,
            )
        except FileNotFoundError:
            self._usage_records.append(UsageRecord(source="unavailable"))
            yield "ERROR: claude CLI not found in PATH"
            return

        for raw_line in streamer.lines():
            if not raw_line.strip():
                continue

            # Check for result event (final output, process should exit soon)
            result_text = _extract_result_text(raw_line)
            if result_text is not None:
                self._saw_result = True
                self._final_message = result_text
                # The same event carries the usage/cost self-report
                # (measured against claude CLI 2.1.214; see base.py).
                result_event_line = raw_line
                break

            # Parse stream-json events
            for display_line in _parse_stream_event(raw_line):
                if display_line.strip():
                    accumulated_text.append(display_line)
                yield display_line

        if streamer.timed_out:
            self._usage_records.append(UsageRecord(
                duration_seconds=time.monotonic() - started,
                source="timeout",
            ))
            yield timeout_message(timeout)
            return

        # Wait for process to exit, but don't hang forever
        streamer.finish(timeout=10)

        self._usage_records.append(_usage_from_result_event(
            result_event_line, time.monotonic() - started,
        ))

        # Set final_message from accumulated text if not already set by result event
        if self._final_message is None and accumulated_text:
            self._final_message = accumulated_text[-1]

    @property
    def final_message(self) -> str | None:
        """Return last non-empty output line."""
        return self._final_message

    @property
    def usage_records(self) -> list[UsageRecord]:
        """One usage record per ``run`` call, accumulated (R3.1)."""
        return list(self._usage_records)


def _usage_from_result_event(
    raw_line: str | None, fallback_duration: float,
) -> UsageRecord:
    """Build a UsageRecord from the stream-json ``result`` event.

    Measured shape (claude CLI 2.1.214): the event carries a ``usage``
    dict (``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
    ``cache_creation_input_tokens``), a ``total_cost_usd`` float, and
    ``duration_ms``. Formats drift across CLI versions, so every field
    is optional: a parse failure logs a warning and records unknown -
    the meter never gates correctness (R3.1 requirement 4).
    """
    duration = fallback_duration
    if raw_line is None:
        # Stream ended without a result event (e.g. CLI died early or a
        # non-stream-json fake in tests): calls + wall time only.
        return UsageRecord(duration_seconds=duration, source="unavailable")
    try:
        evt = json.loads(raw_line)
        duration_ms = evt.get("duration_ms")
        if (
            isinstance(duration_ms, (int, float))
            and not isinstance(duration_ms, bool)
            and duration_ms >= 0
        ):
            duration = float(duration_ms) / 1000.0

        usage = evt.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        def _tok(key: str) -> int | None:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value if value >= 0 else None

        input_tokens = _tok("input_tokens")
        output_tokens = _tok("output_tokens")
        cache_read = _tok("cache_read_input_tokens")
        cache_creation = _tok("cache_creation_input_tokens")

        cost_raw = evt.get("total_cost_usd")
        cost: float | None = None
        if (
            isinstance(cost_raw, (int, float))
            and not isinstance(cost_raw, bool)
            and cost_raw >= 0
        ):
            cost = float(cost_raw)

        parts = [
            p for p in (input_tokens, output_tokens, cache_read, cache_creation)
            if p is not None
        ]
        if not parts and cost is None:
            logger.warning(
                "claude result event carried no parsable usage fields; "
                "recording unknown usage for this call"
            )
            return UsageRecord(duration_seconds=duration, source="parse-error")

        return UsageRecord(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            total_tokens=sum(parts) if parts else None,
            cost_usd=cost,
            duration_seconds=duration,
            source="claude-stream-json",
        )
    except Exception as exc:  # noqa: BLE001 - meter must never crash a run
        logger.warning("Failed to parse claude usage: %s", exc)
        return UsageRecord(
            duration_seconds=fallback_duration, source="parse-error",
        )


def _extract_result_text(raw_line: str) -> str | None:
    """Extract final result text from a stream-json result event.

    Returns the result text if this is a result event, None otherwise.
    The result event is the last event in a Claude Code stream-json
    session and signals that the process is about to exit.
    """
    try:
        evt = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if evt.get("type") == "result":
        result = evt.get("result", "")
        return str(result) if result is not None else None
    return None


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


def _format_tool_use(tool_name: str, tool_input: dict[str, Any]) -> Iterator[str]:
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
