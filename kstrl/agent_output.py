"""Drain one agent output under a byte cap and a time limit, and take the JSON reply."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kstrl.agents.base import model_output_text
from kstrl.jsonread import read_json
from kstrl.usage import timed_out_since, usage_cursor


def extract_json(text: str) -> Any:
    """Extract JSON from text, handling optional code fences."""
    # Try direct parse first
    stripped = text.strip()
    try:
        return read_json(stripped)
    except json.JSONDecodeError:
        pass

    # Try extracting from code fences
    fence_pattern = r"```(?:json)?\s*\n(.*?)\n```"
    matches = re.findall(fence_pattern, stripped, re.DOTALL)
    for match in matches:
        try:
            return read_json(match.strip())
        except json.JSONDecodeError:
            continue

    # Try finding JSON object boundaries
    brace_start = stripped.find("{")
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(stripped)):
            if stripped[i] == "{":
                depth += 1
            elif stripped[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return read_json(stripped[brace_start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError("No valid JSON found in output")


# Hard cap on agent stream output. A pathological or compromised agent
# could emit unbounded data; this guards against memory blowup and
# downstream prompt-context flooding. 5MB is generous - real reviewer
# / distiller / decompose responses are well under 100KB.
MAX_AGENT_OUTPUT_BYTES = 5 * 1024 * 1024


class AgentOutputTooLarge(RuntimeError):
    """Raised when an agent emits more than MAX_AGENT_OUTPUT_BYTES of
    streamed output. Callers should treat this as an infrastructure
    failure (the agent likely misbehaved) and fail loudly in strict
    modes, advisory in soft modes."""


class AgentTimedOut(RuntimeError):
    """Raised when the adapter killed the agent at its deadline.

    The lines the agent printed before the kill are never returned, so no
    role can read a killed call's reply as a verdict (#603). Every caller
    of :func:`collect_agent_output` already turns a ``RuntimeError`` into
    an infrastructure failure whose text carries this message.
    """


def timed_out_message(timeout: float | None) -> str:
    """What a killed call's failure says, for every role."""
    return f"the agent timed out after {timeout}s and was stopped; its reply was not read"


def collect_agent_output(
    agent: Any,
    prompt: str,
    cwd: Path | None = None,
    timeout: float | None = None,
    *,
    max_bytes: int = MAX_AGENT_OUTPUT_BYTES,
    on_line: Callable[[str], None] | None = None,
) -> list[str]:
    """Drain ``agent.run(...)`` into a list, aborting if total bytes
    exceed ``max_bytes``.

    ``on_line`` observes each streamed line as it arrives (phase
    transcripts, TUI rewrite chunk 4). An observer failure disables the
    observer for the rest of the run - a dead transcript file must
    never abort an agent call.

    Raises :class:`AgentOutputTooLarge` when the cap is hit, and
    :class:`AgentTimedOut` when the adapter killed the agent at
    ``timeout``. Callers are expected to catch both and translate them to
    their phase-specific failure mode.
    """
    output_lines: list[str] = []
    total_bytes = 0
    cursor = usage_cursor(agent)
    for line in agent.run(prompt, cwd=cwd, timeout=timeout):
        output_lines.append(line)
        if on_line is not None:
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 - transcripts never gate
                on_line = None
        total_bytes += len(line) + 1  # +1 for the implicit newline
        if total_bytes > max_bytes:
            raise AgentOutputTooLarge(
                f"Agent output exceeded {max_bytes // 1024 // 1024}MB cap "
                f"(>{total_bytes} bytes, {len(output_lines)} lines)"
            )
    if timed_out_since(agent, cursor):
        raise AgentTimedOut(timed_out_message(timeout))
    return output_lines


def select_agent_output(agent: Any, output_lines: list[str]) -> str:
    """Return the best text candidate for JSON extraction from a finished
    agent run.

    Returns :attr:`agent.final_message` if it contains parseable JSON;
    otherwise returns the joined streamed output. This shields callers
    that pass the result through :func:`extract_json` (e.g. via a
    domain-specific parser) from codex's prompt-echo behavior.
    """
    streamed = model_output_text(agent, output_lines)
    final = getattr(agent, "final_message", None)
    if not final:
        return streamed
    final_str = str(final)
    try:
        extract_json(final_str)
    except ValueError:
        return streamed
    return final_str


def extract_agent_json(agent: Any, output_lines: list[str]) -> Any:
    """Extract JSON from a completed agent run, trying agent.final_message
    first and falling back to the streamed output.

    Codex CLI (and other agents that echo the input prompt back) include
    the JSON schema example inside their stdout, which can trip the
    first-brace heuristic in :func:`extract_json`. ``agent.final_message``
    is populated by codex via ``--output-last-message`` and by
    ClaudeCodeAgent from its result event, and contains only the model's
    actual reply. Preferring it sidesteps the echoed-prompt problem.

    For CustomAgent (whose final_message is just the last non-empty line
    of streamed output), the multi-line JSON case is handled by the
    streamed-output fallback when final_message fails to parse.

    Raises :class:`ValueError` if neither candidate parses.
    """
    streamed = model_output_text(agent, output_lines)
    final = getattr(agent, "final_message", None)

    candidates: list[str] = []
    if final:
        candidates.append(final)
    if streamed and streamed != final:
        candidates.append(streamed)

    last_error: ValueError | None = None
    for candidate in candidates:
        try:
            return extract_json(candidate)
        except ValueError as exc:
            last_error = exc

    if last_error is None:
        raise ValueError("No agent output to parse")
    raise last_error
