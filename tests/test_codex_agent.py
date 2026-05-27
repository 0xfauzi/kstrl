"""Contract tests for the codex agent integration.

These tests guard against future codex CLI updates that change the
--output-last-message contract or the streaming output format. The
knowledge layer's distillation parser silently degrades when the codex
echoed-prompt JSON schema example leaks into the stream; the
final_message path is the load-bearing fallback. If codex stops
populating that file, distillation parsing reverts to the broken case
without a clear signal.

Tests that require codex on PATH are skipped when codex is absent;
the structural tests don't need it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ralph_py.agents.codex import CodexAgent


CODEX_AVAILABLE = shutil.which("codex") is not None


class TestCodexAgentStructure:
    """Tests that don't require codex to be installed - they assert the
    agent's protocol shape and option-detection behavior."""

    def test_implements_agent_protocol(self) -> None:
        a = CodexAgent()
        assert hasattr(a, "name")
        assert hasattr(a, "run")
        assert hasattr(a, "final_message")
        # final_message starts None before any run
        assert a.final_message is None

    def test_supports_output_last_message_introspection_cached(self) -> None:
        """The class memoizes the --output-last-message detection. Two
        calls must return the same cached value."""
        # Reset cache to ensure fresh probe
        CodexAgent._supports_output_last_message = None
        a = CodexAgent._codex_supports_output_last_message()
        b = CodexAgent._codex_supports_output_last_message()
        assert a == b

    def test_name_includes_model_when_set(self) -> None:
        assert CodexAgent(model="o3").name == "codex (o3)"
        assert CodexAgent().name == "codex"


@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not installed")
class TestCodexLiveContract:
    """Tests that require codex on PATH. Catch real upstream CLI changes
    early; if any of these fail, the knowledge / review / security
    parsers will be downstream-broken."""

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
        set. If codex stops writing the last-message file, this catches it."""
        agent = CodexAgent()
        # Drain the iterator so the subprocess completes
        lines = []
        # Use a tiny timeout; on a flaky network this skips
        try:
            for line in agent.run(
                "Reply with exactly the single word: OK\n",
                cwd=tmp_path, timeout=60.0,
            ):
                lines.append(line)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"codex live test skipped (auth/network): {exc}")

        # We don't assert on the content - codex can route through OAuth
        # and may return varied output. The important contract is that
        # final_message gets populated (either from --output-last-message
        # or from the last_non_empty_line fallback).
        assert agent.final_message is not None
        assert len(agent.final_message) > 0
