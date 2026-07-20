"""TUI surface B1: non-mutating retry preview + prepare extraction."""

from __future__ import annotations

import copy
import io
from pathlib import Path

import pytest

from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.retry_plan import prepare_retry, preview_retry
from kstrl.ui.plain import PlainUI


def _failed_manifest() -> Manifest:
    manifest = Manifest(
        version="1", spec_file="s", project_name="t",
        base_branch="main", single_pr=False,
        components=[
            Component(
                id=i, title=i, description="", dependencies=[],
                prd_path=f"scripts/kstrl/feature/{i}/prd.json",
                branch_name=f"kstrl/{i}",
            )
            for i in ("comp-a", "comp-b", "comp-c")
        ],
    )
    a, b, _ = manifest.components
    b.dependencies = ["comp-a"]
    a.status = ComponentStatus.FAILED.value
    a.error = "boom"
    b.status = ComponentStatus.SKIPPED.value
    b.error = "Dependency 'comp-a' failed"
    manifest.components[2].status = ComponentStatus.COMPLETED.value
    return manifest


class TestPreview:
    def test_preview_leaves_manifest_untouched(self) -> None:
        manifest = _failed_manifest()
        snapshot = copy.deepcopy(manifest)

        preview = preview_retry(manifest, "comp-a")

        assert preview.component_id == "comp-a"
        assert preview.reset_dependents == ["comp-b"]
        assert preview.failed_branch == "kstrl/comp-a"
        assert preview.single_pr is False
        assert manifest == snapshot  # the deep-copy did the mutation

    def test_preview_propagates_reset_errors(self) -> None:
        manifest = _failed_manifest()
        with pytest.raises(ValueError):
            preview_retry(manifest, "comp-c")  # completed, not failed
        with pytest.raises(ValueError):
            preview_retry(manifest, "nope")


class TestPrepare:
    def test_prepare_mutates_saves_and_narrates(
        self, tmp_path: Path,
    ) -> None:
        manifest = _failed_manifest()
        manifest_file = tmp_path / "manifest.json"
        stream = io.StringIO()
        ui = PlainUI(no_color=True, file=stream)

        preview = prepare_retry(
            manifest, "comp-a", manifest_file, tmp_path, ui,
        )

        assert preview.reset_dependents == ["comp-b"]
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.PENDING.value
        loaded = Manifest.load(manifest_file)
        reloaded = loaded.get_component("comp-b")
        assert reloaded is not None
        assert reloaded.status == ComponentStatus.PENDING.value
        out = stream.getvalue()
        assert "Retry plan" in out
        assert "comp-a" in out
        assert "comp-b" in out

    def test_single_pr_leaves_branch_and_warns(
        self, tmp_path: Path,
    ) -> None:
        manifest = _failed_manifest()
        manifest.single_pr = True
        manifest_file = tmp_path / "manifest.json"
        stream = io.StringIO()

        prepare_retry(
            manifest, "comp-a", manifest_file, tmp_path,
            PlainUI(no_color=True, file=stream),
        )

        out = stream.getvalue()
        assert "single_pr mode: the shared branch is left in place" in out
