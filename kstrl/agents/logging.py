"""Agent wrapper that tees streamed output to a log file.

Moved from cli.py (TUI surface C2) so command cores outside the CLI
module can wrap agents; behavior unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from kstrl.agents.base import Agent, UsageRecord


class LoggingAgent:
    """Agent wrapper that appends streamed output to a log file."""

    def __init__(self, agent: Agent, log_path: Path) -> None:
        self._agent = agent
        self._log_path = log_path

    @property
    def name(self) -> str:
        return self._agent.name

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as handle:
            for line in self._agent.run(prompt, cwd, timeout):
                handle.write(f"{line}\n")
                handle.flush()
                yield line

    @property
    def final_message(self) -> str | None:
        return self._agent.final_message

    @property
    def usage_records(self) -> list[UsageRecord]:
        """R3.1: forward the wrapped agent's usage records."""
        records = getattr(self._agent, "usage_records", None)
        return list(records) if records is not None else []
