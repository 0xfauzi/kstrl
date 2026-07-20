"""Claude Agent SDK adapter for Ralph (R7.6).

Drives the claude-code engineer path through the Claude Agent SDK for
its measured structural wins (docs/sdk-spike.md): typed usage/cost data
(no stream-json parse heuristics), in-loop ``max_budget_usd``
enforcement, a default-on PreToolUse workspace guard (prevention where
the subprocess path has detection), and typed failure surfaces.

Architecture (measured constraint, 2026-07-20, SDK 0.2.123): the SDK's
``SubprocessCLITransport`` spawns the claude CLI via ``anyio.open_process``
WITHOUT ``start_new_session``, and its ``close()`` signals only the
direct child. The CLI therefore shares the harness's process group: a
hung tool grandchild would be orphaned on kill, and a group-kill from
the harness would kill the harness itself. An in-process asyncio bridge
consequently CANNOT meet the R0.1 timeout battery.

So the SDK runs one level down: ``run()`` spawns
``python -m ralph_py.agents.sdk_runner`` through the R0.1-proven
:class:`~ralph_py.agents.proc.DeadlineStreamer` (``start_new_session=True``).
The runner drives the SDK; the claude CLI and its tool processes are
grandchildren inside the runner's session, so a deadline breach
group-kills the entire tree with the same measured machinery every
other adapter uses. Structured data crosses back on a prefixed
JSON-line contract owned by this repo on both sides - versioned
together, unlike the CLI stream-json surface the R3.1 meter documents
as parse-drift-prone.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ralph_py.agents.base import UsageRecord
from ralph_py.agents.proc import DeadlineStreamer, timeout_message
from ralph_py.sandbox import (
    SandboxConfig,
    claude_sandbox_drops_skip_permissions,
    claude_sandbox_settings,
)

logger = logging.getLogger(__name__)

# Prefixed-line contract between sdk_runner (emitter) and this adapter
# (parser). Both sides live in this package and change together; the
# prefixes are chosen to never collide with model output because the
# runner emits them at line start on their own lines.
USAGE_PREFIX = "RALPH-SDK-USAGE "
RESULT_PREFIX = "RALPH-SDK-RESULT "


class ClaudeSdkAgent:
    """Agent that drives claude-code through the Claude Agent SDK.

    Opt-in via ``agent_type = "claude-sdk"``; the subprocess adapters
    remain the default (the SDK is an optional dependency - install the
    ``sdk`` extra).
    """

    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        sandbox: SandboxConfig | None = None,
        max_budget_usd: float | None = None,
        workspace_guard: bool = True,
    ):
        """Initialize the SDK adapter.

        Args:
            model: Model name passed through to the SDK/CLI.
            effort: Reasoning effort level (low, medium, high, max).
            sandbox: OS-level sandbox intent (R7.5); the SAME settings
                payload the CLI adapter uses rides
                ``ClaudeAgentOptions.settings``.
            max_budget_usd: In-loop budget ceiling enforced by the CLI
                per turn (measured in docs/sdk-spike.md: the halt is
                typed, inside the agent loop, overshoot bounded by one
                turn instead of one phase).
            workspace_guard: Default-on PreToolUse hook denying file
                tools (Write/Edit/MultiEdit/NotebookEdit) that target
                paths outside the run's working directory.
        """
        self._model = model
        self._effort = effort
        self._sandbox = sandbox
        self._max_budget_usd = max_budget_usd
        self._workspace_guard = workspace_guard
        self._final_message: str | None = None
        self._usage_records: list[UsageRecord] = []
        # Test hook: the R0.1 battery points the SDK at fake CLIs.
        self._cli_path: str | None = None

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        if self._model:
            return f"claude-sdk ({self._model})"
        return "claude-sdk"

    @classmethod
    def is_available(cls) -> bool:
        """Whether the optional claude-agent-sdk package is installed.

        CLI presence is the SDK's concern at runtime (it locates a
        bundled or system CLI and raises a typed ``CLINotFoundError``
        which the runner surfaces as an error line).
        """
        return importlib.util.find_spec("claude_agent_sdk") is not None

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        """Run the SDK runner subprocess with the prompt on stdin.

        Yields human-readable lines; structured usage/result records
        are consumed off the prefixed contract lines. When ``timeout``
        is set and the runner (or the CLI underneath it) hangs, the
        runner's process group is killed and a timeout error line is
        yielded last - identical semantics to the other adapters.
        """
        self._final_message = None
        started = time.monotonic()
        usage_payload: dict[str, Any] | None = None
        result_payload: dict[str, Any] | None = None

        config: dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "effort": self._effort,
            "settings": claude_sandbox_settings(self._sandbox),
            "bypass_permissions": not claude_sandbox_drops_skip_permissions(
                self._sandbox
            ),
            "max_budget_usd": self._max_budget_usd,
            "workspace_guard": self._workspace_guard,
            "cwd": str(cwd) if cwd else None,
            "cli_path": self._cli_path,
        }

        cmd = [sys.executable, "-u", "-m", "ralph_py.agents.sdk_runner"]
        streamer = DeadlineStreamer(
            cmd, cwd=cwd, stdin_text=json.dumps(config), timeout=timeout,
        )

        for line in streamer.lines():
            if line.startswith(USAGE_PREFIX):
                usage_payload = _parse_contract_line(line, USAGE_PREFIX)
                continue
            if line.startswith(RESULT_PREFIX):
                result_payload = _parse_contract_line(line, RESULT_PREFIX)
                continue
            yield line

        if streamer.timed_out:
            self._usage_records.append(UsageRecord(
                duration_seconds=time.monotonic() - started,
                source="timeout",
            ))
            yield timeout_message(timeout)
            return

        streamer.finish()

        self._usage_records.append(_usage_record_from_payload(
            usage_payload, duration_seconds=time.monotonic() - started,
        ))
        if result_payload is not None:
            result_text = result_payload.get("result")
            if isinstance(result_text, str) and result_text:
                self._final_message = result_text

    @property
    def final_message(self) -> str | None:
        """Final result text from the SDK's typed ResultMessage."""
        return self._final_message

    @property
    def usage_records(self) -> list[UsageRecord]:
        """One usage record per ``run`` call, accumulated (R3.1)."""
        return list(self._usage_records)


def _parse_contract_line(
    line: str, prefix: str,
) -> dict[str, Any] | None:
    """Parse a prefixed contract line; malformed payloads degrade to
    None (the meter must never gate correctness - R3.1)."""
    try:
        payload = json.loads(line[len(prefix):])
    except (json.JSONDecodeError, ValueError):
        logger.warning("Malformed sdk-runner contract line: %.120s", line)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _usage_record_from_payload(
    payload: dict[str, Any] | None, *, duration_seconds: float,
) -> UsageRecord:
    """Map the runner's usage payload onto a UsageRecord.

    Field-defensive like the R3.1 claude parser: a missing or
    ill-typed field becomes None ("not reported"), never a crash. The
    payload originates from the SDK's typed ResultMessage, so in
    practice these are typed ints/floats; the defense covers contract
    bugs and truncated lines.
    """
    if payload is None:
        return UsageRecord(
            duration_seconds=duration_seconds, source="unavailable",
        )
    tokens = {
        name: _opt_int(payload.get(name))
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
    }
    parts = [v for v in tokens.values() if v is not None]
    total = sum(parts) if parts else None
    return UsageRecord(
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
        cache_creation_tokens=tokens["cache_creation_tokens"],
        total_tokens=total,
        cost_usd=_opt_float(payload.get("cost_usd")),
        duration_seconds=duration_seconds,
        source="claude-sdk-typed",
    )


def _opt_int(value: object) -> int | None:
    """Non-negative int or None; bool rejected (int subclass)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _opt_float(value: object) -> float | None:
    """Non-negative float or None; bool rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None
