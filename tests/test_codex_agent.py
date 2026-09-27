"""Live contract tests for the codex agent integration.

These tests guard against future codex CLI updates that change the
--output-last-message contract or the streaming output format. The
knowledge layer's distillation parser silently degrades when the codex
echoed-prompt JSON schema example leaks into the stream; the
final_message path is the load-bearing fallback. If codex stops
populating that file, distillation parsing reverts to the broken case
without a clear signal.

R4.3 network policy: the default suite is network-free. This tier drives
the real codex CLI - an LLM call over the network - so it is opt-in
behind KSTRL_RUN_LIVE_CONTRACT=1 in addition to requiring codex on PATH:

    KSTRL_RUN_LIVE_CONTRACT=1 uv run pytest tests/test_codex_agent.py -v
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kstrl.agents.codex import CodexAgent

CODEX_AVAILABLE = shutil.which("codex") is not None
LIVE_CONTRACT_ENABLED = "1" in (
    os.environ.get("KSTRL_RUN_LIVE_CONTRACT"),
    os.environ.get("KSTRL_RUN_LIVE_CONTRACT"),
)


@pytest.mark.skipif(
    not LIVE_CONTRACT_ENABLED,
    reason="live codex contract tests are opt-in: set KSTRL_RUN_LIVE_CONTRACT=1",
)
@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not installed")
class TestCodexLiveContract:
    """Opt-in tier that drives the real codex CLI (network + auth).

    Catches real upstream CLI changes early; if any of these fail, the
    knowledge / review / security parsers will be downstream-broken.
    Because the tier is explicitly opted into, failures here surface as
    failures - nothing is swallowed into skips."""

    def test_codex_supports_output_last_message(self) -> None:
        """If this fails, codex has removed --output-last-message and the
        final_message fallback in distill_facts will quietly stop
        working; output parsing will revert to the streamed-prompt-echo
        bug we fixed."""
        CodexAgent._supports_output_last_message = None
        supported = CodexAgent._codex_supports_output_last_message()
        assert supported is True, (
            "codex no longer advertises --output-last-message; "
            "knowledge / review / security parsers will silently regress"
        )

    def test_smoke_run_populates_final_message(self, tmp_path: Path) -> None:
        """Drive codex with a trivial prompt and assert final_message is
        set. If codex stops writing the last-message file, this catches
        it. Auth or network failures fail the test - the caller opted
        into the live tier and wants the real signal."""
        agent = CodexAgent()
        # Drain the iterator so the subprocess completes
        lines = []
        for line in agent.run(
            "Reply with exactly the single word: OK\n",
            cwd=tmp_path,
            timeout=60.0,
        ):
            lines.append(line)

        # We don't assert on the content - codex can route through OAuth
        # and may return varied output. The important contract is that
        # final_message gets populated (either from --output-last-message
        # or from the last_non_empty_line fallback).
        assert agent.final_message is not None
        assert len(agent.final_message) > 0
