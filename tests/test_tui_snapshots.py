"""Stage 3 PR G (TUI rewrite): SVG snapshot tests.

Kept deliberately few (overview and detail) at a fixed size over the
fixed-run_id fixture, so churn stays reviewable. Update
with: uv run pytest tests/test_tui_snapshots.py --snapshot-update
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ralph_py.tui.app import Mode, RalphTuiApp
from tests.helpers.fake_run import FakeRunSpec, write_fake_run

SIZE = (120, 36)


@pytest.fixture()
def fixed_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = write_fake_run(tmp_path, FakeRunSpec(components=3))
    return tmp_path, run_dir


def _app(root: Path, run_dir: Path) -> RalphTuiApp:
    # Poll interval high enough that no timer fires between the pilot
    # settling and the snapshot capture (determinism).
    return RalphTuiApp(
        run_dir=run_dir, root_dir=root, mode=Mode.DASH, poll_interval=60.0,
    )


def test_overview_snapshot(
    snap_compare: Any, fixed_run: tuple[Path, Path],
) -> None:
    root, run_dir = fixed_run
    assert snap_compare(_app(root, run_dir), terminal_size=SIZE)


def test_component_detail_snapshot(
    snap_compare: Any, fixed_run: tuple[Path, Path],
) -> None:
    root, run_dir = fixed_run

    async def open_detail(pilot: Any) -> None:
        pilot.app.open_component("comp-a")
        await pilot.pause()

    assert snap_compare(
        _app(root, run_dir), terminal_size=SIZE, run_before=open_detail,
    )
