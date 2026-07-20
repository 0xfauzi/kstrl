"""Contract tests for the codex agent integration.

These tests guard against future codex CLI updates that change the
--output-last-message contract or the streaming output format. The
knowledge layer's distillation parser silently degrades when the codex
echoed-prompt JSON schema example leaks into the stream; the
final_message path is the load-bearing fallback. If codex stops
populating that file, distillation parsing reverts to the broken case
without a clear signal.

R4.3 network policy: the default suite is network-free. The structural
tests below never invoke codex (the probe is faked with a counting
stub). The live-contract tier drives the real codex CLI - an LLM call
over the network - so it is opt-in behind RALPH_RUN_LIVE_CONTRACT=1
in addition to requiring codex on PATH:

    RALPH_RUN_LIVE_CONTRACT=1 uv run pytest tests/test_codex_agent.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kstrl.agents.codex import CodexAgent

CODEX_AVAILABLE = shutil.which("codex") is not None
LIVE_CONTRACT_ENABLED = "1" in (
    os.environ.get("KSTRL_RUN_LIVE_CONTRACT"),
    os.environ.get("RALPH_RUN_LIVE_CONTRACT"),
)


class TestCodexAgentStructure:
    """Tests that never invoke codex - they assert the agent's protocol
    shape and option-detection behavior against a faked probe."""

    def test_implements_agent_protocol(self) -> None:
        a = CodexAgent()
        assert hasattr(a, "name")
        assert hasattr(a, "run")
        assert hasattr(a, "final_message")
        # final_message starts None before any run
        assert a.final_message is None

    def test_supports_output_last_message_introspection_cached(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The class memoizes the --output-last-message probe: two calls
        must agree AND spawn exactly one subprocess. Asserting only
        ``a == b`` would pass even with the memoization deleted; the
        invocation count is the caching proof (R4.3)."""
        probe_calls = {"count": 0}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            probe_calls["count"] += 1
            return subprocess.CompletedProcess(
                cmd, 0, stdout="usage: codex exec [--output-last-message FILE]\n",
            )

        # monkeypatch restores both the cache slot and subprocess.run on
        # teardown, so other tests see a fresh probe state.
        monkeypatch.setattr(CodexAgent, "_supports_output_last_message", None)
        monkeypatch.setattr("kstrl.agents.codex.subprocess.run", fake_run)

        a = CodexAgent._codex_supports_output_last_message()
        b = CodexAgent._codex_supports_output_last_message()

        assert a is True
        assert b is True
        assert probe_calls["count"] == 1, (
            "memoization broken: the --help probe ran once per call "
            "instead of being cached on the class"
        )

    def test_probe_failure_caches_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing probe (codex missing / timing out) caches False
        without retrying on the next call."""
        probe_calls = {"count": 0}

        def raising_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            probe_calls["count"] += 1
            raise FileNotFoundError("codex not on PATH")

        monkeypatch.setattr(CodexAgent, "_supports_output_last_message", None)
        monkeypatch.setattr("kstrl.agents.codex.subprocess.run", raising_run)

        assert CodexAgent._codex_supports_output_last_message() is False
        assert CodexAgent._codex_supports_output_last_message() is False
        assert probe_calls["count"] == 1

    def test_name_includes_model_when_set(self) -> None:
        assert CodexAgent(model="o3").name == "codex (o3)"
        assert CodexAgent().name == "codex"


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
            cwd=tmp_path, timeout=60.0,
        ):
            lines.append(line)

        # We don't assert on the content - codex can route through OAuth
        # and may return varied output. The important contract is that
        # final_message gets populated (either from --output-last-message
        # or from the last_non_empty_line fallback).
        assert agent.final_message is not None
        assert len(agent.final_message) > 0
