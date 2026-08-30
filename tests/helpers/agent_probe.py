"""Stub the #262 agent liveness probe (tests/test_agent_liveness.py,
tests/test_reviewer_rotation.py).

``liveness._stream`` is the single subprocess seam every probe goes
through. tests/conftest.py switches probing off suite-wide AND replaces
that seam with one that fails the test, so a test wanting a probe has to
arm both deliberately. This does exactly that, in one call.
"""

from __future__ import annotations

import pytest

from kstrl.agents import liveness


def stub_probe(
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
    *,
    timed_out: bool = False,
) -> list[list[str]]:
    """Arm probing with a canned CLI transcript.

    Returns the list the seam appends each probe's argv to, so a test
    can assert on the command as well as on the verdict, and can assert
    that no probe ran at all.
    """
    seen: list[list[str]] = []

    def fake_stream(cmd: list[str]) -> tuple[list[str], bool]:
        seen.append(cmd)
        return lines, timed_out

    monkeypatch.setenv(liveness.PROBE_ENV_VAR, "1")
    monkeypatch.setattr(liveness, "_stream", fake_stream)
    liveness.reset_probe_cache()
    return seen
