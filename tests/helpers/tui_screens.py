"""Opening a home-shell screen on a project directory, once.

Two test files push ``EvolveScreen`` at a tmp_path and assert on what it
renders: one about a broken kstrl.toml, one about undecodable data
files. The pauses below are not incidental - they are the empirical
precedent from ``tests/test_evolve_screen.py`` for THIS screen - so a
second copy of them is a second thing to get subtly wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from textual.pilot import Pilot

from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.evolve import EvolveScreen


def home_app(root_dir: Path) -> KstrlTuiApp:
    """The home shell on ``root_dir``, polling fast enough to test."""
    return KstrlTuiApp(root_dir=root_dir, mode=Mode.HOME, poll_interval=0.05)


@asynccontextmanager
async def evolve_screen(root_dir: Path) -> AsyncIterator[tuple[EvolveScreen, Pilot[None]]]:
    """The evolve screen open on ``root_dir``.

    The 0.2s pauses mirror ``tests/test_evolve_screen.py``, which is
    the empirical precedent for this screen: it composes a
    TabbedContent whose panes mount on a later frame, and every test
    there waits the same way.
    """
    app = home_app(root_dir)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        app.push_screen(EvolveScreen())
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, EvolveScreen)
        yield screen, pilot
