"""Write a shell script a PATH lookup can execute.

Here rather than in a test module because `tests/helpers/` is the
direction test dependencies run in: `tests/helpers/fakegh.py` imported
this out of `tests/test_serve_seam.py`, so renaming a private name in
that test module broke collection of two unrelated suites.
"""

from __future__ import annotations

import stat
from pathlib import Path


def write_executable(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and make it executable; return the path."""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
