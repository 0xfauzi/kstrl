"""Safe mode: one name for the degraded states kstrl already enters.

kstrl refuses to do the risky thing in four separate places, and each
refusal is correct on its own:

- the control directory is untrusted, so the daemon will not spend
  (``statedir.control_untrusted_reason``)
- the autonomy ladder fell back to L1 on a damaged state file, or a
  ceiling clamped the run below the level it earned (``autonomy``)
- the queue is paused, including the fail-closed case where the pause
  marker itself could not be read (``workqueue.Queue.pause_state``)
- an adversarial phase did not run for some component of the newest run
  (``pipeline.Pipeline._record_phase_skip``)

What was missing was a single question. An operator who wanted to know
whether the factory was holding back had to check a warning line at run
start, ``ks queue``, ``ks serve`` output, and a callout inside a pull
request body, and there was no word that covered all four. A spacecraft
carries one named safe mode with one recovery procedure, and the value is
precisely that it is one state with one question: are we in it, and why.

This module answers that question and nothing else. It adds no gate, no
pause and no halt, and it changes no decision: every signal it reads
already acts on its own.

Two things worth being exact about.

**"Read-only" here means with respect to factory decisions, not with
respect to the filesystem.** ``Queue.pause_state`` and
``AutonomyState.load`` both call ``statedir.ensure_control_state``, which
creates the XDG control directory and migrates any legacy in-tree control
files. Every control-plane read in kstrl does that; this one is not
special, and claiming it writes nothing would be false.

**Two readers can return the same sentence.** ``Queue.pause_state``
consults ``control_untrusted_reason`` itself and, when it is non-None,
reports the queue as paused with that exact string. Printing it twice
under two labels would tell an operator there are two problems when there
is one, so a reason whose ``detail`` exactly matches one already recorded
is dropped. The rule is exact string equality and nothing cleverer: two
readers that genuinely disagree still both speak.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Adversarial phases whose absence puts the factory in safe mode.
#: ``verify`` is deliberately not here: a skipped mechanical verification
#: is reported by ``ks status`` per component already, and unlike review
#: and security it cannot be skipped by budget exhaustion.
GATED_PHASES: tuple[str, ...] = ("review", "security")

_RUNBOOK = "docs/runbook.md"

#: Where an operator reads what to do about each source. The anchors are
#: the headings under "## Safe mode" in the runbook, and
#: ``tests/test_safemode.py`` asserts every one of them resolves, so the
#: code and the document cannot drift apart quietly.
RECOVERY: dict[str, str] = {
    "control_dir": f"{_RUNBOOK}#control-directory-untrusted",
    "autonomy": f"{_RUNBOOK}#autonomy-fell-back-or-was-clamped",
    "queue": f"{_RUNBOOK}#queue-paused",
    "adversarial_skipped": f"{_RUNBOOK}#an-adversarial-phase-did-not-run",
}


@dataclass(frozen=True)
class SafeModeReason:
    """One degraded state the factory is currently in."""

    #: Which reader produced this. One of the keys of :data:`RECOVERY`.
    source: str
    #: A human sentence, taken verbatim from the existing signal wherever
    #: that signal already has words of its own.
    detail: str
    #: The runbook anchor to read, e.g. ``docs/runbook.md#queue-paused``.
    recovery: str


def _reason(source: str, detail: str) -> SafeModeReason:
    return SafeModeReason(
        source=source, detail=detail, recovery=RECOVERY[source],
    )


def _control_dir_reasons(root_dir: Path) -> list[SafeModeReason]:
    from kstrl.statedir import control_untrusted_reason

    untrusted = control_untrusted_reason(root_dir)
    return [_reason("control_dir", untrusted)] if untrusted else []


def _autonomy_reasons(root_dir: Path) -> list[SafeModeReason]:
    from kstrl.autonomy import (
        AutonomyConfig,
        AutonomyState,
        resolve_runtime_level,
    )
    from kstrl.policy import PolicyConfig

    config = AutonomyConfig.load(root_dir)
    if not config.enabled:
        # A ladder that is switched off is not a ladder that fell down.
        # ``[autonomy] enabled`` is False by default, so reporting a
        # damaged autonomy.json here would put every repo that never
        # opted in into safe mode over a file nothing reads.
        return []

    state = AutonomyState.load(root_dir)
    reasons: list[SafeModeReason] = []
    if state.degraded_reason:
        reasons.append(_reason("autonomy", state.degraded_reason))

    level, clamps = resolve_runtime_level(
        state,
        config,
        policy_enabled=PolicyConfig.load(root_dir).enabled,
        root_dir=root_dir,
    )
    if int(level) < state.level:
        # ``clamps`` carries the ceiling's own words. The fallback keeps
        # the predicate speaking if a future ceiling ever clamps without
        # writing a note, rather than going quiet.
        notes = clamps or [f"clamped to L{int(level)}"]
        reasons.extend(
            _reason(
                "autonomy",
                f"running at L{int(level)}, earned level is "
                f"L{state.level}: {note}",
            )
            for note in notes
        )
    return reasons


def _queue_reasons(root_dir: Path) -> list[SafeModeReason]:
    from kstrl.workqueue import Queue, QueueConfig

    pause = Queue(root_dir, QueueConfig.load(root_dir)).pause_state()
    # ``active`` rather than ``paused``: a lapsed ``resume_after`` means
    # the queue is admitting work again, which is what the daemon's own
    # ``is_paused`` asks.
    if not pause.active():
        return []
    return [_reason("queue", pause.reason or "paused, no reason recorded")]


def _adversarial_reasons(root_dir: Path) -> list[SafeModeReason]:
    from kstrl import events as ev
    from kstrl.reducer import latest_run_dir

    runs_root = root_dir / ".kstrl" / "runs"
    if runs_root.exists() and not runs_root.is_dir():
        # ``_v2_run_dirs`` swallows this into "no runs", which reads as
        # "nothing was skipped" - a fail-open on a question about
        # whether a gate ran.
        return [_reason(
            "adversarial_skipped",
            f"could not read {runs_root}: not a directory",
        )]

    run_dir = latest_run_dir(root_dir)
    if run_dir is None:
        return []

    # Scanned from the raw stream rather than folded: ``PhaseSkipped`` has
    # exactly one emitter and is never capped, while the reducer's
    # ``recent_findings`` keeps only the last MAX_RECENT_FINDINGS per
    # component and would lose the record behind a noisy component.
    skipped: dict[str, set[str]] = {phase: set() for phase in GATED_PHASES}
    for event in ev.read_events(run_dir / "events.jsonl"):
        if isinstance(event, ev.PhaseSkipped) and event.phase in skipped:
            skipped[event.phase].add(event.component)

    reasons: list[SafeModeReason] = []
    for phase in GATED_PHASES:
        components = skipped[phase]
        if not components:
            continue
        reasons.append(_reason(
            "adversarial_skipped",
            f"{phase} did not run for {len(components)} component(s) "
            f"in run {run_dir.name}",
        ))
    return reasons


#: Readers in evaluation order. The order is the order reasons appear.
_READERS: tuple[tuple[str, Callable[[Path], list[SafeModeReason]]], ...] = (
    ("control_dir", _control_dir_reasons),
    ("autonomy", _autonomy_reasons),
    ("queue", _queue_reasons),
    ("adversarial_skipped", _adversarial_reasons),
)


def safe_mode_reasons(root_dir: Path) -> list[SafeModeReason]:
    """Every degraded state the factory is currently in. Empty is nominal.

    Never raises. Each reader is wrapped on its own, so one that fails
    cannot hide the answers of the other three, and its failure becomes a
    reason of its own source: a signal that could not be read is not
    evidence that the signal is clear.

    See the module docstring for what "read-only" means here and for why
    an exactly duplicated ``detail`` is dropped.
    """
    reasons: list[SafeModeReason] = []
    seen: set[str] = set()
    for source, read in _READERS:
        try:
            found = read(root_dir)
        except Exception as exc:  # noqa: BLE001 - a failed read IS a reason
            found = [_reason(
                source, f"could not read the {source} signal: {exc}",
            )]
        for reason in found:
            if reason.detail in seen:
                continue
            seen.add(reason.detail)
            reasons.append(reason)
    return reasons
