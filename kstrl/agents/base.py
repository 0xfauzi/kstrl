"""Base agent protocol and the tool-output marking."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Final, Protocol

from kstrl.usage import UsageRecord

#: Every line ClaudeCodeAgent or ClaudeSdkAgent yields for a tool's OUTPUT
#: starts with this (#598), and so does every line CodexAgent yields from
#: codex's printed transcript (#640) - NOT every adapter: CustomAgent
#: yields its command's raw stdout with no such marking, so a line that
#: happens to start with two spaces there is the agent's own text.
#: Only an agent whose ``marks_tool_output`` attribute is true tags its
#: lines this way; :func:`model_output_text` is how a consumer that does
#: not know which adapter produced a line of output is meant to read it.
TOOL_RESULT_PREFIX: Final = "  | "


def model_output_lines(lines: Iterable[str]) -> list[str]:
    """``lines`` without the tool-output lines: what the model itself said.

    A low-level filter for an adapter that KNOWS its own lines are tagged
    with :data:`TOOL_RESULT_PREFIX` (ClaudeCodeAgent uses this directly on
    its own accumulated text). A caller reading another agent's lines -
    one it did not itself produce - wants :func:`model_output_text`
    instead, which checks first whether the agent marks tool output at
    all before filtering by the prefix.
    """
    return [line for line in lines if not line.startswith(TOOL_RESULT_PREFIX)]


def model_output_text(agent: object, lines: Iterable[str]) -> str:
    """``lines`` joined as the model's own words, never a tool's output.

    Only an agent whose ``marks_tool_output`` attribute is true (currently
    ClaudeCodeAgent, ClaudeSdkAgent and CodexAgent) tags its tool-output
    lines with :data:`TOOL_RESULT_PREFIX`; every other adapter's lines are
    joined as-is. Filtering unconditionally is the #598 defect this guards
    against: a CustomAgent reply that happens to hold an
    indented markdown table row (one starting with two spaces) had that
    row silently deleted, because nothing marks that line as tool output.
    ``agent`` is read with ``getattr(..., False)``, so a third-party or
    predates-this-attribute Agent implementation degrades to "keep every
    line" rather than raising - the same tolerance :func:`collect_usage`
    gives ``usage_records``.
    """
    kept = list(lines)
    if getattr(agent, "marks_tool_output", False):
        kept = model_output_lines(kept)
    return "\n".join(kept)


class Agent(Protocol):
    """Protocol for kstrl agent implementations."""

    @property
    def name(self) -> str:
        """Human-readable agent name for display."""
        ...

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """Run agent with prompt, yielding output lines.

        Args:
            prompt: The prompt text to send to the agent
            cwd: Working directory for the agent process
            timeout: Optional wall-clock timeout in seconds

        Yields:
            Output lines from the agent (without trailing newlines)
        """
        ...

    @property
    def final_message(self) -> str | None:
        """Return final message if available (for codex --output-last-message)."""
        ...

    @property
    def usage_records(self) -> list[UsageRecord]:
        """Usage records accumulated across ``run`` calls (R3.1).

        One record per ``run`` invocation, appended on every exit path
        (success, timeout, CLI-missing). Consumers must read this via
        :func:`collect_usage`, which tolerates implementations that
        predate the property - the protocol addition is backward-
        compatible at runtime for CustomAgent-style adapters and
        third-party fakes.
        """
        ...
