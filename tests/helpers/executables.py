"""Write a shell script a PATH lookup can execute.

Here rather than in a test module because `tests/helpers/` is the
direction test dependencies run in: `tests/helpers/fakegh.py` imported
this out of `tests/test_serve_seam.py`, so renaming a private name in
that test module broke collection of two unrelated suites.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def write_executable(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and make it executable; return the path."""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def put_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    body: str,
    *,
    dirname: str = "fakebin",
    prepend: bool = True,
) -> Path:
    """Write ``body`` as an executable called ``name`` under
    ``tmp_path/dirname`` and put that directory on ``PATH``; return the
    directory (#152 simplify pass, D3: the third copy of this shape -
    ``tests/helpers/fakemutmut.py``'s two installers and
    ``tests/test_diff_mutation.py``'s git-only PATH build all wrote it by
    hand).

    ``prepend=True`` (the default) puts the fake directory FIRST on the
    existing ``PATH``, so it is found before the real tool without losing
    whatever else ``PATH`` already needs - the interpreter, ``git``.
    ``prepend=False`` REPLACES ``PATH`` outright, for a caller that must
    guarantee nothing else on the real ``PATH`` can answer for ``name``
    either: a test asserting ``shutil.which(name)`` is ``None`` needs that
    even on a machine that happens to have the real tool installed.
    """
    bindir = tmp_path / dirname
    bindir.mkdir(exist_ok=True)
    write_executable(bindir / name, body)
    new_path = str(bindir) if not prepend else f"{bindir}{os.pathsep}{os.environ['PATH']}"
    monkeypatch.setenv("PATH", new_path)
    return bindir
