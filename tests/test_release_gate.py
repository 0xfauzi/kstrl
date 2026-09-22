"""The [release] section and its gate (R8.7 slice 1, #154).

Pure, no I/O beyond parsing this module's own source for the two
containment guards (T11, T12). Nothing here starts a process or reads
an environment variable, which is the whole containment claim of slice
1 - see ``kstrl/release.py``'s module docstring.

T11 and T12 were replaced in the #154 fix round (A2): a per-file
spelling walk and a text substring check both measured GREEN today and
RED on a plant that routed through another module (``from kstrl.verify
import run_scrubbed``, and an environment door opened through another
module's ``from_env``), because neither guard follows anything past
this file's own source text. T11 now runs this module's import CLOSURE
against the same ``EXPECTED_PROCESS_MODULES`` census
``tests/test_process_lifecycle.py`` already owns, and T12 runs the
loader under an environment that raises on every read, so a spawn or an
env read reached through ANY module in the call tree is caught rather
than one spelled in this file.
"""

from __future__ import annotations

import ast
import itertools
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from kstrl import release
from kstrl.manifest import Component
from tests.helpers.astwalk import KSTRL_PACKAGE
from tests.test_process_lifecycle import EXPECTED_PROCESS_MODULES


def _component(comp_id: str, merge_sha: str, completed_at: str) -> Component:
    return Component(
        id=comp_id,
        title=comp_id.upper(),
        description="",
        dependencies=[],
        prd_path=f"scripts/kstrl/feature/{comp_id}/prd.json",
        branch_name=f"kstrl/factory/{comp_id}",
        merge_sha=merge_sha,
        completed_at=completed_at,
    )


_FULLY_PERMITTED = release.ReleaseInputs(
    release_enabled=True,
    environment="prod",
    run_clean=True,
    stopped=False,
    release_ref="c" * 40,
    policy_enabled=True,
    policy_deploy=True,
    ladder_deploy_permitted=None,
)


def _inputs(**overrides: object) -> release.ReleaseInputs:
    return replace(_FULLY_PERMITTED, **overrides)  # type: ignore[arg-type]


class TestReleaseWithheldVocabulary:
    def test_every_input_combination_yields_a_reason_from_the_vocabulary(self) -> None:
        for (
            release_enabled,
            policy_enabled,
            policy_deploy,
            run_clean,
            stopped,
            environment,
            ladder_deploy_permitted,
            release_ref,
        ) in itertools.product(
            (True, False),
            (True, False),
            (True, False),
            (True, False),
            (True, False),
            ("", "prod"),
            (None, True, False),
            ("", "c" * 40),
        ):
            inputs = release.ReleaseInputs(
                release_enabled=release_enabled,
                environment=environment,
                run_clean=run_clean,
                stopped=stopped,
                release_ref=release_ref,
                policy_enabled=policy_enabled,
                policy_deploy=policy_deploy,
                ladder_deploy_permitted=ladder_deploy_permitted,
            )
            reason = release.release_withheld(inputs)
            assert reason != ""
            assert reason in release.RELEASE_WITHHELD_REASONS

    def test_every_reason_in_the_vocabulary_is_produced_by_some_input(self) -> None:
        produced: set[str] = set()
        for (
            release_enabled,
            policy_enabled,
            policy_deploy,
            run_clean,
            stopped,
            environment,
            ladder_deploy_permitted,
            release_ref,
        ) in itertools.product(
            (True, False),
            (True, False),
            (True, False),
            (True, False),
            (True, False),
            ("", "prod"),
            (None, True, False),
            ("", "c" * 40),
        ):
            inputs = release.ReleaseInputs(
                release_enabled=release_enabled,
                environment=environment,
                run_clean=run_clean,
                stopped=stopped,
                release_ref=release_ref,
                policy_enabled=policy_enabled,
                policy_deploy=policy_deploy,
                ladder_deploy_permitted=ladder_deploy_permitted,
            )
            produced.add(release.release_withheld(inputs))
        assert produced == set(release.RELEASE_WITHHELD_REASONS)


class TestReleaseWithheldDirections:
    def test_policy_disabled_withholds_even_when_deploy_is_true(self) -> None:
        assert release.release_withheld(_inputs(policy_enabled=False)) == "policy_disabled"

    def test_an_absent_environment_withholds(self) -> None:
        assert release.release_withheld(_inputs(environment="")) == "environment_unset"

    def test_a_fully_permitted_run_still_withholds_for_no_driver(self) -> None:
        assert release.release_withheld(_inputs()) == "no_driver"

    def test_a_stopped_run_names_the_stop_rather_than_the_generic_not_clean(self) -> None:
        """#154 fix round, A1: a stopped run gets its own reason rather
        than the generic ``run_not_clean`` a run_is_clean(stopped=True)
        result would otherwise fall through to."""
        assert release.release_withheld(_inputs(run_clean=False, stopped=True)) == "run_stopped"


class TestReleaseRefFrom:
    def test_the_release_ref_is_the_last_merge_and_a_tie_goes_to_the_later_component(
        self,
    ) -> None:
        components = [
            _component("no-merge", merge_sha="", completed_at="2026-01-03T00:00:00Z"),
            _component("first", merge_sha="a" * 40, completed_at="2026-01-01T00:00:00Z"),
            _component("tie-earlier", merge_sha="b" * 40, completed_at="2026-01-02T00:00:00Z"),
            _component("tie-later", merge_sha="c" * 40, completed_at="2026-01-02T00:00:00Z"),
        ]
        assert release.release_ref_from(components) == "c" * 40
        assert release.release_ref_from([]) == ""
        assert release.release_ref_from([components[0]]) == ""

    def test_the_release_ref_is_chosen_by_completed_at_not_manifest_order(self) -> None:
        """The component that merged last sits FIRST in manifest order,
        so a rule that took the last manifest entry would answer 'c'.
        """
        components = [
            _component("late-but-early", merge_sha="d" * 40, completed_at="2026-01-05T00:00:00Z"),
            _component("first", merge_sha="a" * 40, completed_at="2026-01-01T00:00:00Z"),
            _component("tie-earlier", merge_sha="b" * 40, completed_at="2026-01-02T00:00:00Z"),
            _component("tie-later", merge_sha="c" * 40, completed_at="2026-01-02T00:00:00Z"),
        ]
        assert release.release_ref_from(components) == "d" * 40

    def test_since_excludes_a_merge_a_previous_run_left_on_the_manifest(self) -> None:
        """#154 fix round, A1b: ``merge_sha`` persists across runs, so a
        merge dated before ``since`` must not be reported as THIS run's
        ref even though it is still the only merge on the manifest."""
        components = [
            _component("alpha", merge_sha="a" * 40, completed_at="2020-01-01T00:00:00Z"),
        ]
        assert release.release_ref_from(components, since="2020-01-01T00:00:00Z") == "a" * 40
        assert release.release_ref_from(components, since="2020-01-02T00:00:00Z") == ""

    def test_since_defaults_to_no_restriction(self) -> None:
        components = [
            _component("alpha", merge_sha="a" * 40, completed_at="2000-01-01T00:00:00Z"),
        ]
        assert release.release_ref_from(components) == "a" * 40


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
