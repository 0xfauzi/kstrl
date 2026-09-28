"""#648: an inbox writer decides on the row it reads inside the control lock.

The inbox is one append-only log shared by every kstrl process of every
checkout of one origin. ``Inbox.add`` used to fold the log with no lock
held, bump the open item it found, and append that snapshot under the
lock. Two things were lost in the gap between the read and the append:

- a concurrent repeat of the same key: each process missed the other's
  open item, opened its own, and overwrote the other's occurrence count;
- an operator's decision: ``ks inbox approve`` landing in the gap was
  overwritten by the snapshot, so the item read OPEN again with nobody
  recorded as having decided it.

Both tests drive the real writers in real, separate processes: the
factory's health-breach filing path (``health:{metric}:{rule}`` is the
key every run repeats) and the real ``ks inbox approve`` command. Real
processes, not threads in this one, because ``flock`` is not re-entrant
across two opens in one process, so an in-process second writer would
deadlock against the hold it is meant to race.

THE FUSE IS REAL TIME. A lock taken in the wrong place is a deadlock,
and a deadlock hangs rather than failing, so every wait here has a
wall-clock bound and a hang is reported as HUNG.
"""

from __future__ import annotations

import io
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemStatus

pytest.importorskip("fcntl", reason="control_lock is flock, which is POSIX-only")

WRITERS = 4
REPEATS = 100
APPROVALS = 3
JOIN_TIMEOUT_S = 180.0
ROUND_TIMEOUT_S = 60.0
BREACH_KEY = "health:lead_time:over_limit"


def _file_breach(root: Path, run_id: str) -> None:
    """One call of the factory's own filing path. Fails loudly on a warning.

    ``_open_health_breach_items`` never raises: a failed write is a
    warning on the run's UI. A lost write here would read as a lost
    occurrence, so the child turns a warning into a non-zero exit.
    """
    from kstrl.factory import _open_health_breach_items
    from kstrl.health import HealthBreach
    from kstrl.ui.plain import PlainUI

    out = io.StringIO()
    breach = HealthBreach(
        metric="lead_time", rule="over_limit", value=9.0, limit=1.0, window_runs=5
    )
    _open_health_breach_items(root, [breach], run_id=run_id, ui=PlainUI(no_color=True, file=out))
    if out.getvalue():
        sys.stderr.write(out.getvalue())
        sys.exit(3)


def _repeat(root: str, who: str, barrier: object) -> None:
    """A factory run repeating one breach REPEATS times, after the others are ready."""
    barrier.wait(timeout=60)  # type: ignore[attr-defined]
    for _ in range(REPEATS):
        _file_breach(Path(root), f"run-{who}")


def _repeat_until_stopped(root: str, stop: str) -> None:
    """A factory repeating one breach until the test says stop, bounded.

    Writes how many repeats it filed to ``<stop>.count`` when it ends.
    """
    deadline = time.monotonic() + JOIN_TIMEOUT_S
    filed = 0
    while not Path(stop).exists() and time.monotonic() < deadline:
        _file_breach(Path(root), "run-repeater")
        filed += 1
        # Room for the operator's command to take the lock between two
        # repeats; flock is not FIFO.
        time.sleep(0.005)
    Path(f"{stop}.count").write_text(str(filed), encoding="utf-8")


def _join_all(procs: list[multiprocessing.process.BaseProcess]) -> None:
    try:
        for proc in procs:
            proc.join(JOIN_TIMEOUT_S)
            if proc.is_alive():
                pytest.fail(f"HUNG: a writer was still alive after {JOIN_TIMEOUT_S}s")
        for proc in procs:
            assert proc.exitcode == 0, f"a writer died: exitcode {proc.exitcode}"
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.kill()


def _breach_items(root: Path) -> list[InboxItem]:
    return [i for i in Inbox(root, InboxConfig()).items() if i.dedupe_key == BREACH_KEY]


def _ks_approve(root: Path, item_id: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kstrl", "inbox", "approve", item_id, "--root", str(root)],
        cwd=root,
        env=dict(os.environ),
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_four_factories_repeating_one_breach_leave_one_item_with_every_occurrence(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(WRITERS)
    procs = [
        ctx.Process(target=_repeat, args=(str(tmp_path), str(who), barrier))
        for who in range(WRITERS)
    ]
    for proc in procs:
        proc.start()
    _join_all(procs)

    items = _breach_items(tmp_path)
    assert [(i.status, i.occurrences) for i in items] == [(ItemStatus.OPEN, WRITERS * REPEATS)], (
        f"{len(items)} item(s) with {sum(i.occurrences for i in items)} occurrences: "
        "a repeat folded the log outside the control lock"
    )


def _next_open_item(root: Path, approved: list[str]) -> InboxItem:
    """The open breach item, failing if it is one the operator already approved."""
    deadline = time.monotonic() + ROUND_TIMEOUT_S
    while time.monotonic() < deadline:
        open_now = [i for i in _breach_items(root) if i.status is ItemStatus.OPEN]
        reopened = [i.id for i in open_now if i.id in approved]
        assert not reopened, (
            f"approved item(s) {reopened} read OPEN again: a repeat appended "
            "the row it read before the operator's approval"
        )
        if open_now:
            return open_now[0]
        time.sleep(0.01)
    pytest.fail(f"HUNG: no open {BREACH_KEY} item within {ROUND_TIMEOUT_S}s")


def test_an_operator_approval_survives_a_factory_repeating_the_same_breach(
    tmp_path: Path,
) -> None:
    stop = tmp_path / "stop"
    ctx = multiprocessing.get_context("spawn")
    repeater = ctx.Process(target=_repeat_until_stopped, args=(str(tmp_path), str(stop)))
    repeater.start()
    approved: list[str] = []
    try:
        for _ in range(APPROVALS):
            item_id = _next_open_item(tmp_path, approved).id
            _ks_approve(tmp_path, item_id)
            approved.append(item_id)
            # Let the repeater run over the approval before the next round looks.
            time.sleep(0.2)
    finally:
        stop.touch()
        _join_all([repeater])

    final = {i.id: i for i in _breach_items(tmp_path)}
    assert len(set(approved)) == APPROVALS, approved
    filed = int((tmp_path / "stop.count").read_text(encoding="utf-8"))
    counted = sum(i.occurrences for i in final.values())
    assert counted == filed, (
        f"{filed} repeats filed, {counted} counted across {len(final)} item(s): "
        "a decision appended the row it read before a repeat bumped it"
    )
    for item_id in approved:
        item = final[item_id]
        assert item.status is ItemStatus.APPROVED and item.decided_by, (
            f"{item_id[:8]} is {item.status} decided_by={item.decided_by!r}: "
            "the operator's approval was overwritten"
        )
