"""The Python manifests kstrl still names (#696).

What is left of the per-ecosystem records after #696 slice 7. ``ks init``
reads no language, a confirmed ``[stack]`` is the only statement of how a
project builds, and the architect may scope a component to any build file.
One reader remains, which #696 slice 8 removes: the function-fixture runner
reads :func:`is_python_project`.
"""

from __future__ import annotations

from pathlib import Path

#: The manifests :func:`is_python_project` looks for.
PYTHON_MANIFESTS: tuple[str, ...] = ("pyproject.toml", "setup.py")


def is_python_project(root: Path) -> bool:
    """Whether ``root`` holds one of :data:`PYTHON_MANIFESTS` (#621).

    The one copy of this test, which the fixture runner asks.
    """
    return any((root / name).exists() for name in PYTHON_MANIFESTS)
