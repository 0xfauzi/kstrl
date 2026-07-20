"""Base agent protocol and usage metering types for Ralph."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageRecord:
    """Token/cost usage for ONE agent invocation (one ``run`` call).

    R3.1 cost meter. Every field except ``duration_seconds`` and
    ``source`` is a CLI self-report parsed from agent output; ``None``
    means "the CLI did not report it", never zero. These are hints for
    accounting - they must never gate correctness (a parse failure
    produces an all-``None`` record, not an exception).

    Measured emission formats (2026-07-18, see R3.1 PR):
    - claude CLI 2.1.214 stream-json ``result`` event: ``usage``
      (``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
      ``cache_creation_input_tokens``), ``total_cost_usd``,
      ``duration_ms``.
    - codex CLI 0.134.0 plain output: a trailing ``tokens used`` /
      ``14,511`` line pair - TOTAL tokens only, no in/out split, no cost.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    # CLI-reported total where only a total exists (codex); for claude
    # the adapter derives it as the sum of the four component fields.
    total_tokens: int | None = None
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    # Provenance: "claude-stream-json", "codex-text", "unavailable",
    # "timeout", or "parse-error".
    source: str = "unavailable"


@dataclass
class UsageTotals:
    """Aggregate of UsageRecords (per phase, per component, or per run).

    ``known_calls`` counts invocations that reported at least one token
    or cost figure; ``calls - known_calls`` invocations contributed only
    wall time, so every token/cost total is a LOWER BOUND whenever
    ``unreported_calls > 0`` (H4: totals are only as honest as their
    coverage, and the rollup renders that gap explicitly).
    """

    calls: int = 0
    known_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0

    @property
    def unreported_calls(self) -> int:
        """Invocations that reported no token/cost data at all."""
        return self.calls - self.known_calls

    def add_record(self, record: object) -> None:
        """Fold one usage record into the totals.

        Defensive by design (R3.1 requirement 4): ``record`` is read
        via ``getattr`` with per-field type checks so a malformed or
        foreign object degrades to "one call, nothing reported" instead
        of raising.
        """
        self.calls += 1
        known = False
        token_fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
        part_sum = 0
        parts_seen = False
        for name in token_fields:
            value = _as_int(getattr(record, name, None))
            if value is not None:
                setattr(self, name, getattr(self, name) + value)
                part_sum += value
                parts_seen = True
                known = True
        total = _as_int(getattr(record, "total_tokens", None))
        if total is not None:
            self.total_tokens += total
            known = True
        elif parts_seen:
            self.total_tokens += part_sum
        cost = _as_float(getattr(record, "cost_usd", None))
        if cost is not None:
            self.cost_usd += cost
            known = True
        duration = _as_float(getattr(record, "duration_seconds", None))
        if duration is not None:
            self.duration_seconds += duration
        if known:
            self.known_calls += 1

    def merge(self, other: UsageTotals) -> None:
        """Fold another totals object into this one."""
        self.calls += other.calls
        self.known_calls += other.known_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.total_tokens += other.total_tokens
        self.cost_usd += other.cost_usd
        self.duration_seconds += other.duration_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serializable form for the progress log / journal / TSV."""
        return {
            "calls": self.calls,
            "known_calls": self.known_calls,
            "unreported_calls": self.unreported_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "duration_seconds": round(self.duration_seconds, 2),
        }


def _as_int(value: object) -> int | None:
    """Non-negative int or None. bool is rejected (it is an int subclass
    and a malformed ``usage`` dict could carry flags where counts go)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _as_float(value: object) -> float | None:
    """Non-negative float or None (accepts ints, rejects bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def collect_usage(agent: object) -> UsageTotals:
    """Aggregate an agent's accumulated ``usage_records`` defensively.

    Works on ANY object: an agent without the attribute (a third-party
    Agent implementation predating R3.1, or a test fake) yields empty
    totals rather than an error - the meter must never gate correctness.
    """
    totals = UsageTotals()
    try:
        records = getattr(agent, "usage_records", None)
        if records is None:
            return totals
        for record in list(records):
            totals.add_record(record)
    except Exception as exc:  # noqa: BLE001 - meter must never crash a run
        logger.warning("Failed to collect agent usage records: %s", exc)
    return totals


class Agent(Protocol):
    """Protocol for Ralph agent implementations."""

    @property
    def name(self) -> str:
        """Human-readable agent name for display."""
        ...

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
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
