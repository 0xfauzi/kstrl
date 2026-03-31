"""Agent abstraction for Ralph.

Provides a unified interface for invoking AI agents (Claude, Codex, custom)
and classifying their output lines by role.
"""

from __future__ import annotations

import asyncio
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


async def run_agent_async(
    agent_type: str,
    model: str,
    custom_command: str,
    prompt_path: Path,
    cwd: Path,
    reasoning_effort: str = "",
) -> AsyncIterator[AgentOutput]:
    """Run an agent asynchronously, yielding classified output lines.

    The prompt file is piped to the agent's stdin. Output is read line by line
    from stdout+stderr (merged).
    """
    command = build_agent_command(agent_type, model, custom_command)

    # For codex, we need special handling
    if agent_type == "codex":
        # Build the full codex command with all flags
        parts = ["codex", "exec", "-C", str(cwd)]
        if model:
            parts.extend(["-m", model])
        if reasoning_effort:
            parts.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        # The prompt is piped via stdin with "-" flag
        parts.append("-")
        cmd_str = " ".join(parts)
    else:
        cmd_str = command

    # Read the prompt file
    prompt_content = prompt_path.read_text(encoding="utf-8")

    # Run via shell (needed for AGENT_CMD which may contain pipes/redirections)
    process = await asyncio.create_subprocess_shell(
        cmd_str,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )

    # Feed prompt to stdin
    assert process.stdin is not None
    process.stdin.write(prompt_content.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    # Stream output line by line
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
