"""One record per ecosystem kstrl knows, and the one resolver of Phase 1's commands (#635).

Before #635 the same facts lived in five tables in ``kstrl/init_cmd.py``
keyed on the string ``_detect_project_context`` returned, and the gate
defaults lived in ``kstrl/verify.py``, which never asked what the tree
was. This module holds both: :data:`TOOLCHAINS` is the record per
ecosystem, :func:`detect` chooses one for a tree, and :func:`resolve` is
the only reader of a record's commands for a gate.

It changes no behaviour. :func:`resolve` still gives an unset key the
Python command in every tree, as the gates did before #635; whether a
detected non-Python record supplies its own commands is a later slice's
decision. ``tests/test_python_toolchain_identity_e2e.py`` holds every
toolchain surface byte-identical across the move.

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

#: The three Phase 1 gates a record can name a command for.
Capability = Literal["test", "typecheck", "lint"]

#: Gate default when ``[verify] test_command`` is unset.
DEFAULT_TEST_COMMAND = "uv run pytest"

#: Gate default when ``[verify] lint_command`` is unset.
DEFAULT_LINT_COMMAND = "uv run ruff check ."

#: Gate fallback when ``[verify] typecheck_command`` is unset AND the
#: project does not scope mypy itself. :func:`python_typecheck_default`
#: prefers ``uv run mypy`` (no path) whenever pyproject.toml does.
DEFAULT_TYPECHECK_COMMAND = "uv run mypy ."

#: What :func:`python_typecheck_default` uses instead when the project has
#: scoped mypy via ``[tool.mypy] files`` or ``packages``.
SCOPED_TYPECHECK_COMMAND = "uv run mypy"


@dataclass(frozen=True)
class Commands:
    """A record's command per capability. None means the ecosystem has no
    such step. "" is never stored here: "" in ``[verify]`` is the operator
    turning a gate off (#621), and a record cannot say that for them."""

    test: str | None
    typecheck: str | None
    lint: str | None


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
    #: What `ks init` seeds, commented, into kstrl.toml ``[verify]`` on a
    #: non-Python tree. The Python record's are the gate defaults.
    commands: Commands


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
        commands=Commands(
            test=DEFAULT_TEST_COMMAND,
            typecheck=DEFAULT_TYPECHECK_COMMAND,
            lint=DEFAULT_LINT_COMMAND,
        ),
    ),
    "Rust": Toolchain(
        id="Rust",
        markers=("Cargo.toml",),
        ignores=("target/",),
        lockfiles=("Cargo.lock",),
        # --all-targets (#621): without it neither command reads #[cfg(test)]
        # code. Measured on cargo 1.94: `cargo check` exits 0 on a test with a
        # type error and `cargo clippy -- -D warnings` exits 0 on
        # `assert!(true)`; with the flag both exit 101.
        commands=Commands(
            test="cargo test",
            typecheck="cargo check --all-targets",
            lint="cargo clippy --all-targets -- -D warnings",
        ),
    ),
    "TypeScript": Toolchain(
        id="TypeScript",
        markers=("package.json",),
        ignores=_JS_IGNORES,
        lockfiles=_JS_LOCKFILES,
        commands=Commands(test="npm test", typecheck="npx tsc --noEmit", lint="npx eslint ."),
    ),
    "JavaScript": Toolchain(
        id="JavaScript",
        markers=("package.json",),
        ignores=_JS_IGNORES,
        lockfiles=_JS_LOCKFILES,
        commands=Commands(test="npm test", typecheck=None, lint="npx eslint ."),
    ),
    "Go": Toolchain(
        id="Go",
        markers=("go.mod",),
        ignores=("bin/", "*.test", "*.out"),
        lockfiles=("go.sum",),
        commands=Commands(test="go test ./...", typecheck="go vet ./...", lint="golangci-lint run"),
    ),
    "Kotlin": Toolchain(
        id="Kotlin",
        markers=("build.gradle.kts",),
        ignores=_JVM_IGNORES,
        lockfiles=(),
        commands=Commands(test="mvn test", typecheck=None, lint=None),
    ),
    "Java": Toolchain(
        id="Java",
        markers=("pom.xml", "build.gradle"),
        ignores=_JVM_IGNORES,
        lockfiles=(),
        commands=Commands(test="mvn test", typecheck=None, lint=None),
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

    The one copy of this test: ``doctor.check_verify_commands``, the
    Phase 1 gates (``verify._command_not_run``) and the fixture runner
    all ask it.
    """
    return detect(root) is TOOLCHAINS["Python"]


def record_test_command(root: Path, toolchain: Toolchain) -> str | None:
    """``toolchain``'s test command in ``root``.

    The only command that needs the tree, not just the ecosystem: a JVM
    tree that ships the Gradle wrapper runs its tests through it.
    """
    if toolchain.id in ("Java", "Kotlin") and (root / "gradlew").exists():
        return "./gradlew test"
    return toolchain.commands.test


def python_typecheck_default(cwd: Path) -> str:
    """Choose a sensible default mypy invocation for ``cwd``.

    Generic ``uv run mypy .`` is hostile to projects whose pyproject.toml
    deliberately scopes mypy via ``[tool.mypy] files`` or ``packages``:
    the ``.`` argument overrides those settings and pulls in test files
    or vendored code that the project never intended to typecheck. When
    the project has configured its own mypy scope, defer to it by
    invoking ``uv run mypy`` with no path argument (mypy then reads the
    config). When no such config is present, fall back to the broad
    ``uv run mypy .`` so a green-field project still gets coverage.

    This is the Gap 2 fix from the end-to-end factory validation run:
    the factory's verify command was overriding the project's own
    typecheck scope, leading to Phase 1 failures on diffs that were
    actually fine. Gap 2 landed on the gate and not on ``ks init``, which
    kept scaffolding ``mypy src/ --strict`` into CLAUDE.md - the very
    shape it identified as wrong. #261 closed that half.
    """
    import tomllib

    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        # The read is outside the guard for the same reason it is in
        # ``config.load_toml_document``: an I/O fault is not a parse
        # fault. Here it makes no difference to the caller, since both
        # end at the same default, but a rule applied at one of two
        # sites and not the other is a rule the next author has to guess
        # at.
        try:
            raw = pyproject.read_bytes()
        except OSError:
            return DEFAULT_TYPECHECK_COMMAND
        try:
            data = tomllib.loads(raw.decode())
        except Exception:
            # ``Exception``, not an enumeration of what tomllib is
            # believed to raise: see ``kstrl.config.load_toml_document``
            # for the argument and ``tests/test_toml_readers.py`` for
            # the guard. The one fact local to THIS site is that a
            # pyproject.toml is not the operator's kstrl.toml, so it
            # fails to a documented default rather than to an error,
            # which is why catching the whole class costs nothing here.
            return DEFAULT_TYPECHECK_COMMAND
        mypy_section = data.get("tool", {}).get("mypy", {})
        if isinstance(mypy_section, dict):
            # Acknowledged edge case: this heuristic does not consult
            # ``[[tool.mypy.overrides]]`` (per-module relaxation) or
            # modules-only configs. If a project relaxes via overrides
            # but doesn't set ``files``/``packages``, the broad
            # ``uv run mypy .`` default would override the relaxation.
            # Real-world rare. Users can always override explicitly via
            # ``--typecheck-command`` or env var.
            if mypy_section.get("files") or mypy_section.get("packages"):
                return SCOPED_TYPECHECK_COMMAND
    return DEFAULT_TYPECHECK_COMMAND


def resolve(cwd: Path, capability: Capability, configured: str | None) -> str:
    """The exact command Phase 1 runs for ``capability`` in ``cwd``.

    The only reader of a record's commands for a gate. The operator's
    ``[verify]`` value wins, and "" is the gate turned off (#621). An
    unset key gets the Python record's command in every tree, as it did
    before #635.
    """
    if configured is not None:
        return configured
    if capability == "test":
        return DEFAULT_TEST_COMMAND
    if capability == "lint":
        return DEFAULT_LINT_COMMAND
    return python_typecheck_default(cwd)
