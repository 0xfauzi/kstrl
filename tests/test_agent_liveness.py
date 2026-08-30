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
from pathlib import Path
from typing import Any

import pytest

from kstrl.agents import liveness, proc
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
        # One attempt only, because the first one answered.
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

    @pytest.mark.parametrize("is_error", [False, True])
    def test_interleaved_stderr_is_skipped_not_fatal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        is_error: bool,
    ) -> None:
        """DeadlineStreamer merges stderr into stdout (proc.py), so real
        transcripts carry Node warnings and update notices around the
        envelope. Parsing the whole stream as one document would fail on
        any of them: a healthy CLI would read as dead, and a refusal
        would lose the reason the operator needs.
        """
        stub_probe(
            monkeypatch,
            [
                "(node:48211) ExperimentalWarning: WASI is an experimental feature",
                "  (Use `node --trace-warnings ...` to show where the warning was created)",
                *_claude_result(is_error=is_error, result="Credit balance is too low"),
                "npm notice New major version of npm available!",
            ],
        )

        result = probe_family(CLAUDE_FAMILY)

        assert result.live is not is_error
        assert result.detail == (None if not is_error else "Credit balance is too low")

    def test_a_failed_haiku_attempt_falls_back_to_the_default_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An account or CLI version where the `haiku` alias is
        unavailable must not condemn the whole claude family. The
        fallback asks a weaker question: no --model at all, which is the
        model a cross-family reviewer runs with.
        """
        seen = stub_probe(
            monkeypatch,
            _claude_result(is_error=True, result="Invalid model name: haiku"),
            then=_claude_result(is_error=False),
        )

        assert probe_family(CLAUDE_FAMILY) == ProbeResult(live=True)
        assert [("--model" in cmd) for cmd in seen] == [True, False]

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
        seen = stub_probe(monkeypatch, ["some partial output"], timed_out=True)

        assert probe_family(CLAUDE_FAMILY) == ProbeResult(live=False, detail="no answer within 60s")
        # A breach spends the whole budget, so the fallback never starts.
        assert len(seen) == 1

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

    def test_an_explained_refusal_is_not_retried(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """codex has no weaker question to fall back to, so it gets one
        attempt. An identical retry cannot change a verdict the CLI has
        already explained, and measured it would double both the stall
        and the token bill on the path that is already going wrong.
        """
        seen = stub_probe(
            monkeypatch,
            [_codex_event(type="turn.failed", error={"message": "usage limit reached"})],
        )

        result = probe_family(CODEX_FAMILY)

        assert result.live is False
        assert result.detail == "usage limit reached"
        assert len(seen) == 1

    def test_a_spent_budget_stops_the_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A family that hangs must not be waited out once per attempt.
        The budget bounds the WALK, so a first attempt that burns it
        stops the fallback from starting.
        """
        monkeypatch.setattr(liveness, "PROBE_TIMEOUT_SECONDS", 0.0)
        seen = stub_probe(
            monkeypatch,
            _claude_result(is_error=True, result="Invalid model name: haiku"),
            then=_claude_result(is_error=False),
        )

        assert probe_family(CLAUDE_FAMILY).live is False
        assert len(seen) == 1

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

    def test_seam_runs_in_a_scratch_directory(self) -> None:
        """A probe must not load the target project's CLAUDE.md, hooks
        or MCP servers. A project hook that errors is the same class of
        fault as the malformed ~/.codex/hooks.json that motivated #262,
        and the probe exists to detect that, not to reproduce it.
        """
        lines, _ = _REAL_STREAM(["/bin/pwd"])

        assert len(lines) == 1
        cwd = Path(lines[0]).resolve()
        assert cwd != Path.cwd().resolve()
        assert "kstrl-probe-" in cwd.name

    def test_seam_deregisters_the_streamer_on_the_timeout_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`finish` runs on both paths, so `proc._ACTIVE` never keeps a
        streamer that is already dead. Otherwise a concurrent shutdown
        signals an already-killed process group, and the reader and
        writer threads are never joined.
        """
        monkeypatch.setattr(liveness, "PROBE_TIMEOUT_SECONDS", 0.1)
        before = len(proc._ACTIVE)

        lines, timed_out = _REAL_STREAM(["/bin/sleep", "30"])

        assert timed_out is True
        assert lines == []
        assert len(proc._ACTIVE) == before


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
