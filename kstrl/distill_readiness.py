"""How many distiller replies did not parse, over recent runs (#495).

The one reader of :attr:`kstrl.events.DistillResult.parse_failed`.
``ks evolve`` prints the line this module builds under "Learning
readiness", beside the fact-utilization numbers, because a distill whose
reply did not parse wrote no facts for a reason that is not "nothing to
say".

It reads the event stream, not the evolution journal: ``DistillResult``
is only on the stream. The window is the newest ``lookback_runs`` run
directories under ``.kstrl/runs/``, which is not the journal's window
(the journal counts run ids it holds). The line says "run(s)" and names
the count so the two are not read as one population.
"""

from __future__ import annotations

from pathlib import Path

from kstrl import events as ev
from kstrl.reducer import read_run_dir, run_dirs_newest_first


def distill_parse_failure_line(root_dir: Path, lookback_runs: int) -> str:
    """One readiness line: distills whose reply did not parse, of all
    distills, in the newest ``lookback_runs`` run directories.

    An unreadable ``.kstrl/runs/`` is reported as not counted, never as
    zero: a zero here is a measurement, and a directory that could not
    be listed measured nothing. A single unreadable ``events.jsonl`` is
    read as empty by :func:`kstrl.events.read_events` and so drops out
    of BOTH counts; the denominator shows it.
    """
    try:
        run_dirs = run_dirs_newest_first(root_dir)[:lookback_runs]
    except OSError as exc:
        return (
            f"  distill replies that did not parse: not counted, runs directory unreadable ({exc})"
        )
    distills = [
        event
        for run_dir in run_dirs
        for event in read_run_dir(run_dir)
        if isinstance(event, ev.DistillResult)
    ]
    failed = sum(1 for event in distills if event.parse_failed)
    return (
        f"  distill replies that did not parse: {failed} of {len(distills)} "
        f"distill(s) in the last {len(run_dirs)} run(s)"
    )
