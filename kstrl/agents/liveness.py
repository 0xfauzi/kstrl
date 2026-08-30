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
caches the answer. Measured on the reporting machine, each in a scratch
directory, which is where the probe runs:

===================================  ======  =========================
attempt                              wall    cost
===================================  ======  =========================
claude, ``--model haiku``            4.2s    $0.025 ($0.003 warm cache)
claude, no ``--model`` (fallback)    4.1s    $0.149
codex                                6.1s    19.6k tokens, no cost
===================================  ======  =========================

FALSE NEGATIVES ARE THE FAILURE MODE HERE. A probe that reports a
healthy CLI dead is this module's own purpose running backwards: the
verdict is cached for the whole process and silently downgrades review
AND security to same-family, dropping the R7.1 heterogeneity property.
Four decisions exist only to make that harder, and each is stated once,
where it is implemented:

1. every probe runs in a fresh temporary directory (``_stream``),
2. output is parsed per line, never as one document
   (``_last_result_event``),
3. claude gets a second, weaker attempt before it is condemned
   (``_PROBES``),
4. a codex error item is not a failed turn (``_codex_turn_outcome``).

The isolation in (1) is deliberately PROJECT scope only. A user-level
fault must still participate, because a malformed ``~/.codex/hooks.json``
is the motivating case. The trade that buys: a project hook or MCP
server that hangs the real CLI passes the probe and then fails every
review. That false positive is the accepted side of "false negatives are
the failure mode", not an oversight.

Every failure resolves to "not live" rather than an exception: a probe
must never be able to take down a working run.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
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

#: Per attempt, ~9x the slowest measured probe (6.1s). Deliberately not a
#: knob: the tail of that distribution is unmeasured, and a deadline set
#: too short buys nothing but false negatives. The budget also bounds the
#: WALK (see ``_probe_until_live``), so a family cannot be waited out
#: twice; an attempt already under way is still bounded only by its own
#: deadline plus ``DeadlineStreamer``'s kill grace.
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


#: Returned whenever nothing was actually asked. An unverified family
#: reads exactly like the PATH-only trust that predates #262, so no
#: caller has to special-case "we did not check".
UNPROBED = ProbeResult(live=True)

#: The one deadline-breach verdict, matched by IDENTITY in the walk so a
#: CLI that happens to print this same sentence cannot pass itself off as
#: a timeout. A breach is the one failure a further attempt must not
#: follow: the CLI has already had the full budget.
_TIMED_OUT = ProbeResult(live=False, detail=_TIMEOUT_DETAIL)

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
    attempts = _PROBES.get(family)
    if attempts is None or not probing_enabled():
        return UNPROBED
    cached = _CACHE.get(family)
    if cached is not None:
        return cached
    result = _probe_until_live(attempts)
    _CACHE[family] = result
    return result


def _probe_until_live(attempts: tuple[Callable[[], ProbeResult], ...]) -> ProbeResult:
    """Walk a family's attempts until one completes a turn.

    Every entry after the first is a WEAKER assumption, never a repeat.
    An identical retry cannot change an answer the CLI has already
    explained - "You've hit your usage limit", "Credit balance is too
    low", a missing binary - and measured, it doubles both the stall and
    the bill on exactly the path that is already going wrong. The
    protection against a transient blip is that a spurious failure
    DEGRADES (same-family review plus a warning quoting the reason), the
    same outcome a missing CLI has always produced, rather than failing
    the run.

    The walk also stops on a deadline breach, and once the probe budget
    is spent, so a family that hangs or crawls cannot be waited out once
    per attempt.
    """
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    result = _attempt(attempts[0])
    for weaker in attempts[1:]:
        if result.live or result is _TIMED_OUT or time.monotonic() >= deadline:
            break
        result = _attempt(weaker)
    return result


def _attempt(probe: Callable[[], ProbeResult]) -> ProbeResult:
    try:
        return probe()
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return _dead(f"probe could not start: {exc}")


def _stream(cmd: list[str]) -> tuple[list[str], bool]:
    """Run one probe command in a scratch directory; ``(lines, timed_out)``.

    The single subprocess seam every probe goes through, so a test
    replaces one function and the suite cannot reach a real CLI.

    The scratch directory is decision (1) from the module docstring: no
    project ``CLAUDE.md``, hook, or MCP server definition participates,
    because a project hook that errors is the very fault class this
    module exists to detect and the probe must not reproduce it. It is
    also cheaper - measured, the codex probe in the repository root
    loads project instructions and costs 21.5k input tokens against
    19.6k here.
    """
    with tempfile.TemporaryDirectory(prefix="kstrl-probe-") as scratch:
        streamer = DeadlineStreamer(
            cmd,
            cwd=Path(scratch),
            stdin_text="ping",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        lines = list(streamer.lines())
        timed_out = streamer.timed_out
        # On both paths, so the streamer always deregisters from
        # proc._ACTIVE and its reader and writer threads are joined.
        streamer.finish()
        return lines, timed_out


def _probe_claude(model: str | None) -> ProbeResult:
    """One turn; live unless the result envelope says ``is_error``."""
    cmd = ["claude", "--print", "--output-format", "json"]
    if model is not None:
        cmd.extend(["--model", model])
    lines, timed_out = _stream(cmd)
    if timed_out:
        return _TIMED_OUT
    payload = _last_result_event(lines)
    if payload is None:
        return _dead(_last_line(lines) or "no JSON result event")
    if payload.get("is_error"):
        return _dead(_as_text(payload.get("result")) or "reported is_error")
    return ProbeResult(live=True)


def _probe_codex() -> ProbeResult:
    """One read-only turn. ``--skip-git-repo-check`` is what ``_stream``'s
    scratch directory costs: measured, ``codex exec`` refuses to start
    outside a trusted git directory ("Not inside a trusted directory and
    --skip-git-repo-check was not specified", exit in 0.09s), and
    ``-s read-only`` does not cover that check."""
    lines, timed_out = _stream(
        ["codex", "exec", "--json", "-s", "read-only", "--skip-git-repo-check", "-"]
    )
    if timed_out:
        return _TIMED_OUT
    completed, failure = _codex_turn_outcome(lines)
    if failure is not None:
        return _dead(failure)
    if not completed:
        return _dead(_last_line(lines) or "no turn.completed event")
    return ProbeResult(live=True)


def _codex_turn_outcome(lines: list[str]) -> tuple[bool, str | None]:
    """``(turn completed, failure message)`` from a codex event stream.

    Decision (4) from the module docstring: an error ITEM is not a
    failed turn. A malformed ``~/.codex/hooks.json`` surfaces as
    ``{"type": "error", ...}`` inside an ``item.completed`` event while
    the turn still completes successfully, so gating on "any error
    event" would condemn a working CLI. Only ``turn.failed`` present, or
    ``turn.completed`` absent, is a failure.
    """
    completed = False
    failure: str | None = None
    for event in _json_events(lines):
        if event.get("type") == "turn.completed":
            completed = True
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            message = _as_text(error.get("message")) if isinstance(error, dict) else None
            failure = message or "turn.failed"
    return completed, failure


#: Each family's probe attempts, in order; the walk stops at the first
#: live verdict. Every entry after the first is a WEAKER assumption, so
#: a family with nothing left to give up has exactly one.
#:
#: Claude's fallback drops ``--model``. It exists because an account or
#: CLI version where the ``haiku`` alias is unavailable must not be able
#: to condemn the whole family, and what it then asks for is the model a
#: cross-family reviewer runs with (the rotation constructs its adapter
#: with model=None; the reviewer's other argv differs, so this checks the
#: account and the CLI, not the reviewer's exact invocation). ``haiku``
#: goes first because it is 6x cheaper, measured at $0.025 against
#: $0.149, so the expensive question is only asked once the cheap one has
#: already failed.
_PROBES: dict[str, tuple[Callable[[], ProbeResult], ...]] = {
    CLAUDE_FAMILY: (partial(_probe_claude, "haiku"), partial(_probe_claude, None)),
    CODEX_FAMILY: (_probe_codex,),
}


def _dead(detail: str) -> ProbeResult:
    return ProbeResult(live=False, detail=_truncate(detail))


def _json_events(lines: list[str]) -> Iterator[dict[str, Any]]:
    """Every line that parses as a JSON object, in order.

    Decision (2) from the module docstring: per line, never as one
    document. ``DeadlineStreamer`` merges stderr into stdout
    (``proc.py``), so a Node ``ExperimentalWarning`` or an npm update
    notice rides along with the events, and joining the stream would let
    any one of them make the whole transcript unreadable.
    """
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def _last_result_event(lines: list[str]) -> dict[str, Any] | None:
    """The claude result envelope, or None if the stream carried none."""
    return next(
        (e for e in _json_events(lines[::-1]) if "is_error" in e or "type" in e),
        None,
    )


def _as_text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _last_line(lines: list[str]) -> str | None:
    return next((s for line in reversed(lines) if (s := line.strip())), None)


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _DETAIL_MAX_CHARS:
        return collapsed
    return collapsed[: _DETAIL_MAX_CHARS - 3] + "..."
