"""One record per ecosystem kstrl knows: its markers, ignores and lockfiles (#635).

What is left of it after the #696 flag day (slice 4). The command half is
gone: kstrl no longer chooses a command for any tree, Python included, and
a confirmed ``[stack]`` is the only source of the commands it runs. What
remains is detection, read by ``ks init`` (slice 6), the decompose
build-manifest refusal (slice 7) and the fixture runner (slice 8), each of
which #696 removes in its own slice.

Prompt text is not here. The standards and antipatterns bodies stay
enrolled ``*_PROMPT`` constants in ``kstrl/init_cmd.py`` (H3), keyed by
:data:`ToolchainId`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from kstrl.jsonread import read_json

ToolchainId = Literal["Python", "Rust", "TypeScript", "JavaScript", "Go", "Java", "Kotlin"]


@dataclass(frozen=True)
class Toolchain:
    """What kstrl knows about one ecosystem."""

    id: ToolchainId
    #: Root files whose presence selects this record, first match wins in
    #: :data:`TOOLCHAINS` order. ``decompose.ROOT_BUILD_MANIFESTS`` is
    #: their union.
    markers: tuple[str, ...]
    #: Build output and caches `ks init` writes into .gitignore.
    #: Deliberately no lockfile and no .python-version: both pin a build,
    #: so both belong in version control. Measured, none of `uv run`,
    #: `uv sync`, `uv lock` or `uv venv` writes a .python-version, so it
    #: cannot appear mid-iteration the way a lockfile can.
    ignores: tuple[str, ...]
    #: Generated files that pin a build and therefore belong in version
    #: control, which `ks init` stages. An empty tuple is a STATED policy
    #: ("this toolchain has no lockfile"), not an omission (#201 review).
    #: Every name is in ``policy.LOCKFILE_BASENAMES``. Measured, not
    #: assumed: with none present, `uv run pytest` writes uv.lock,
    #: `cargo test` writes Cargo.lock and `npm install` writes
    #: package-lock.json. Go writes go.sum only once the module requires
    #: something, and Gradle/Maven have no lockfile by default.
    lockfiles: tuple[str, ...]


_JS_IGNORES = (
    "node_modules/",
    "dist/",
    "build/",
    "coverage/",
    ".next/",
    "*.tsbuildinfo",
)

# npm, yarn and pnpm each write their own; whichever exists is the one
# this project uses.
_JS_LOCKFILES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)

_JVM_IGNORES = (
    "target/",
    "build/",
    ".gradle/",
)

#: Every record, in detection order: first match wins. Kotlin precedes Java
#: because build.gradle.kts decides a JVM tree whatever else is beside it.
TOOLCHAINS: dict[ToolchainId, Toolchain] = {
    "Python": Toolchain(
        id="Python",
        markers=("pyproject.toml", "setup.py"),
        ignores=(
            "__pycache__/",
            "*.py[cod]",
            ".venv/",
            "venv/",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".coverage",
            "htmlcov/",
            "build/",
            "dist/",
            "*.egg-info/",
        ),
        lockfiles=("uv.lock", "poetry.lock", "Pipfile.lock"),
    ),
    "Rust": Toolchain(
        id="Rust",
        markers=("Cargo.toml",),
        ignores=("target/",),
        lockfiles=("Cargo.lock",),
    ),
    "TypeScript": Toolchain(
        id="TypeScript",
        markers=("package.json",),
        ignores=_JS_IGNORES,
        lockfiles=_JS_LOCKFILES,
    ),
    "JavaScript": Toolchain(
        id="JavaScript",
        markers=("package.json",),
        ignores=_JS_IGNORES,
        lockfiles=_JS_LOCKFILES,
    ),
    "Go": Toolchain(
        id="Go",
        markers=("go.mod",),
        ignores=("bin/", "*.test", "*.out"),
        lockfiles=("go.sum",),
    ),
    "Kotlin": Toolchain(
        id="Kotlin",
        markers=("build.gradle.kts",),
        ignores=_JVM_IGNORES,
        lockfiles=(),
    ),
    "Java": Toolchain(
        id="Java",
        markers=("pom.xml", "build.gradle"),
        ignores=_JVM_IGNORES,
        lockfiles=(),
    ),
}

if set(get_args(ToolchainId)) != set(TOOLCHAINS):
    raise RuntimeError(
        "kstrl.toolchains: every ToolchainId needs exactly one record in TOOLCHAINS; "
        f"ids without a record: {sorted(set(get_args(ToolchainId)) - set(TOOLCHAINS))}"
    )


def toolchain_named(language: str) -> Toolchain | None:
    """The record ``language`` names, or None for ``"unknown"``.

    ``language`` is the string ``init_cmd._detect_project_context``
    returns, which is a record's ``id`` or ``"unknown"``.
    """
    return next((toolchain for toolchain in TOOLCHAINS.values() if toolchain.id == language), None)


def _is_javascript(root: Path) -> bool:
    """A package.json tree with no ``typescript`` dependency and no tsconfig.json.

    Moved from ``init_cmd._detect_project_context`` with its outcomes
    unchanged: a package.json that is not valid JSON reads as TypeScript,
    and one that is empty or cannot be read as UTF-8 text reads as ``{}``.
    """
    try:
        text: str | None = (root / "package.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = None
    try:
        pkg = read_json(text or "{}")
        pkg = pkg if isinstance(pkg, dict) else {}
        dependencies = pkg.get("dependencies")
        dev_dependencies = pkg.get("devDependencies")
        deps = {
            **(dependencies if isinstance(dependencies, dict) else {}),
            **(dev_dependencies if isinstance(dev_dependencies, dict) else {}),
        }
        return "typescript" not in deps and not (root / "tsconfig.json").exists()
    except (OSError, ValueError):
        return False


def detect(root: Path) -> Toolchain | None:
    """The record for the ecosystem ``root``'s build manifests name, or None.

    First match wins, in :data:`TOOLCHAINS` order, so a JVM tree holding
    build.gradle.kts is Kotlin even beside pom.xml. TypeScript and
    JavaScript share package.json, and :func:`_is_javascript` reads it to
    choose.
    """
    for toolchain in TOOLCHAINS.values():
        if not any((root / marker).exists() for marker in toolchain.markers):
            continue
        if toolchain.id == "TypeScript" and _is_javascript(root):
            return TOOLCHAINS["JavaScript"]
        return toolchain
    return None


def is_python_project(root: Path) -> bool:
    """Whether :func:`detect` chooses the Python record for ``root`` (#621).

    The one copy of this test, which the fixture runner asks.
    """
    return detect(root) is TOOLCHAINS["Python"]
