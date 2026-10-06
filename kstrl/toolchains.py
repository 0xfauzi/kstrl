"""The root build manifests kstrl still names (#696).

What is left of the per-ecosystem records after #696 slice 6. ``ks init``
reads no language any more: it writes the same files on every tree, and a
confirmed ``[stack]`` is the only statement of how a project builds. One
reader remains, which #696 slice 7 removes: the decompose build-manifest
refusal reads :data:`BUILD_MANIFESTS`.
"""

from __future__ import annotations

from pathlib import Path

#: Every root build manifest kstrl names. ``decompose.ROOT_BUILD_MANIFESTS``
#: is this set, so no component may be scoped to one, and
#: ``init_cmd.build_manifest_blocker`` refuses a repository holding none of
#: them and no ``[stack]``.
BUILD_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "Cargo.toml",
    "package.json",
    "go.mod",
    "build.gradle.kts",
    "pom.xml",
    "build.gradle",
)


def has_build_manifest(root: Path) -> bool:
    """Whether any of :data:`BUILD_MANIFESTS` exists at ``root``."""
    return any((root / name).exists() for name in BUILD_MANIFESTS)
