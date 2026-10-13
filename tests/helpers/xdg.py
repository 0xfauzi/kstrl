"""Reset the cached ``XDG_STATE_HOME`` resolution of ``kstrl.io.statedir``."""

from __future__ import annotations

from kstrl.io import statedir


def clear_xdg_state_home_cache() -> None:
    """Drop the cached ``XDG_STATE_HOME`` resolution."""
    statedir._resolved_xdg_home = None
    statedir._resolved_xdg_raw = None
