"""#262: the agent preflight probes for a turn, not just for a file on PATH.

The live case that motivated this: ``codex`` 0.150.1 on PATH, quota
exhausted and ``~/.codex/hooks.json`` malformed. ``is_available()``
returned True, the R7.1 rotation selected it for review and security,
and the run paid the whole engineer bill before the first adversarial
dispatch failed.

The cross-family downgrade that failure motivated is rotation behaviour
and lives with the rest of it, in tests/test_reviewer_rotation.py.

Every test here replaces ``liveness._stream``, the single subprocess
seam. No test may spawn a real CLI; tests/conftest.py enforces that.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kstrl.agents import liveness
from kstrl.agents.liveness import (
    CLAUDE_FAMILY,
    CODEX_FAMILY,
    PROBE_ENV_VAR,
    ProbeResult,
    probe_family,
    probing_enabled,
)
from tests.helpers.agent_probe import stub_probe

#: The real subprocess seam, captured at import time - tests/conftest.py
#: replaces the module attribute per test so nothing can reach a CLI by
#: accident. One test below calls this directly, against /bin/echo, to
#: prove the seam every other test stubs actually runs a process.
_REAL_STREAM = liveness._stream


def _claude_result(*, is_error: bool, result: str = "PONG") -> list[str]:
    """One ``claude -p --output-format json`` result envelope."""
    return [
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": is_error,
                "result": result,
                "total_cost_usd": 0.0027,
            }
        )
    ]


def _codex_event(**fields: Any) -> str:
    return json.dumps(fields)


class TestClaudeProbe:
    def test_live_agent_completes_a_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = stub_probe(monkeypatch, _claude_result(is_error=False))

        assert probe_family(CLAUDE_FAMILY) == ProbeResult(live=True)
        # Cheapest model, non-interactive, machine-readable: the shape
        # the cost table in liveness.py's docstring was measured against.
        assert seen == [["claude", "--print", "--output-format", "json", "--model", "haiku"]]

    def test_dead_agent_quotes_the_cli_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(
            monkeypatch,
            _claude_result(is_error=True, result="Credit balance is too low"),
        )

        result = probe_family(CLAUDE_FAMILY)

        assert result.live is False
        assert result.detail == "Credit balance is too low"

    def test_non_zero_exit_with_no_json_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A CLI that dies before it emits its result envelope (bad auth,
        # a crash, a usage message on stderr) prints no parsable JSON.
        stub_probe(monkeypatch, ["Invalid API key. Run /login.", ""])

        result = probe_family(CLAUDE_FAMILY)

        assert result.live is False
        assert result.detail == "Invalid API key. Run /login."

    def test_silent_death_still_reports_a_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(monkeypatch, [])

        assert probe_family(CLAUDE_FAMILY) == ProbeResult(live=False, detail="no JSON result event")

    def test_missing_is_error_field_is_forgiving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An older or newer CLI that parses but omits the flag must not
        # be failed: a false negative is worse than today's behaviour.
        stub_probe(monkeypatch, [json.dumps({"type": "result", "result": "PONG"})])

        assert probe_family(CLAUDE_FAMILY).live is True


class TestCodexProbe:
    def test_live_agent_completes_a_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = stub_probe(
            monkeypatch,
            [
                _codex_event(type="item.completed", item={"id": "item_0", "type": "text"}),
                _codex_event(type="turn.completed", usage={"input_tokens": 19600}),
            ],
        )

        assert probe_family(CODEX_FAMILY).live is True
        cmd = seen[0]
        # See liveness.py's docstring: codex exec refuses to start
        # outside a trusted git directory, so a scratch-directory probe
        # MUST skip that check.
        assert "--skip-git-repo-check" in cmd
        assert cmd[cmd.index("-s") + 1] == "read-only"
        assert cmd[-1] == "-"

    def test_error_event_does_not_fail_a_completed_turn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The measured malformed ~/.codex/hooks.json case (liveness.py
        # docstring, gotcha 2): an error item is emitted and the turn
        # still completes. Gating on "any error event" would report a
        # working CLI as dead.
        stub_probe(
            monkeypatch,
            [
                _codex_event(
                    type="item.completed",
                    item={
                        "id": "item_0",
                        "type": "error",
                        "message": "failed to parse hooks config",
                    },
                ),
                _codex_event(type="turn.completed"),
            ],
        )

        assert probe_family(CODEX_FAMILY).live is True

    def test_failed_turn_alongside_an_error_event_is_dead(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_probe(
            monkeypatch,
            [
                _codex_event(
                    type="item.completed",
                    item={"id": "item_0", "type": "error", "message": "hooks config"},
                ),
                _codex_event(type="error", message="You've hit your usage limit."),
                _codex_event(
                    type="turn.failed",
                    error={"message": "You've hit your usage limit."},
                ),
            ],
        )

        result = probe_family(CODEX_FAMILY)

        assert result.live is False
        assert result.detail == "You've hit your usage limit."

    def test_no_terminal_event_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(
            monkeypatch,
            ["Not inside a trusted directory and --skip-git-repo-check was not specified"],
        )

        result = probe_family(CODEX_FAMILY)

        assert result.live is False
        assert result.detail is not None and "trusted directory" in result.detail

    def test_failed_turn_without_a_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(monkeypatch, [_codex_event(type="turn.failed", error=None)])

        assert probe_family(CODEX_FAMILY).detail == "turn.failed"


class TestProbeRobustness:
    def test_hung_agent_hits_the_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(monkeypatch, ["some partial output"], timed_out=True)

        assert probe_family(CLAUDE_FAMILY) == ProbeResult(live=False, detail="no answer within 60s")

    def test_missing_binary_is_dead_not_an_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_probe(monkeypatch, [])

        def explode(cmd: list[str]) -> tuple[list[str], bool]:
            raise FileNotFoundError(2, "No such file or directory", "codex")

        monkeypatch.setattr(liveness, "_stream", explode)

        result = probe_family(CODEX_FAMILY)

        assert result.live is False
        assert result.detail is not None and "codex" in result.detail

    def test_result_is_cached_for_the_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = stub_probe(monkeypatch, _claude_result(is_error=False))

        assert probe_family(CLAUDE_FAMILY).live is True
        assert probe_family(CLAUDE_FAMILY).live is True

        assert len(seen) == 1

    def test_long_detail_is_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_probe(monkeypatch, _claude_result(is_error=True, result="x" * 900))

        detail = probe_family(CLAUDE_FAMILY).detail

        assert detail is not None
        assert len(detail) == 300
        assert detail.endswith("...")

    def test_family_without_a_probe_body_is_reported_live(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # claude-sdk runs the SDK transport, not the `claude` binary, so
        # the probe table has no entry and nothing runs. The preflight
        # never asks for it either - that half is pinned by
        # tests/test_preflight.py::test_claude_sdk_is_not_routed_to_a_probe.
        seen = stub_probe(monkeypatch, [])

        assert probe_family("claude-sdk") == ProbeResult(live=True)
        assert seen == []


class TestStreamSeam:
    """The seam itself, exercised against a harmless binary.

    Every other test replaces ``_stream``, which would leave the one
    function that touches a subprocess covered by nothing. ``/bin/echo``
    is not an agent CLI: it costs nothing and bills no account.
    """

    def test_seam_returns_the_process_output(self) -> None:
        lines, timed_out = _REAL_STREAM(["/bin/echo", "one\ntwo"])

        assert lines == ["one", "two"]
        assert timed_out is False


class TestKillSwitch:
    def test_default_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PROBE_ENV_VAR, raising=False)

        assert probing_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", ""])
    def test_switch_off_skips_the_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        seen = stub_probe(monkeypatch, _claude_result(is_error=True))
        monkeypatch.setenv(PROBE_ENV_VAR, value)

        assert probing_enabled() is False
        assert probe_family(CLAUDE_FAMILY) == ProbeResult(live=True)
        assert seen == []
