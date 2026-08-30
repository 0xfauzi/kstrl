"""Keep the suite off the machine's real agent CLIs (#262).

Two jobs, both about not depending on - or billing - whatever happens to
be installed:

``stub_probe`` arms the liveness probe with a canned transcript.
``liveness._stream`` is the single subprocess seam every probe goes
through, and tests/conftest.py switches probing off suite-wide AND
replaces that seam with one that fails the test, so a test wanting a
probe has to arm both deliberately.

``set_cli_availability`` fixes what ``is_available()`` reports, so a
test's verdict does not change between a laptop with both CLIs and a CI
runner with neither.
"""

from __future__ import annotations

import pytest

from kstrl.agents import ClaudeCodeAgent, CodexAgent, liveness


def stub_probe(
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
    *,
    then: list[str] | None = None,
    timed_out: bool = False,
) -> list[list[str]]:
    """Arm probing with a canned CLI transcript.

    ``then`` is the transcript for every attempt after the first, which
    is what lets a test drive claude's fallback (``--model haiku``, then
    no model) without hand-rolling the seam.

    Returns the list the seam appends each attempt's argv to, so a test
    can assert on the command as well as on the verdict, and can assert
    that no probe ran at all.
    """
    seen: list[list[str]] = []

    def fake_stream(cmd: list[str]) -> tuple[list[str], bool]:
        seen.append(cmd)
        return (lines if len(seen) == 1 or then is None else then), timed_out

    monkeypatch.setenv(liveness.PROBE_ENV_VAR, "1")
    monkeypatch.setattr(liveness, "_stream", fake_stream)
    liveness.reset_probe_cache()
    return seen


def set_cli_availability(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claude: bool,
    codex: bool,
) -> None:
    """Fix what ``is_available()`` reports for both CLI families."""
    monkeypatch.setattr(ClaudeCodeAgent, "is_available", classmethod(lambda cls: claude))
    monkeypatch.setattr(CodexAgent, "is_available", classmethod(lambda cls: codex))
