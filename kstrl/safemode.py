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

**Absence of evidence is not evidence of absence.** Every reader here
had to be written so that "I could not look" is distinguishable from
"nothing is wrong". ``ev.read_events`` answers an unreadable file with an
empty list; ``reducer._v2_run_dirs`` answers an unreadable ``runs/`` with
an empty list; a run in flight has recorded no skip yet; a ``decompose``
run has no phase chain and so finishes clean without ever asking. Each of
those reads as nominal unless the reader is explicit about it, and each
would be a fail-open on exactly the question this module exists to
answer.

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

#: Run kind whose stream can carry a skipped adversarial gate. Only a
#: factory run drives the phase chain that emits ``PhaseSkipped``.
_SKIPPING_RUN_KIND = "factory"

#: How far back to look for a finished run while a newer one is in
#: flight. A backstop, not a policy: the factory lock means at most one
#: run is live, so the second entry almost always settles it.
_LOOKBACK_RUNS = 20

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
        source=source,
        detail=detail,
        recovery=RECOVERY[source],
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
                f"running at L{int(level)}, earned level is L{state.level}: {note}",
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


def _report_for_run(run_dir: Path) -> tuple[list[SafeModeReason], bool]:
    """One run's skipped gates, and whether that run finished.

    Read from the raw stream rather than the reducer's fold, because
    ``ComponentState.recent_findings`` is capped at 50 per component and
    would lose a skip behind a noisy component.

    A stream that cannot be read returns a reason and ``False``, never an
    empty list: ``ev.read_events`` answers OSError with ``[]``, and an
    empty list here would mean "nothing was skipped". ``False`` is also
    right for the caller's look-back, since a run we could not read has
    not answered the question either.
    """
    from kstrl import events as ev

    events_path = run_dir / "events.jsonl"
    try:
        # ONE strict snapshot. Opening to probe and then calling
        # ``read_events`` would open twice: a stream deleted between the
        # two opens comes back as [], which reads as "nothing skipped".
        raw = events_path.read_bytes()
    except FileNotFoundError:
        return [
            _reason(
                "adversarial_skipped",
                f"could not read run {run_dir.name}: no event stream "
                "(is [factory] progress_log_enabled false?)",
            )
        ], False
    except OSError as exc:
        return [
            _reason(
                "adversarial_skipped",
                f"could not read run {run_dir.name}: {exc}",
            )
        ], False

    skipped: dict[str, set[str]] = {phase: set() for phase in GATED_PHASES}
    finished = False
    torn = 0
    lines = raw.decode("utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        event = ev.parse_event_line(line)
        if event is None:
            # ``read_events`` drops these silently, so a corrupt
            # phase_skipped line followed by a valid factory_completed
            # reads as a clean finished run. A torn LAST line is normal
            # for a run being appended to right now; anything earlier is
            # damage, and damage is not a clean sensor.
            if line.strip() and index < len(lines) - 1:
                torn += 1
            continue
        if isinstance(event, ev.RunCompleted):
            finished = True
        elif isinstance(event, ev.PhaseSkipped) and event.phase in skipped:
            skipped[event.phase].add(event.component)

    reasons = [
        _reason(
            "adversarial_skipped",
            f"{phase} did not run for {len(skipped[phase])} component(s) in run {run_dir.name}",
        )
        for phase in GATED_PHASES
        if skipped[phase]
    ]
    if torn:
        reasons.append(
            _reason(
                "adversarial_skipped",
                f"could not read run {run_dir.name}: {torn} unparseable "
                "line(s) in the event stream, so a skipped phase could be "
                "hidden in the damage",
            )
        )
        # Damaged, so it has not answered: the caller keeps looking back.
        finished = False
    return reasons, finished


def _adversarial_reasons(root_dir: Path) -> list[SafeModeReason]:
    from kstrl.reducer import run_dirs_newest_first
    from kstrl.runid import run_kind

    # Only a factory run drives the phase chain, so only a factory run
    # can skip an adversarial gate. Without this filter a `ks decompose`
    # started afterwards becomes "the newest run", finishes clean because
    # it has no phases at all, and clears a factory run's skip.
    factory_runs = [
        d for d in run_dirs_newest_first(root_dir) if run_kind(d.name) == _SKIPPING_RUN_KIND
    ]
    runs = factory_runs[:_LOOKBACK_RUNS]
    if not runs:
        return []

    # Walk back until a run has FINISHED. "No skip recorded yet" is not
    # "no skip": run B writes its first event long before it reaches
    # review, so stopping at B would clear run A's skip the moment B
    # started. Every run passed on the way contributes its own skips -
    # a crashed run that did record one still recorded it, and dropping
    # that was this loop's own bug in the previous round.
    reasons: list[SafeModeReason] = []
    settled = False
    for run_dir in runs:
        run_reasons, finished = _report_for_run(run_dir)
        reasons.extend(run_reasons)
        if finished:
            settled = True
            break

    if not settled and len(factory_runs) > len(runs):
        # The walk hit the backstop with no finished run behind it, and
        # there are older runs it did not open. Silence here would be a
        # verdict the search never reached.
        reasons.append(
            _reason(
                "adversarial_skipped",
                f"could not determine whether the adversarial gates ran: no "
                f"finished factory run among the {_LOOKBACK_RUNS} most "
                f"recent of {len(factory_runs)}",
            )
        )
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
    try:
        # Migrate FIRST, so every reader below sees the same control
        # state. Without this the control reader reports leftover legacy
        # files as untrusted and the queue reader then migrates them
        # through its own ensure_control_state, leaving a reason whose
        # recovery target no longer exists by the time it is printed.
        from kstrl.statedir import ensure_control_state

        ensure_control_state(root_dir)
    except OSError:
        # Not fatal: the control reader below reports an unusable
        # control directory in its own words.
        pass

    reasons: list[SafeModeReason] = []
    control_details: set[str] = set()
    for source, read in _READERS:
        try:
            found = read(root_dir)
        except Exception as exc:  # noqa: BLE001 - a failed read IS a reason
            found = [
                _reason(
                    source,
                    f"could not read the {source} signal: {exc}",
                )
            ]
        if source == "control_dir":
            control_details = {reason.detail for reason in found}
        for reason in found:
            # The ONE known aliasing, and only it: Queue.pause_state
            # consults control_untrusted_reason itself and hands back
            # that exact string. Dropping every repeated detail instead
            # would let an operator's pause reason silently delete an
            # unrelated source and its recovery anchor.
            if source == "queue" and reason.detail in control_details:
                continue
            reasons.append(reason)
    return reasons
