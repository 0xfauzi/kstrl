"""TUI surface B1: non-mutating retry preview + prepare extraction."""

from __future__ import annotations

import copy
import io
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.retry_plan import prepare_retry, preview_retry
from kstrl.ui.plain import PlainUI
from tests.helpers.run_limits import every_limit_argv
from tests.test_retry_carries_flags import _RecordingChannel


def _failed_manifest() -> Manifest:
    manifest = Manifest(
        version="1",
        spec_file="s",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=i,
                title=i,
                description="",
                dependencies=[],
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
        self,
        tmp_path: Path,
    ) -> None:
        manifest = _failed_manifest()
        manifest_file = tmp_path / "manifest.json"
        stream = io.StringIO()
        ui = PlainUI(no_color=True, file=stream)

        preview = prepare_retry(
            manifest,
            "comp-a",
            manifest_file,
            tmp_path,
            ui,
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
        self,
        tmp_path: Path,
    ) -> None:
        manifest = _failed_manifest()
        manifest.single_pr = True
        manifest_file = tmp_path / "manifest.json"
        stream = io.StringIO()

        prepare_retry(
            manifest,
            "comp-a",
            manifest_file,
            tmp_path,
            PlainUI(no_color=True, file=stream),
        )

        out = stream.getvalue()
        assert "single_pr mode: the shared branch is left in place" in out


def _component(cid: str, deps: list[str], status: ComponentStatus) -> Component:
    return Component(
        id=cid,
        title=cid,
        description="",
        dependencies=deps,
        prd_path=f"scripts/kstrl/feature/{cid}/prd.json",
        branch_name=f"kstrl/{cid}",
        status=status.value,
    )


def _manifest_of(*components: Component) -> Manifest:
    return Manifest(
        version="1",
        spec_file="s",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=list(components),
    )


def _two_failed_siblings() -> Manifest:
    """#485 as measured: http-api and cli both FAILED on a COMPLETED storage."""
    return _manifest_of(
        _component("link-rules", [], ComponentStatus.COMPLETED),
        _component("storage", [], ComponentStatus.COMPLETED),
        _component("http-api", ["storage"], ComponentStatus.FAILED),
        _component("cli", ["storage"], ComponentStatus.FAILED),
    )


def _skipped_on_two_failures() -> Manifest:
    """d waits on both a and b, so retrying a alone cannot reset it."""
    return _manifest_of(
        _component("a", [], ComponentStatus.FAILED),
        _component("b", [], ComponentStatus.FAILED),
        _component("d", ["a", "b"], ComponentStatus.SKIPPED),
    )


def _skipped_on_one_failure() -> Manifest:
    return _manifest_of(
        _component("a", [], ComponentStatus.FAILED),
        _component("b", ["a"], ComponentStatus.SKIPPED),
    )


class TestNotInThisRetry:
    """#485: the plan names every FAILED or SKIPPED component the retry leaves out."""

    def test_names_the_other_failed_component_and_its_command(self) -> None:
        preview = preview_retry(_two_failed_siblings(), "http-api")

        assert preview.not_in_retry == ["cli: FAILED; retry it after this run with ks retry cli"]

    def test_a_skipped_component_that_stays_skipped_names_what_it_waits_on(self) -> None:
        preview = preview_retry(_skipped_on_two_failures(), "a")

        assert preview.reset_dependents == []
        assert preview.not_in_retry == [
            "b: FAILED; retry it after this run with ks retry b",
            "d: SKIPPED; waits on b",
        ]

    def test_a_skipped_component_names_a_failure_it_reaches_through_another(self) -> None:
        manifest = _manifest_of(
            _component("a", [], ComponentStatus.FAILED),
            _component("b", [], ComponentStatus.FAILED),
            _component("c", ["b"], ComponentStatus.SKIPPED),
            _component("e", ["c"], ComponentStatus.SKIPPED),
        )

        preview = preview_retry(manifest, "a")

        assert preview.not_in_retry == [
            "b: FAILED; retry it after this run with ks retry b",
            "c: SKIPPED; waits on b",
            "e: SKIPPED; waits on b",
        ]

    def test_a_skipped_component_names_every_failure_it_waits_on_in_manifest_order(
        self,
    ) -> None:
        manifest = _manifest_of(
            _component("a", [], ComponentStatus.FAILED),
            _component("b", [], ComponentStatus.FAILED),
            _component("c", [], ComponentStatus.FAILED),
            _component("d", ["c", "b"], ComponentStatus.SKIPPED),
        )

        preview = preview_retry(manifest, "a")

        assert preview.not_in_retry == [
            "b: FAILED; retry it after this run with ks retry b",
            "c: FAILED; retry it after this run with ks retry c",
            "d: SKIPPED; waits on b, c",
        ]

    def test_a_dependent_this_retry_resets_is_not_listed(self) -> None:
        preview = preview_retry(_skipped_on_one_failure(), "a")

        assert preview.reset_dependents == ["b"]
        assert preview.not_in_retry == []

    def test_a_skipped_component_with_no_failed_dependency_says_so(self) -> None:
        manifest = _manifest_of(
            _component("a", [], ComponentStatus.FAILED),
            _component("e", [], ComponentStatus.SKIPPED),
        )

        preview = preview_retry(manifest, "a")

        assert preview.not_in_retry == ["e: SKIPPED; waits on no FAILED component"]

    def test_preview_and_prepare_return_the_same_list(self, tmp_path: Path) -> None:
        manifest = _skipped_on_two_failures()
        preview = preview_retry(manifest, "a")

        prepared = prepare_retry(
            manifest,
            "a",
            tmp_path / "manifest.json",
            tmp_path,
            PlainUI(no_color=True, file=io.StringIO()),
        )

        assert preview.not_in_retry != []
        assert prepared.not_in_retry == preview.not_in_retry
        assert prepared == preview


class TestTheRetryCommandPrintsWhatItLeavesOut:
    """#485 through the real `ks retry`, answered Quit at the confirmation."""

    def _retry(
        self, tmp_path: Path, manifest: Manifest, component_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> str:
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest_file.parent.mkdir(parents=True)
        manifest.save(manifest_file)
        monkeypatch.setattr(cli_mod, "UiInteractionChannel", _RecordingChannel)
        for name in [k for k in os.environ if k.startswith("KSTRL_")]:
            monkeypatch.delenv(name)
        result = CliRunner().invoke(
            cli_mod.cli,
            [
                "retry",
                component_id,
                "--root",
                str(tmp_path),
                # No launch record: state every run limit, or the retry refuses (#526).
                *every_limit_argv(),
                "--ui",
                "plain",
                "--no-color",
            ],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_the_plan_names_the_failed_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = self._retry(tmp_path, _two_failed_siblings(), "http-api", monkeypatch)

        assert re.search(
            r"Cascade-skipped dependents reset:\s*\(none\)\n"
            r"\s*Not in this retry:\s*cli: FAILED; retry it after this run with ks retry cli\n"
            r"\s*Manifest:",
            out,
        ), out
        assert "ks retry http-api" not in out, out

    def test_the_plan_says_none_when_nothing_is_left_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = self._retry(tmp_path, _skipped_on_one_failure(), "a", monkeypatch)

        assert re.search(
            r"Cascade-skipped dependents reset:\s*b\n"
            r"\s*Not in this retry:\s*\(none\)\n"
            r"\s*Manifest:",
            out,
        ), out
