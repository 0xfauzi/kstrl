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
from tests.helpers.astwalk import KSTRL_PACKAGE
from tests.test_process_lifecycle import EXPECTED_PROCESS_MODULES


def _process_module_label(dotted: str) -> str:
    """``kstrl.agents.proc`` -> ``agents/proc.py``, the key format
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
            found.add(node.module)
    return found


def _reachable_process_modules(root_module: str) -> set[str]:
    """Every ``kstrl.*`` module reachable from ``root_module`` that is a
    key of ``EXPECTED_PROCESS_MODULES`` (#154 fix round, A2, T11).

    The root module (``kstrl.release`` itself) is walked WHOLE, deferred
    imports included: an import anywhere in this file, even inside a
    method, is this file reaching for it, and the plant that defeated
    the old per-file spelling check (``from kstrl.verify import
    run_scrubbed``) is a module-level import in a driver added to this
    file. Every module reached FROM there is walked at its TOP LEVEL
    only, deliberately not transitively-deferred: ``kstrl.config``
    (which ``ReleaseConfig.load`` genuinely imports) has an unrelated
    FUNCTION-scoped import, nowhere near ``load_toml_section`` or
    ``resolve_config_file``, that eventually reaches ``kstrl.git``
    (``configured_path_errors`` -> ``init_cmd.shipped_label`` ->
    ``from kstrl import git``) - a real edge, but one release.py's own
    call path never crosses, and following it flags a census entry no
    change in this PR touches. Measured on the shipped tree: this
    two-tier walk reaches no key of ``EXPECTED_PROCESS_MODULES``; a full
    transitive walk (following every deferred import at every hop)
    reaches ``kstrl.git`` even with no driver present, which is a false
    positive this test must not have.
    """
    seen: set[str] = set()
    pending = [root_module]
    is_root = True
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")[1:]
        source = KSTRL_PACKAGE.joinpath(*parts).with_suffix(".py") if parts else None
        if source is not None and source.is_file():
            pending.extend(_kstrl_imports(source, top_level_only=not is_root))
        is_root = False
    return {m for m in seen if _process_module_label(m) in EXPECTED_PROCESS_MODULES}


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
        assert "kstrl.manifest" in _kstrl_imports(source, top_level_only=False)
        assert "kstrl.config" in _kstrl_imports(source, top_level_only=False)

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
