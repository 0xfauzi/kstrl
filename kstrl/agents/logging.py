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
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8 pinned, and this one was measured breaking: what goes
        # through here is raw agent output, so one accented character in
        # a model's reply raised UnicodeEncodeError and killed the run
        # mid-stream. #320's rule read from the write side: kstrl must
        # not become the source of locale-encoded bytes its own readers
        # then cannot decode.
        #
        # The env that reproduces it is LC_ALL=C PYTHONUTF8=0, not
        # LC_ALL=C alone: on CPython 3.12 a C locale turns PEP 540 UTF-8
        # mode ON, so the write succeeds there and only the second
        # variable actually removes the utf-8 default. #344's review
        # caught the first version of this comment naming the env that
        # does not reproduce. tests/test_encoding_sites.py drives it.
        with self._log_path.open("a", encoding="utf-8") as handle:
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
