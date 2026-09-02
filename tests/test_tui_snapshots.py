"""Stage 3 PR G (TUI rewrite): SVG snapshot tests.

Kept deliberately few (overview and detail) at a fixed size over the
fixed-run_id fixture, so churn stays reviewable. Update
with: uv run pytest tests/test_tui_snapshots.py --snapshot-update
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.widgets import component_table
from tests.helpers.fake_run import FakeRunSpec, write_fake_run
from tests.helpers.settle import drained, mounted

SIZE = (120, 36)


class _FrozenClock:
    """Stands in for the ``time`` module inside one widget module.

    ``component_table`` reads exactly one thing from ``time``, so this is
    the whole surface. Replacing the module REFERENCE in that module's
    globals rather than patching ``time.time`` itself keeps the freeze
    scoped to the widget under snapshot: every other clock in the
    process, pytest's own included, is untouched.
    """

    def __init__(self, frozen: float) -> None:
        self._frozen = frozen

    def time(self) -> float:
        return self._frozen


@pytest.fixture()
def fixed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The fixture run, with the board's age clock frozen where the run ended.

    ``test_overview_snapshot`` already froze the activity feed's stamps
    for determinism and missed this one. ``ComponentTable.update_state``
    renders ``int(time.time() - comp.last_event_ts)`` per component, and
    the fixture stamps its events with "now", so the stored SVG pins
    ``0s`` three times. Measured against this fixture: an offset of 0.9s
    still renders ``['0s', '0s', '0s']`` and 1.1s renders
    ``['1s', '1s', '1s']``. So any run where more than one second of real
    time passes between this fixture and the render produces a different
    SVG and fails, which is a load-dependent flake rather than a
    regression. Seen once locally in three full-suite runs and once on
    CI.

    Frozen AFTER ``write_fake_run`` returns, which is the same instant
    the stored snapshots were generated at, so the pinned ages stay
    ``0s`` and no snapshot has to move.
    """
    run_dir = write_fake_run(tmp_path, FakeRunSpec(components=3))
    monkeypatch.setattr(component_table, "time", _FrozenClock(time.time()))
    return tmp_path, run_dir


def _app(root: Path, run_dir: Path) -> KstrlTuiApp:
    # Poll interval high enough that no timer fires between the pilot
    # settling and the snapshot capture (determinism).
    return KstrlTuiApp(
        run_dir=run_dir,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=60.0,
    )


def test_overview_snapshot(
    snap_compare: Any,
    fixed_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The activity feed stamps wall-clock times; freeze for determinism.
    from kstrl.tui.widgets import activity

    monkeypatch.setattr(activity, "_stamp", lambda ts: "12:00:00")
    root, run_dir = fixed_run
    assert snap_compare(_app(root, run_dir), terminal_size=SIZE)


def test_component_detail_snapshot(
    snap_compare: Any,
    fixed_run: tuple[Path, Path],
) -> None:
    root, run_dir = fixed_run

    async def open_detail(pilot: Any) -> None:
        pilot.app.open_component("comp-a")
        # snap_compare captures the SVG AFTER this callback returns, so
        # a half-built screen here is a snapshot mismatch that reads as
        # a design regression. compose makes the transcript queryable
        # before the screen's own on_mount fills the header from the
        # store, so the mount is not enough on its own.
        await mounted(pilot, lambda: pilot.app.screen, "#transcript")
        await drained(
            pilot,
            pilot.app.screen,
            what="the detail screen's on_mount to fill it from the store",
        )

    assert snap_compare(
        _app(root, run_dir),
        terminal_size=SIZE,
        run_before=open_detail,
    )
