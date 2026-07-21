"""Codex CLI agent for kstrl."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from kstrl.agents.base import UsageRecord
from kstrl.agents.proc import DeadlineStreamer, timeout_message
from kstrl.sandbox import SandboxConfig, codex_sandbox_args

# Measured against codex CLI 0.134.0 (R3.1): plain `codex exec` output
# ends with a two-line trailer - a line reading "tokens used" followed by
# a comma-formatted integer line ("14,511"). That is the ONLY usage data
# codex emits with the flags this adapter uses: a TOTAL, no in/out split,
# no cost. (`codex exec --json` exposes a structured turn.completed
# usage event, but switching to it would rework streaming display and
# the plain-text COMPLETION_MARKER detection - noted as follow-up.)
_TOKENS_USED_LINE = re.compile(r"^tokens used:?\s*(?P<total>[\d,]+)?$", re.IGNORECASE)
_TOKENS_COUNT_LINE = re.compile(r"^[\d,]{1,20}$")


def _parse_token_count(text: str) -> int | None:
    """Parse a comma-formatted token count; None on any mismatch."""
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


class CodexAgent:
    """Agent that uses the Codex CLI."""

    _supports_output_last_message: bool | None = None

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox: SandboxConfig | None = None,
    ):
        """Initialize Codex agent.

        Args:
            model: Model name to pass to codex -m
            reasoning_effort: Reasoning effort level for codex -c
            sandbox: OS-level sandbox intent (R7.5); mapped to
                ``--sandbox workspace-write`` plus the network-access
                config override (measured against codex 0.134.0 - see
                kstrl.sandbox)
        """
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._sandbox = sandbox
        self._final_message: str | None = None
        self._usage_records: list[UsageRecord] = []

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        if self._model:
            return f"codex ({self._model})"
        return "codex"

    @classmethod
    def is_available(cls) -> bool:
        """Check if codex CLI is available."""
        return shutil.which("codex") is not None

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        """Run codex with prompt piped to stdin.

        Yields output lines as they arrive. When ``timeout`` is set and the
        CLI hangs (with or without output), its process group is killed and
        a timeout error line is yielded last.
        """
        self._final_message = None
        last_non_empty_line: str | None = None
        started = time.monotonic()
        trailer_total: int | None = None
        expect_token_count = False

        # Build command (non-interactive)
        cmd = ["codex", "exec"]
        if cwd:
            cmd.extend(["-C", str(cwd)])
        if self._model:
            cmd.extend(["-m", self._model])
        if self._reasoning_effort:
            # Translate unified effort levels to codex-specific values
            codex_effort = "xhigh" if self._reasoning_effort == "max" else self._reasoning_effort
            cmd.extend(["-c", f'model_reasoning_effort="{codex_effort}"'])
        cmd.extend(codex_sandbox_args(self._sandbox))

        # Use --output-last-message when supported by the codex CLI.
        last_msg_file: Path | None = None
        if self._codex_supports_output_last_message():
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                last_msg_file = Path(f.name)
            cmd.extend(["--output-last-message", str(last_msg_file)])

        try:
            streamer = DeadlineStreamer(
                cmd, cwd=cwd, stdin_text=prompt, timeout=timeout,
            )

            # Stream output
            for line in streamer.lines():
                stripped = line.strip()
                if stripped:
                    last_non_empty_line = line
                # Track the "tokens used" trailer (last match wins - the
                # real trailer is at end-of-stream; an agent echoing the
                # same text earlier is overwritten). This is a hint for
                # the cost meter, never a gate.
                if expect_token_count:
                    expect_token_count = False
                    if _TOKENS_COUNT_LINE.match(stripped):
                        trailer_total = _parse_token_count(stripped)
                match = _TOKENS_USED_LINE.match(stripped)
                if match:
                    if match.group("total"):
                        trailer_total = _parse_token_count(match.group("total"))
                    else:
                        expect_token_count = True
                yield line

            if streamer.timed_out:
                # Killed mid-run: the last-message file was likely never
                # written; partial output is not a trustworthy final
                # message, so leave it unset. The trailer never printed
                # either, so only wall time is recorded.
                self._usage_records.append(UsageRecord(
                    duration_seconds=time.monotonic() - started,
                    source="timeout",
                ))
                yield timeout_message(timeout)
                return

            streamer.finish()

            self._usage_records.append(UsageRecord(
                total_tokens=trailer_total,
                duration_seconds=time.monotonic() - started,
                source="codex-text" if trailer_total is not None else "unavailable",
            ))

            # Read final message
            if last_msg_file and last_msg_file.exists():
                content = last_msg_file.read_text().strip()
                if content:
                    self._final_message = content
            if self._final_message is None and last_non_empty_line:
                self._final_message = last_non_empty_line

        finally:
            # Cleanup temp file
            if last_msg_file is not None:
                try:
                    last_msg_file.unlink()
                except Exception:
                    pass

    @property
    def final_message(self) -> str | None:
        """Return final message from --output-last-message."""
        return self._final_message

    @property
    def usage_records(self) -> list[UsageRecord]:
        """One usage record per ``run`` call, accumulated (R3.1)."""
        return list(self._usage_records)

    @classmethod
    def _codex_supports_output_last_message(cls) -> bool:
        """Check whether codex supports --output-last-message."""
        if cls._supports_output_last_message is not None:
            return cls._supports_output_last_message

        try:
            result = subprocess.run(
                ["codex", "exec", "--help"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
            )
            cls._supports_output_last_message = "--output-last-message" in result.stdout
        except Exception:
            cls._supports_output_last_message = False

        return cls._supports_output_last_message
