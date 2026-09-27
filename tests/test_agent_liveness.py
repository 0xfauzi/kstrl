"""#262: the agent preflight's subprocess seam, run against real binaries.

Every other liveness test replaces ``liveness._stream`` (tests/conftest.py
enforces that no test spawns a real agent CLI), which would leave the one
function that touches a subprocess covered by nothing. These tests run the
real seam against ``/bin/echo``, ``/bin/pwd`` and ``/bin/sleep``: none is
an agent CLI, so they cost nothing and bill no account. The probe's argv
shapes and its verdicts over stubbed transcripts are carried by the
reviewer-rotation tests with claude and codex stripped from PATH.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.agents import liveness, proc

#: The real subprocess seam, captured at import time - tests/conftest.py
#: replaces the module attribute per test so nothing can reach a CLI by
#: accident.
_REAL_STREAM = liveness._stream


class TestStreamSeam:
    """The seam itself, exercised against a harmless binary."""

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
