"""Every agent reply a calibration run scores, kept beside its baseline (#523).

A capture used to keep a verdict per run and throw the reply away, so a
refused, missed or false-positive run could only be read by paying for
another run. Now each agent call a run makes is recorded where the harness
drains it (``_collect`` in ``tests/test_calibration.py`` for the reviewer,
security and architect arms, :class:`BoundedAgent` in
``tests/helpers/calibration_integration_fixture.py`` for the integration
arms), and ``_DetectionReport`` writes one file per recorded run:

    <results dir>/replies-<timestamp>/<role>/<fixture_id>/run-<n>.json

``<timestamp>`` is the one in ``baseline-<timestamp>.json``, and ``n`` counts
from 1 in the order the baseline lists that fixture's runs, so ``run-1.json``
is ``runs[0]``. The file holds the run's record (the same fields the baseline
reads) plus ``run`` and ``calls``: one entry per agent call, each with the
lines the agent streamed and its ``final_message``. The scored text is one
of those two; which one is ``kstrl.decompose._select_agent_output``'s rule.

A run that made no recorded call raises instead of writing an empty file, so
the fixture never reaches ``complete_fixture`` and the baseline is saved as a
partial capture that ``load_baseline`` refuses by name. A failed write does
the same, because the reply is written before the run is recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_json

#: The agent calls made since the harness last took them, in call order.
CALLS: list[dict[str, Any]] = []


def keep_call(streamed: Sequence[str], final_message: object) -> None:
    """Record one finished (or abandoned) agent call."""
    CALLS.append(
        {
            "streamed": list(streamed),
            "final_message": None if final_message is None else str(final_message),
        }
    )


def take_calls() -> list[dict[str, Any]]:
    """Every call recorded since the last take, and forget them."""
    taken = list(CALLS)
    CALLS.clear()
    return taken


def replies_dir(results_dir: Path, timestamp: str) -> Path:
    """Where the replies of the capture saved as ``baseline-<timestamp>.json`` go."""
    return results_dir / f"replies-{timestamp}"


def write_reply(
    directory: Path,
    record: Mapping[str, Any],
    run: int,
    calls: Sequence[Mapping[str, Any]],
) -> Path:
    """Write run ``run`` of ``record``'s fixture with its agent calls.

    Raises AssertionError when ``calls`` is empty: every scored run asks an
    agent, so an empty list means the arm reached its agent some way this
    module does not see, and a capture that saved it would look complete
    while holding no reply.
    """
    role = str(record["role"])
    fixture_id = str(record["fixture_id"])
    if not calls:
        raise AssertionError(
            f"{role}/{fixture_id} run {run}: no agent reply was kept. Every "
            "calibration agent call must be drained by _collect or BoundedAgent (#523)."
        )
    path = directory / role / fixture_id / f"run-{run}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {**record, "run": run, "calls": list(calls)})
    return path
