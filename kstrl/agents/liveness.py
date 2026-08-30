"""Can the installed agent CLI actually complete a turn? (#262)

``is_available()`` answers a narrower question than its callers assume:
"a file with this name exists on PATH". It says nothing about
authentication, quota, or a corrupt config file. The two came apart on a
real machine: ``codex`` 0.150.1 was on PATH, its account quota was
exhausted and ``~/.codex/hooks.json`` was malformed, and the preflight
saw neither. The expensive consequence is the R7.1 cross-family default
in ``factory.resolve_adversarial_selection``: it picks the OTHER family
for review and security on the strength of ``is_available()``, so the
engineer phase runs and is paid for first and every adversarial dispatch
then fails against a CLI that was never going to answer.

A probe runs one trivial turn per family, at most once per process, and
caches the answer. Measured medians:

- ``claude --print --output-format json --model haiku``: 4.8s wall,
  $0.019 (3.4s / $0.0027 on a warm prompt cache).
- ``codex exec --json -s read-only --skip-git-repo-check -C <tmpdir> -``:
  6.9s, ~19.6k tokens, no cost reported by the CLI.

Two measured behaviours are encoded here and must not be "simplified"
away:

1. ``codex exec`` refuses to start outside a trusted git directory
   ("Not inside a trusted directory and --skip-git-repo-check was not
   specified", exit in 0.09s), so the probe passes
   ``--skip-git-repo-check`` and runs in a temporary directory. The
   temporary directory is also the cheaper of the two options: the same
   probe in the repository root loads project instructions and cost
   21.5k input tokens against 19.6k.
2. A malformed ``~/.codex/hooks.json`` surfaces as
   ``{"type": "error", ...}`` inside an ``item.completed`` event *while
   the turn still completes successfully*. Gating on "any error event"
   would therefore fail a working CLI. The criterion is
   ``turn.failed`` present, or ``turn.completed`` absent - nothing else.

Every failure mode resolves to "not live" rather than an exception: a
probe that is wrong in the pessimistic direction downgrades a reviewer
or prints a warning, and must never be able to take down a working run.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from kstrl.agents.proc import DeadlineStreamer
from kstrl.config import _parse_bool

#: Default ON; ``KSTRL_AGENT_PROBE=0`` restores the pre-#262 behaviour of
#: trusting PATH. Read straight from the environment rather than from a
#: ``KstrlConfig`` field, which is the repo convention for settings:
#: both callers have to reach this switch and one of them,
#: ``factory.resolve_adversarial_selection``, holds no config object. A
#: toml key would therefore work for the CLI preflight and silently not
#: work for the cross-family probe, which is worse than env-only.
PROBE_ENV_VAR = "KSTRL_AGENT_PROBE"

#: ~9x headroom over the slowest measured probe (6.9s). Deliberately not
#: a knob: the tail of this distribution is unmeasured, and a deadline
#: set too short buys nothing but false negatives - which downgrade the
#: cross-family reviewer that R7.1 exists to keep.
PROBE_TIMEOUT_SECONDS = 60.0

CLAUDE_FAMILY = "claude-code"
CODEX_FAMILY = "codex"

_DETAIL_MAX_CHARS = 300
_TIMEOUT_DETAIL = f"no answer within {PROBE_TIMEOUT_SECONDS:.0f}s"


@dataclass(frozen=True)
class ProbeResult:
    """Could this CLI complete a turn, and if not, what did it say?

    ``detail`` carries the CLI's own words, so the operator reads
    "You've hit your usage limit" rather than a generic kstrl sentence.
    """

    live: bool
    detail: str | None = None


#: Returned whenever nothing was actually asked: an unverified family
#: reads exactly like today's PATH-only trust, so no caller has to
#: special-case "we did not check".
_UNPROBED = ProbeResult(live=True)

_CACHE: dict[str, ProbeResult] = {}


def reset_probe_cache() -> None:
    """Forget every cached probe result (tests; long-lived processes)."""
    _CACHE.clear()


def probing_enabled() -> bool:
    """Whether liveness probing runs at all."""
    return _parse_bool(os.environ.get(PROBE_ENV_VAR, "1"))


def probe_family(family: str) -> ProbeResult:
    """Can ``family`` complete a turn? Cached for the life of the process.

    Reports live for a family with no CLI to probe (a custom agent
    command, ``claude-sdk``) and when probing is switched off.
    """
    probe = _PROBES.get(family)
    if probe is None or not probing_enabled():
        return _UNPROBED
    cached = _CACHE.get(family)
    if cached is not None:
        return cached
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        result = _dead(f"probe could not start: {exc}")
    _CACHE[family] = result
    return result


def _stream(cmd: list[str]) -> tuple[list[str], bool]:
    """Run one probe command to completion; return ``(lines, timed_out)``.

    The single subprocess seam every probe goes through, so a test
    replaces one function and the suite cannot reach a real CLI.
    ``DeadlineStreamer`` supplies the process-group kill and the
    shutdown-time cleanup the agent adapters get.
    """
    streamer = DeadlineStreamer(
        cmd,
        stdin_text="ping",
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    lines = list(streamer.lines())
    if streamer.timed_out:
        return lines, True
    streamer.finish()
    return lines, False


def _probe_claude() -> ProbeResult:
    """One haiku turn; live unless the result envelope says ``is_error``."""
    lines, timed_out = _stream(["claude", "--print", "--output-format", "json", "--model", "haiku"])
    if timed_out:
        return _dead(_TIMEOUT_DETAIL)
    payload = _load_json("\n".join(lines))
    if payload is None:
        return _dead(_last_line(lines) or "no JSON result event")
    if payload.get("is_error"):
        return _dead(_as_text(payload.get("result")) or "reported is_error")
    return ProbeResult(live=True)


def _probe_codex() -> ProbeResult:
    """One read-only turn in a scratch directory; see the module
    docstring for why the criterion is the turn event and not the
    error events, and why the scratch directory needs a flag."""
    with tempfile.TemporaryDirectory(prefix="kstrl-probe-") as scratch:
        lines, timed_out = _stream(
            [
                "codex",
                "exec",
                "--json",
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                scratch,
                "-",
            ]
        )
    if timed_out:
        return _dead(_TIMEOUT_DETAIL)
    completed, failure = _codex_turn_outcome(lines)
    if failure is not None:
        return _dead(failure)
    if not completed:
        return _dead(_last_line(lines) or "no turn.completed event")
    return ProbeResult(live=True)


def _codex_turn_outcome(lines: list[str]) -> tuple[bool, str | None]:
    """``(turn completed, failure message)`` from a codex event stream."""
    completed = False
    failure: str | None = None
    for line in lines:
        event = _load_json(line)
        if event is None:
            continue
        if event.get("type") == "turn.completed":
            completed = True
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            message = _as_text(error.get("message")) if isinstance(error, dict) else None
            failure = message or "turn.failed"
    return completed, failure


_PROBES = {CLAUDE_FAMILY: _probe_claude, CODEX_FAMILY: _probe_codex}


def _dead(detail: str) -> ProbeResult:
    return ProbeResult(live=False, detail=_truncate(detail))


def _load_json(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, or None if it is not one."""
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _as_text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _last_line(lines: list[str]) -> str | None:
    return next((s for line in reversed(lines) if (s := line.strip())), None)


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _DETAIL_MAX_CHARS:
        return collapsed
    return collapsed[: _DETAIL_MAX_CHARS - 3] + "..."
