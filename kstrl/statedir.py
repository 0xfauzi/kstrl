"""Runtime state-dir resolution for the Ralph -> kstrl rename.

The state dir is ``.kstrl/`` after the rename. This module's only job is
the migration warning: when a repo still has legacy ``.ralph/`` state and
no ``.kstrl/`` yet, tell the operator ONCE how to keep that state. We
never auto-move (journals, worktrees, and knowledge live there - moving
them behind the operator's back is how state gets lost).
"""

from __future__ import annotations

import warnings
from pathlib import Path

_warned_legacy_state: set[Path] = set()


def state_dir(root_dir: Path) -> Path:
    """Return ``root_dir/.kstrl``, warning once if only ``.ralph`` exists."""
    primary = root_dir / ".kstrl"
    legacy = root_dir / ".ralph"
    if not primary.exists() and legacy.exists():
        if root_dir not in _warned_legacy_state:
            _warned_legacy_state.add(root_dir)
            warnings.warn(
                f"legacy state dir {legacy} found; new runs use {primary}. "
                "To keep existing journals/knowledge: mv .ralph .kstrl",
                DeprecationWarning,
                stacklevel=2,
            )
    return primary
