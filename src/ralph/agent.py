"""Agent abstraction for Ralph.

Provides a unified interface for invoking AI agents (Claude, Codex, custom)
and classifying their output lines by role.

Supports streaming JSON output from Claude for real-time display.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ralph.models import build_agent_command


class LineRole(Enum):
    """Classification of an agent output line."""

    AI = "AI"
    THINK = "THINK"
    TOOL = "TOOL"
    SYS = "SYS"
    PROMPT = "PROMPT"
    GIT = "GIT"
    GUARD = "GUARD"
    USER = "USER"
    UNKNOWN = "UNKNOWN"


# Patterns for classifying codex transcript lines
_ROLE_PATTERNS: list[tuple[re.Pattern[str], LineRole]] = [
    (re.compile(r"^(System|system):"), LineRole.SYS),
    (re.compile(r"^(User|user):"), LineRole.PROMPT),
    (re.compile(r"^(Assistant|assistant):"), LineRole.AI),
    (re.compile(r"^(Thinking|thinking):"), LineRole.THINK),
    (re.compile(r"^(Tool|tool):"), LineRole.TOOL),
]

COMPLETION_MARKER = "<promise>COMPLETE</promise>"


@dataclass
class AgentOutput:
    """A single line of agent output with its classified role."""

    line: str
    role: LineRole


def classify_line(line: str) -> LineRole:
    """Classify an agent output line by its role.

    Detects codex transcript role markers (System:/User:/Assistant:/Thinking:/Tool:)
    and common patterns (git operations, guard messages).
    """
    stripped = line.strip()

    for pattern, role in _ROLE_PATTERNS:
        if pattern.match(stripped):
            return role

    # Detect git operations
    if stripped.startswith("GIT") or "git " in stripped.lower():
        return LineRole.GIT

    # Detect guard/guardrail messages
    if stripped.startswith("GUARD") or "disallowed" in stripped.lower():
        return LineRole.GUARD

    return LineRole.AI


def detect_completion(line: str) -> bool:
    """Check if a line contains the completion marker."""
    return COMPLETION_MARKER in line


# Track last emitted thinking/text to deduplicate accumulated content.
# Claude's stream-json sends the full accumulated content on each event,
# so the same thinking block appears repeatedly as it grows.
_last_thinking: str = ""
_last_ai_text: str = ""


def _parse_stream_json_line(raw: str) -> list[AgentOutput]:
    """Parse a single stream-json line from Claude into AgentOutput(s).

    Returns a list (possibly empty) of outputs. A single JSON event
    can produce multiple display lines (e.g., an assistant message with
    both thinking and text blocks).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [AgentOutput(line=raw, role=classify_line(raw))]

    event_type = data.get("type", "")

    if event_type == "assistant":
        return _parse_assistant_event(data)

    elif event_type == "tool":
        # Tool response - compact summary
        tool_name = data.get("tool_name", "")
        content = str(data.get("content", ""))
        if len(content) > 150:
            summary = f"{tool_name} returned {len(content)} chars"
        else:
            summary = f"{tool_name}: {content[:150]}"
        return [AgentOutput(line=summary, role=LineRole.TOOL)]

    elif event_type == "result":
        cost = data.get("cost_usd", 0)
        duration = data.get("duration_seconds", 0)
        if cost or duration:
            return [AgentOutput(
                line=f"Done (${cost:.4f}, {duration:.1f}s)",
                role=LineRole.SYS,
            )]

    # Skip: user, system, rate_limit_event, unknown
    return []


def _parse_assistant_event(data: dict) -> list[AgentOutput]:
    """Parse an assistant event into one output per content block.

    Deduplicates thinking and AI text blocks since Claude's stream-json
    sends accumulated content on each event (the same block appears
    repeatedly as it grows).
    """
    global _last_thinking, _last_ai_text  # noqa: PLW0603

    message = data.get("message", {})
    content_blocks = message.get("content", [])
    outputs: list[AgentOutput] = []

    for block in content_blocks:
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "").strip()
            if text and text != _last_ai_text:
                # Only emit the new portion
                if _last_ai_text and text.startswith(_last_ai_text):
                    new_part = text[len(_last_ai_text):].strip()
                    if new_part:
                        outputs.append(AgentOutput(
                            line=new_part, role=LineRole.AI,
                        ))
                else:
                    outputs.append(AgentOutput(line=text, role=LineRole.AI))
                _last_ai_text = text

        elif block_type == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking and thinking != _last_thinking:
                # Only emit the new portion of thinking
                if _last_thinking and thinking.startswith(_last_thinking):
                    new_part = thinking[len(_last_thinking):].strip()
                    if new_part:
                        first_line = new_part.split("\n")[0][:200]
                        outputs.append(AgentOutput(
                            line=first_line, role=LineRole.THINK,
                        ))
                else:
                    # First thinking block or completely new
                    first_line = thinking.split("\n")[0][:200]
                    outputs.append(AgentOutput(
                        line=first_line, role=LineRole.THINK,
                    ))
                _last_thinking = thinking

        elif block_type == "tool_use":
            tool_name = block.get("name", "unknown")
            tool_input = block.get("input", {})
            summary = _format_tool_call(tool_name, tool_input)
            outputs.append(AgentOutput(line=summary, role=LineRole.TOOL))

        elif block_type == "tool_result":
            content = str(block.get("content", ""))
            if content and len(content) > 200:
                outputs.append(AgentOutput(
                    line=f"({len(content)} chars)", role=LineRole.TOOL,
                ))

    return outputs


def reset_stream_state() -> None:
    """Reset dedup state between iterations."""
    global _last_thinking, _last_ai_text  # noqa: PLW0603
    _last_thinking = ""
    _last_ai_text = ""


def _format_tool_call(name: str, inputs: dict) -> str:
    """Format a tool call into a compact one-line summary."""
    if name == "Bash":
        cmd = inputs.get("command", "")
        return f"$ {cmd[:140]}" if cmd else "Bash"
    elif name == "Read":
        path = inputs.get("file_path", "")
        return f"Read {_short_path(path)}"
    elif name == "Write":
        path = inputs.get("file_path", "")
        return f"Write {_short_path(path)}"
    elif name == "Edit":
        path = inputs.get("file_path", "")
        return f"Edit {_short_path(path)}"
    elif name == "Glob":
        pattern = inputs.get("pattern", "")
        return f"Glob {pattern[:80]}"
    elif name == "Grep":
        pattern = inputs.get("pattern", "")
        return f"Grep {pattern[:80]}"
    elif name == "Bash" and inputs.get("command"):
        return f"$ {inputs['command'][:140]}"
    else:
        return name


def _short_path(path: str) -> str:
    """Shorten a file path for display (keep last 3 segments)."""
    parts = path.split("/")
    if len(parts) <= 3:
        return path
    return ".../" + "/".join(parts[-3:])


def _extract_recent_handoff(progress_path: Path, max_entries: int = 5) -> str:
    """Extract the last N iteration entries from progress.txt.

    Entries are delimited by lines starting with '## Iteration'.
    Returns just the recent entries, not the file header.
    """
    content = progress_path.read_text(encoding="utf-8")
    if not content.strip():
        return ""

    # Split on iteration headers
    entries: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if line.startswith("## Iteration"):
            if current:
                entries.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append("\n".join(current))

    if not entries:
        return ""

    # Take only the last N entries
    recent = entries[-max_entries:]
    return "\n\n".join(recent)


async def run_agent_async(
    agent_type: str,
    model: str,
    custom_command: str,
    prompt_path: Path,
    cwd: Path,
    reasoning_effort: str = "",
    progress_path: Path | None = None,
    iteration: int = 0,
) -> AsyncIterator[AgentOutput]:
    """Run an agent asynchronously, yielding classified output lines.

    For Claude, uses stream-json format for real-time streaming.
    For Codex and custom agents, reads stdout line-by-line.

    If progress_path is provided and the file exists, its contents are
    appended to the prompt as inter-iteration handoff context.
    """
    # Read the prompt file
    prompt_content = prompt_path.read_text(encoding="utf-8")

    # Inject handoff context from progress.txt (last N entries only)
    if progress_path and progress_path.exists():
        handoff = _extract_recent_handoff(progress_path, max_entries=5)
        if handoff:
            prompt_content += (
                f"\n\n---\n\n"
                f"## Handoff context (iteration {iteration})\n\n"
                f"Below are the handoff notes from the most recent iterations. "
                f"Read them to understand what has been done and what to do next.\n\n"
                f"{handoff}\n"
            )

    if agent_type == "claude":
        async for output in _run_claude_streaming(
            model=model, prompt=prompt_content, cwd=cwd,
        ):
            yield output
    elif agent_type == "codex":
        async for output in _run_codex(
            model=model,
            prompt=prompt_content,
            cwd=cwd,
            reasoning_effort=reasoning_effort,
        ):
            yield output
    else:
        command = build_agent_command(agent_type, model, custom_command)
        async for output in _run_generic(
            command=command, prompt=prompt_content, cwd=cwd,
        ):
            yield output


async def run_conversation_agent(
    model: str,
    prompt: str,
    cwd: Path,
) -> AsyncIterator[AgentOutput]:
    """Run Claude for an interactive planning conversation.

    No file permissions, no verbose hooks - just a conversation.
    """
    async for output in _run_claude_streaming(
        model=model, prompt=prompt, cwd=cwd,
        verbose=False, permission_mode="",
    ):
        yield output


async def run_prd_generation(
    model: str,
    prompt: str,
    json_schema: str,
    cwd: Path,
) -> str:
    """Run Claude with --output-format json --json-schema to generate a PRD.

    Returns the raw JSON output string. The caller should parse it with
    parse_prd_from_json_output() from conversation.py.

    This is a non-streaming call - it waits for the full response.
    """
    effective_model = model or "sonnet"
    # Shell-escape the schema by writing it via stdin along with the prompt
    cmd = (
        f"claude --print --model {effective_model} "
        f"--output-format json "
        f"--json-schema '{json_schema}'"
    )

    process = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=4 * 1024 * 1024,
    )

    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    assert process.stdout is not None
    stdout_bytes = await process.stdout.read()
    await process.wait()

    return stdout_bytes.decode("utf-8", errors="replace")


async def _run_claude_streaming(
    model: str,
    prompt: str,
    cwd: Path,
    verbose: bool = True,
    permission_mode: str = "acceptEdits",
) -> AsyncIterator[AgentOutput]:
    """Run Claude with stream-json output for real-time streaming."""
    effective_model = model or "sonnet"
    cmd_parts = [
        f"claude --print --model {effective_model}",
        "--output-format stream-json",
    ]
    if verbose:
        cmd_parts.append("--verbose")
    if permission_mode:
        cmd_parts.append(f"--permission-mode {permission_mode}")
    cmd = " ".join(cmd_parts)

    # Stream-json lines can be very large (tool results with full file
    # contents). Increase the buffer limit from the default 64KB to 4MB
    # to avoid "Separator is found, but chunk is longer than limit" errors.
    process = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=4 * 1024 * 1024,
    )

    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    assert process.stdout is not None
    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
        raw = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if not raw:
            continue
        for output in _parse_stream_json_line(raw):
            yield output

    # Check stderr for errors
    assert process.stderr is not None
    stderr_bytes = await process.stderr.read()
    if stderr_bytes:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            yield AgentOutput(line=f"stderr: {stderr_text[:200]}", role=LineRole.SYS)

    await process.wait()


async def _run_codex(
    model: str,
    prompt: str,
    cwd: Path,
    reasoning_effort: str = "",
) -> AsyncIterator[AgentOutput]:
    """Run Codex agent with line-by-line output."""
    parts = ["codex", "exec", "-C", str(cwd), "--full-auto"]
    if model:
        parts.extend(["-m", model])
    if reasoning_effort:
        parts.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    parts.append("-")
    cmd_str = " ".join(parts)

    process = await asyncio.create_subprocess_shell(
        cmd_str,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )

    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    assert process.stdout is not None
    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        role = classify_line(line)
        yield AgentOutput(line=line, role=role)

    await process.wait()


async def _run_generic(
    command: str,
    prompt: str,
    cwd: Path,
) -> AsyncIterator[AgentOutput]:
    """Run a generic/custom agent with line-by-line output."""
    process = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )

    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    assert process.stdout is not None
    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        role = classify_line(line)
        yield AgentOutput(line=line, role=role)

    await process.wait()


def run_agent_sync(
    agent_type: str,
    model: str,
    custom_command: str,
    prompt_path: Path,
    cwd: Path,
    reasoning_effort: str = "",
) -> tuple[list[AgentOutput], bool]:
    """Run an agent synchronously. Returns (output_lines, completed).

    This is a convenience wrapper for non-async contexts (CLI headless mode).
    """
    import subprocess

    command = build_agent_command(agent_type, model, custom_command)

    if agent_type == "codex":
        parts = ["codex", "exec", "-C", str(cwd)]
        if model:
            parts.extend(["-m", model])
        if reasoning_effort:
            parts.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        parts.append("-")
        cmd_str = " ".join(parts)
    else:
        cmd_str = command

    prompt_content = prompt_path.read_text(encoding="utf-8")

    result = subprocess.run(
        cmd_str,
        shell=True,
        input=prompt_content,
        capture_output=True,
        text=True,
        cwd=cwd,
    )

    output = result.stdout + result.stderr
    lines: list[AgentOutput] = []
    completed = False

    for raw_line in output.splitlines():
        role = classify_line(raw_line)
        lines.append(AgentOutput(line=raw_line, role=role))
        if detect_completion(raw_line):
            completed = True

    return lines, completed
