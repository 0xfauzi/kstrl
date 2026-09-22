"""A run's own dependents say what happened to them, not just the manifest (#448/#457).

``manifest.cascade_skip`` marks a failed component's transitive
dependents SKIPPED in the manifest, but the run wrote no event for it.
So a dependent skipped by THIS run read as pending on the board (which
folds only this run's events), while ``ks status --no-tui`` (which
reads the manifest) said skipped. Same defect class as #448 itself: an
event the reducer needs was never written.

The fix is one helper, ``ComponentPipeline._cascade_skip``, that calls
``manifest.cascade_skip`` and emits ``ComponentSkipped`` for every id it
returns. The four call sites in ``kstrl/pipeline.py`` route through it.

Two things are pinned here: an end-to-end run where a dependent is
skipped, read back through the reducer and through ``ks status
--no-tui`` in agreement; and a structural census, so a fifth call site
that skips the manifest without saying so on the stream fails red
instead of passing quietly, the way the original defect did.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import tests.test_scope_snapshot as scope_snapshot
from kstrl import reducer
from kstrl.manifest import Component
from kstrl.reducer import load_run_state
from tests.test_harness_path_scope import (
    AUTHORED,
    COMPONENT_ID,
    _component,
    _manifest,
    _setup_project,
    _write_prd,
)
from tests.test_scope_snapshot import _run, _Seams

DEPENDENT_ID = "after-format"
TRANSITIVE_ID = "after-after-format"


class TestACascadeSkipIsRecordedOnTheStream:
    def test_a_dependent_of_a_failing_component_reads_skipped_not_pending(
        self, tmp_path: Path
    ) -> None:
        _setup_project(tmp_path)
        _write_prd(
            tmp_path / "scripts" / "kstrl" / "feature" / DEPENDENT_ID / "prd.json",
            AUTHORED,
        )
        _write_prd(
            tmp_path / "scripts" / "kstrl" / "feature" / TRANSITIVE_ID / "prd.json",
            AUTHORED,
        )
        dependent = Component(
            DEPENDENT_ID,
            DEPENDENT_ID,
            "depends on document-format",
            [COMPONENT_ID],
            f"scripts/kstrl/feature/{DEPENDENT_ID}/prd.json",
            f"kstrl/factory/{DEPENDENT_ID}",
        )
        transitive = Component(
            TRANSITIVE_ID,
            TRANSITIVE_ID,
            "depends on after-format",
            [DEPENDENT_ID],
            f"scripts/kstrl/feature/{TRANSITIVE_ID}/prd.json",
            f"kstrl/factory/{TRANSITIVE_ID}",
        )
        manifest = _manifest([_component(), dependent, transitive])
        manifest.save(tmp_path / "scripts" / "kstrl" / "manifest.json")

        seams = _Seams()
        orig_loop_result = scope_snapshot.LoopResult

        class _FailingLoopResult:
            """Makes ``_run``'s stubbed engineer loop report failure,
            so ``document-format`` exhausts its retries (max_retries=0
            in ``_run``'s FactoryConfig) and cascades a skip onto its
            dependent."""

            def __new__(cls, **kwargs: object) -> object:
                kwargs["completed"] = False
                kwargs["exit_code"] = 1
                return orig_loop_result(**kwargs)

        scope_snapshot.LoopResult = _FailingLoopResult
        try:
            result = _run(tmp_path, seams, manifest=manifest)
        finally:
            scope_snapshot.LoopResult = orig_loop_result

        assert result.failed == [COMPONENT_ID]
        assert result.skipped == [DEPENDENT_ID, TRANSITIVE_ID]

        state, source = load_run_state(tmp_path)
        assert source is not None
        assert state.components[COMPONENT_ID].status == "failed"
        assert state.components[DEPENDENT_ID].status == "skipped"
        assert not state.components[DEPENDENT_ID].carried
        assert state.components[TRANSITIVE_ID].status == "skipped"
        assert not state.components[TRANSITIVE_ID].carried

        proc = subprocess.run(
            [sys.executable, "-m", "kstrl", "status", "--no-tui", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0
        combined = proc.stdout + proc.stderr
        assert f"{COMPONENT_ID}: failed" in combined
        assert f"{DEPENDENT_ID}: skipped" in combined
        assert f"{TRANSITIVE_ID}: skipped" in combined


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Every node's immediate parent, since the stdlib ``ast`` module
    keeps none. Built fresh per file; these trees are small."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """The name of the nearest enclosing function, or ``"<module>"``."""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current.name
    return "<module>"


def _is_cascade_skip_call(node: ast.AST) -> bool:
    """Matches the attribute name alone, not a resolved receiver type.

    ``cascade_skip`` names exactly one method in ``kstrl/`` (defined on
    ``Manifest``; grepped), so a leaf-name match is exact here without
    needing alias or receiver resolution, and it still catches a call
    through a differently-typed local that happens to share the name.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cascade_skip"
    )


def _cascade_skip_call_sites() -> list[tuple[str, str]]:
    """Every call to something named ``cascade_skip`` in ``kstrl/``, paired
    with the function it sits in."""
    kstrl_dir = Path(reducer.__file__).parent
    sites: list[tuple[str, str]] = []
    for path in sorted(kstrl_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _parent_map(tree)
        rel = str(path.relative_to(kstrl_dir.parent))
        sites.extend(
            (rel, _enclosing_function(node, parents))
            for node in ast.walk(tree)
            if _is_cascade_skip_call(node)
        )
    return sites


class TestCascadeSkipHasOneCaller:
    """A fifth call site that skips the manifest directly, bypassing the
    helper, would still cascade-skip the dependent - it would just not
    say so on the stream, which is #448's own defect recurring one call
    site over. The end-to-end test above cannot see that: it only knows
    what the four routed sites do today. This is structural instead."""

    def test_manifest_cascade_skip_is_called_from_exactly_one_place(self) -> None:
        sites = _cascade_skip_call_sites()
        assert sites == [("kstrl/pipeline.py", "_cascade_skip")], (
            "manifest.cascade_skip must be called from exactly one place, "
            "kstrl/pipeline.py's _cascade_skip helper, so every site that "
            "skips a dependent also emits ComponentSkipped for it. "
            f"Found: {sites}"
        )
