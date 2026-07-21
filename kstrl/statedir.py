"""Runtime state-dir resolution.

Journals, worktrees, and knowledge all live under a single per-repo
state directory. Resolving it in one place keeps the name from drifting
between call sites.
"""

from __future__ import annotations

from pathlib import Path

STATE_DIR_NAME = ".kstrl"


def state_dir(root_dir: Path) -> Path:
    """Return the state directory for ``root_dir``."""
    return root_dir / STATE_DIR_NAME
