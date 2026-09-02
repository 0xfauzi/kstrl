"""Opening a home-shell screen on a project directory, once.

Three test files push ``EvolveScreen`` at a tmp_path and assert on what
it renders: one about a broken kstrl.toml, one about undecodable data
files, one about the screen's own three tabs. What they share is not a
pause count but a sequence of CONDITIONS, and stating it once is the
point of this module.

There are three, in the order the app satisfies them.

1. Home is already the active screen when the pilot arrives. That is a
   guarantee and not a race: ``App._process_messages`` dispatches
   ``events.Mount`` at app.py:3438 and only invokes the ready callback
   at :3458, and ``run_test`` waits on that callback before yielding,
   so ``KstrlTuiApp.on_mount`` has pushed home already. What the wait
   below adds is home's OWN children being mounted. The push does not
   need that, and an earlier draft of this docstring claimed it did:
   "a screen pushed before the app's on_mount lands under home" is
   false, and was corrected by a /simplify pass that checked it.
2. ``EvolveScreen`` composes a ``TabbedContent`` whose panes mount on a
   later frame. That frame is what the callers' ``#proposals-table`` /
   ``#patterns-table`` / ``#trends-table`` / ``#proposal-detail``
   queries used to race, and it is why the 0.2s pauses were here.
3. ``EvolveScreen.on_mount`` adds the columns to all three tables and
   THEN calls ``reload`` in one synchronous call. A poll can only run
   between messages, so a table that has columns is a screen whose
   on_mount has returned: the rows and the config-problem banner are
   already drawn. Nothing here can observe a half-run on_mount.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from textual.pilot import Pilot
from textual.widgets import DataTable

from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.evolve import EvolveScreen
from tests.helpers.settle import mounted, settled


def home_app(root_dir: Path) -> KstrlTuiApp:
    """The home shell on ``root_dir``, polling fast enough to test."""
    return KstrlTuiApp(root_dir=root_dir, mode=Mode.HOME, poll_interval=0.05)


async def evolve_on(app: KstrlTuiApp, pilot: Pilot[None]) -> EvolveScreen:
    """Push ``EvolveScreen`` on a live app and wait out its three tabs.

    The module docstring says which conditions and why. The last one is
    deliberately weaker than anything a caller asserts: "on_mount has
    returned" says nothing about how many rows landed or what the
    banner says, so a screen that loads the wrong thing still reaches
    the caller's assertion and fails there, in the caller's words.
    """
    await mounted(pilot, lambda: app.screen, "#home-commands")
    app.push_screen(EvolveScreen())
    # push_screen puts the screen on the stack before it mounts
    # anything, so this is the screen the waits below are about.
    screen = app.screen
    assert isinstance(screen, EvolveScreen)
    trends = await mounted(pilot, lambda: screen, "#trends-table")
    await settled(
        pilot,
        lambda: cast(DataTable, trends).columns,
        what="the evolve screen's on_mount to load its three tabs",
    )
    return screen


@asynccontextmanager
async def evolve_screen(root_dir: Path) -> AsyncIterator[tuple[EvolveScreen, Pilot[None]]]:
    """The evolve screen open on its own app, for a caller that wants both."""
    app = home_app(root_dir)
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await evolve_on(app, pilot)
        yield screen, pilot
