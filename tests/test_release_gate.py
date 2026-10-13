"""The [release] gate's containment guards (R8.7 slice 1, #154).

Slice 1 has no driver: nothing reachable from ``kstrl/release.py`` starts a
process or reads an environment variable, which is the whole containment
claim of ``kstrl/release.py``'s module docstring. T11 runs the module's
import CLOSURE against the ``EXPECTED_PROCESS_MODULES`` census
``tests/test_process_lifecycle.py`` owns, T12 runs ``ReleaseConfig.load``
under an environment that raises on every read, and a third test proves the
walk is not vacuously empty. Both replaced, in the #154 fix round (A2), a
per-file spelling walk and a substring check that a plant routed through
another module (``from kstrl.verify import run_scrubbed``, an environment
door opened through another module's ``from_env``) defeated.

The ``release_withheld`` reason table and the ``release_ref_from`` ordering
rules this file once pinned in memory were removed; the release row reaches
the journal through ``tests/test_retry_journal_rows.py`` and a stopped run
names itself through ``tests/test_shutdown.py``.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from kstrl import release
from tests.helpers.astwalk import KSTRL_PACKAGE, defining_origin
from tests.test_process_lifecycle import EXPECTED_PROCESS_MODULES


def _process_module_label(dotted: str) -> str:
    """``kstrl.proc.proc`` -> ``proc/proc.py``, the key format
    ``EXPECTED_PROCESS_MODULES`` uses (``tests/helpers/astwalk/corpus.py::label``,
    read relative to ``kstrl/``)."""
    return dotted.removeprefix("kstrl.").replace(".", "/") + ".py"


def _kstrl_imports(source_file: Path, *, top_level_only: bool) -> set[str]:
    """The ``kstrl.*`` modules ``source_file`` imports.

    ``top_level_only=False`` walks the WHOLE tree (deferred imports
    nested in a function or class body included); ``True`` looks only at
    statements directly in the module body. See
    :func:`_reachable_process_modules` for why the walk needs both.
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    nodes: list[ast.AST] = list(tree.body) if top_level_only else list(ast.walk(tree))
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kstrl"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("kstrl"):
            found.update(_holders(node.module, [a.name for a in node.names]))
    return found


def _holders(module: str, names: list[str]) -> set[str]:
    """The modules that hold ``names`` of ``from module import ...``.

    #863 groups modules into packages, and a module in a different package
    imports the package interface. For a package, the walk goes to the
    module that holds each name, as the import of that module did before
    the move, and not to each module of the package. If the resolver
    cannot prove one step (``guessed``), the walk also reads the whole
    ``__init__.py``, which can only find more."""
    source = _module_source(module)
    if source is None or source.name != "__init__.py":
        return {module}
    out: set[str] = set()
    for name in names:
        origin, guessed = defining_origin(f"{module}.{name}")
        if guessed:
            out.add(module)
            continue
        parts = origin.split(".")
        while len(parts) > 1 and _module_source(".".join(parts)) is None:
            parts.pop()
        out.add(".".join(parts))
    return out


def _reachable_process_modules(root_module: str) -> set[str]:
    """Every ``kstrl.*`` module reachable from ``root_module`` that is a
    key of ``EXPECTED_PROCESS_MODULES`` (#154 fix round, A2, T11).

    The root module (``kstrl.release`` itself) is walked WHOLE, deferred
    imports included: an import anywhere in this file, even inside a
    method, is this file reaching for it, and the plant that defeated
    the old per-file spelling check (``from kstrl.verify import
    run_scrubbed``) is a module-level import in a driver added to this
    file. Every module reached FROM there is walked at its TOP LEVEL
    only, deliberately not transitively-deferred: ``kstrl.config.config``
    (which ``ReleaseConfig.load`` genuinely imports) has an unrelated
    FUNCTION-scoped import, nowhere near ``load_toml_section`` or
    ``resolve_config_file``, that eventually reaches ``kstrl.git.git``
    (``operator_file_notices`` -> ``scaffold_ledger.shipped_label`` ->
    ``from kstrl import git``) - a real edge, but one release.py's own
    call path never crosses, and following it flags a census entry no
    change in this PR touches. Measured on the shipped tree: this
    two-tier walk reaches no key of ``EXPECTED_PROCESS_MODULES``; a full
    transitive walk (following every deferred import at every hop)
    reaches ``kstrl.git.git`` even with no driver present, which is a false
    positive this test must not have.
    """
    seen = _import_closure(root_module)
    return {m for m in seen if _process_module_label(m) in EXPECTED_PROCESS_MODULES}


def _module_source(name: str) -> Path | None:
    """The file of ``kstrl.x.y``: ``x/y.py``, or ``x/y/__init__.py`` for a
    package. #863 groups modules into packages, and a module in a
    different package imports the package (``from kstrl.config import
    X``). The ``__init__.py`` re-exports X from its module, so the walk
    reads the ``__init__.py`` and reaches that module."""
    parts = name.split(".")[1:]
    if not parts:
        return None
    module = KSTRL_PACKAGE.joinpath(*parts).with_suffix(".py")
    if module.is_file():
        return module
    package = KSTRL_PACKAGE.joinpath(*parts, "__init__.py")
    return package if package.is_file() else None


def _import_closure(root_module: str) -> set[str]:
    """Each ``kstrl.*`` module the walk of :func:`_reachable_process_modules`
    reaches from ``root_module``, with ``root_module``."""
    seen: set[str] = set()
    pending = [root_module]
    is_root = True
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        source = _module_source(name)
        if source is not None:
            pending.extend(_kstrl_imports(source, top_level_only=not is_root))
        is_root = False
    return seen


class _EnvironThatRefusesReads:
    """Swapped in for ``os.environ`` around one call (T12, A2): raises on
    every access, so a read reached from ANYWHERE in the call tree is
    caught, not only an ``os.environ``/``os.getenv`` spelled in this one
    file."""

    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"read env {key!r}")

    def get(self, key: str, default: object = None) -> object:
        raise AssertionError(f"read env {key!r}")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"checked env {key!r}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("iterated environ")

    def __len__(self) -> int:
        return 0


class TestReleaseModuleContainment:
    def test_the_release_module_reaches_no_spawning_module(self) -> None:
        """T11 (#154 fix round, A2). Replaces a per-file spelling walk,
        which a plant defeated: ``from kstrl.verify import
        run_scrubbed`` inside ``kstrl/release.py`` spells no process
        primitive ITSELF, so the old check
        (``process_primitive_spellings``) cleared it. This runs the
        import CLOSURE instead (see :func:`_reachable_process_modules`)
        and asserts none of it is a key of ``EXPECTED_PROCESS_MODULES``,
        so a spawn reached through any kstrl module - not only one
        spelled directly in this file - is caught.
        """
        spawning = _reachable_process_modules("kstrl.release")
        assert spawning == set(), (
            f"kstrl.release reaches {sorted(spawning)}, which spell a process "
            "primitive (EXPECTED_PROCESS_MODULES). Slice 1 has no driver; the "
            "driver slice must add its own row there rather than let one in "
            "through another module's import."
        )

    def test_the_walk_is_actually_exercised(self) -> None:
        """Without this the test above could be passing because the walk
        is vacuously empty (#324's own recorded failure mode), not
        because release.py is actually clean."""
        source = KSTRL_PACKAGE / "release.py"
        assert "kstrl.plan.manifest" in _kstrl_imports(source, top_level_only=False)
        # release.py imports the config package interface (#863 M1). The
        # walk goes to the modules that hold the names. If it stops at the
        # package, it is blind.
        assert "kstrl.config.config" in _kstrl_imports(source, top_level_only=False)
        assert "kstrl.config.config_toml" in _import_closure("kstrl.release")

    def test_the_release_module_reads_no_environment_variable(self, tmp_path: Path) -> None:
        """T12 (#154 fix round, A2). Behavioural rather than textual: the
        old check read this file's own source for ``os.environ`` /
        ``os.getenv`` substrings and a bare ``import os``, which a
        loader switched on by ANOTHER module's ``from_env`` (e.g.
        ``PolicyConfig.from_env()``) does not spell here at all. This
        runs ``ReleaseConfig.load`` under an environ that raises on
        every access, so a read anywhere in the call tree is caught.

        ``unittest.mock.patch.object`` as an explicit ``with`` block,
        not the ``monkeypatch`` fixture: pytest's own teardown chain
        (syrupy's session finish reads ``PYTEST_XDIST_WORKER`` via
        ``os.getenv``) runs after a fixture-scoped monkeypatch would
        have restored ``os.environ``, so that fixture's own machinery
        tripped this refusal. A ``with`` block restores inline, before
        control returns to pytest at all.
        """
        with patch.object(os, "environ", _EnvironThatRefusesReads()):
            config = release.ReleaseConfig.load(tmp_path)
        assert config == release.ReleaseConfig(enabled=False, environment="")
