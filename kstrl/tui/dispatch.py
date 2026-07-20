"""Kind-aware initial screen stacks (TUI surface C5).

One helper shared by `ks dash`, `ks status --tui`, the embedded
command paths, and the home shell: which screens a run of a given
kind opens with. Factory runs get the board; decompose runs get the
architect view over the board; single-loop kinds get their
pseudo-component's detail screen when its id is known.
"""

from __future__ import annotations

from kstrl.tui.app import ScreenStackFactory
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.decompose import DecomposeScreen
from kstrl.tui.screens.overview import OverviewScreen


def initial_screens_for_kind(
    kind: str,
    *,
    observe_only: bool,
    component: str = "",
) -> ScreenStackFactory:
    """Bottom-first stack for a run of ``kind``.

    ``component`` names the pseudo-component detail screen to open for
    single-loop kinds; "understand" is implied for understand runs.
    Unknown kinds degrade to the plain board - a forward-compatible
    default, never an error.
    """
    if kind == "decompose":
        return lambda: [
            OverviewScreen(observe_only=observe_only),
            DecomposeScreen(),
        ]
    detail = component or ("understand" if kind == "understand" else "")
    if detail:
        return lambda: [
            OverviewScreen(observe_only=observe_only),
            ComponentScreen(detail),
        ]
    return lambda: [OverviewScreen(observe_only=observe_only)]
