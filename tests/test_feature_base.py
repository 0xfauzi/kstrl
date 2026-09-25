"""The feature base and the pinned Phase 3 commit (#481, slice 1 of #480).

A feature's change runs from the commit before its first merge to the
merged head. ``featureBaseSha`` records the first of these, stamped once
before anything is scheduled, or left empty when a component has already
merged and the start can no longer be known. Phase 3 resolves the base
branch to one commit per round and tests that commit, so a later review
can judge the same tree.

These tests drive the real ``run_factory`` (only the component worker is
stubbed) and the real contract check, against real temporary git
repositories.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import git
from kstrl.contract import (
    ContractConfig,
    ContractMode,
    ContractResult,
    run_contract_testing,
    run_integrated_base_check,
)
from kstrl.factory import ComponentResult, FactoryResult, run_factory
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.ui.plain import PlainUI
from tests.helpers.gitrepo import git_in, set_identity
from tests.test_event_stream import (
    _component,
    _factory_config,
    _make_base_config,
    _make_manifest,
    _setup_project,
    _usage,
)

#: The words every "no feature base" record starts with. Slice 2 turns an
#: empty stamp into "integration not run"; this is what the operator reads
#: until then.
UNKNOWN = "Feature base unknown"


def _rev(repo: Path, rev: str = "main") -> str:
    done = subprocess.run(
        ["git", "rev-parse", rev],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return done.stdout.strip()


def _advance(repo: Path, name: str) -> str:
    """Commit a new file on the checked-out ``main``; returns the new head."""
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git_in(repo, "add", name)
    git_in(repo, "commit", "-q", "-m", f"add {name}")
    return _rev(repo)


def _git_project(root: Path, comp_ids: list[str]) -> str:
    """A project checkout on ``main`` with one commit; returns that commit."""
    root.mkdir(parents=True, exist_ok=True)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    git_in(root, "commit", "-q", "--allow-empty", "-m", "base")
    _setup_project(root, comp_ids)
    return _rev(root)


def _manifest_file(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def _on_disk(root: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_manifest_file(root).read_text(encoding="utf-8"))
    return loaded


def _run(
    root: Path,
    manifest: Manifest,
    *,
    on_component: Callable[[str], None] | None = None,
    **config_overrides: Any,
) -> tuple[FactoryResult, str]:
    """Run the real ``run_factory`` with a stubbed worker.

    Returns the result and everything the run printed.
    """
    out = io.StringIO()

    def fake_component(comp_id: str, *args: Any, **kwargs: Any) -> ComponentResult:
        if on_component is not None:
            on_component(comp_id)
        return ComponentResult(
            comp_id, success=True, iterations=1, duration_seconds=1.0, usage=_usage(500)
        )

    with (
        patch("kstrl.factory._run_component", side_effect=fake_component),
        patch("kstrl.git.get_diff_content", return_value=""),
        # A MERGE_PENDING component would otherwise be re-polled through gh.
        patch.object(ComponentPipeline, "repoll_merge_pending", return_value=None),
    ):
        result = run_factory(
            manifest,
            _factory_config(root, **config_overrides),
            _make_base_config(root),
            PlainUI(no_color=True, file=out),
            root,
            _manifest_file(root),
        )
    return result, out.getvalue()


class TestTheStamp:
    def test_a_fresh_run_stamps_the_base_before_it_schedules_anything(self, tmp_path: Path) -> None:
        before = _git_project(tmp_path, ["comp-a"])
        seen_at_launch: list[object] = []

        def on_component(comp_id: str) -> None:
            # Read off disk while the component runs, then move the base
            # the way a merge would: the stamp must already be written
            # and must not follow the base.
            seen_at_launch.append(_on_disk(tmp_path).get("featureBaseSha"))
            _advance(tmp_path, f"{comp_id}.txt")

        _run(tmp_path, _make_manifest([_component("comp-a")]), on_component=on_component)

        assert seen_at_launch == [before]
        assert _rev(tmp_path) != before, "the base did not move during the run"
        assert _on_disk(tmp_path)["featureBaseSha"] == before
        assert Manifest.load(_manifest_file(tmp_path)).feature_base_sha == before

    def test_a_resume_keeps_an_existing_stamp_byte_identical(self, tmp_path: Path) -> None:
        first = _git_project(tmp_path, ["comp-a"])
        head = _advance(tmp_path, "later.txt")
        _make_manifest([_component("comp-a")]).save(_manifest_file(tmp_path))
        raw = _on_disk(tmp_path)
        raw["featureBaseSha"] = first
        _manifest_file(tmp_path).write_text(json.dumps(raw), encoding="utf-8")

        _run(tmp_path, Manifest.load(_manifest_file(tmp_path)))

        assert head != first
        assert _on_disk(tmp_path)["featureBaseSha"] == first

    @pytest.mark.parametrize(
        ("status", "merge_sha", "pr_url"),
        [
            (ComponentStatus.COMPLETED.value, "f" * 40, "https://github.com/o/r/pull/7"),
            # mergeSha may be empty for a merged PR (R8.7 slice 1).
            (ComponentStatus.COMPLETED.value, "", "https://github.com/o/r/pull/7"),
            (ComponentStatus.MERGE_PENDING.value, "", "https://github.com/o/r/pull/7"),
        ],
        ids=["completed-with-merge-sha", "completed-with-pr-only", "merge-pending"],
    )
    def test_a_manifest_that_has_merged_stays_unstamped_and_says_so(
        self, tmp_path: Path, status: str, merge_sha: str, pr_url: str
    ) -> None:
        _git_project(tmp_path, ["comp-a", "comp-b"])
        merged = _component("comp-a")
        merged.status = status
        merged.merge_sha = merge_sha
        merged.pr_url = pr_url
        manifest = _make_manifest([merged, _component("comp-b", ["comp-a"])])
        manifest.save(_manifest_file(tmp_path))

        _result, output = _run(tmp_path, manifest)

        assert _on_disk(tmp_path)["featureBaseSha"] == ""
        assert UNKNOWN in output

    @pytest.mark.parametrize(
        ("status", "pr_url"),
        [
            # A create_prs=False run completed it: no PR, no merge commit.
            (ComponentStatus.COMPLETED.value, ""),
            # Its PR was opened and the merge was refused: a PR alone is
            # not a merge.
            (ComponentStatus.FAILED.value, "https://github.com/o/r/pull/7"),
        ],
        ids=["completed-without-pr", "failed-with-unmerged-pr"],
    )
    def test_a_component_that_never_merged_does_not_block_the_stamp(
        self, tmp_path: Path, status: str, pr_url: str
    ) -> None:
        """The clean twin of the test above. Nothing reached the base, so
        the stamp is still the feature's start."""
        before = _git_project(tmp_path, ["comp-a", "comp-b"])
        built: Component = _component("comp-a")
        built.status = status
        built.pr_url = pr_url
        manifest = _make_manifest([built, _component("comp-b", ["comp-a"])])
        manifest.save(_manifest_file(tmp_path))

        _result, output = _run(tmp_path, manifest)

        assert _on_disk(tmp_path)["featureBaseSha"] == before
        assert UNKNOWN not in output

    def test_a_base_that_does_not_resolve_leaves_the_stamp_empty_and_says_why(
        self, tmp_path: Path
    ) -> None:
        _setup_project(tmp_path, ["comp-a"])  # not a git repository

        result, output = _run(tmp_path, _make_manifest([_component("comp-a")]))

        assert result.exit_code == 0
        assert _on_disk(tmp_path)["featureBaseSha"] == ""
        assert UNKNOWN in output
        assert "does not resolve to a commit" in output

    def test_a_refused_run_stamps_nothing(self, tmp_path: Path) -> None:
        """A run a pre-spend check refuses has not started the feature, and
        in worktree mode the base fetch runs inside those checks, so the
        stamp comes after them."""
        _git_project(tmp_path, ["comp-a"])
        # An unreadable decision register is a pre-spend refusal (#260).
        (tmp_path / "scripts" / "kstrl" / "decisions.json").write_text(
            "{not json", encoding="utf-8"
        )

        result, output = _run(tmp_path, _make_manifest([_component("comp-a")]))

        assert result.exit_code == 2
        assert "Refusing to run" in output
        assert _on_disk(tmp_path).get("featureBaseSha", "") == ""


class TestTheManifestField:
    def test_the_field_round_trips_through_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        manifest = _make_manifest([_component("comp-a")])
        manifest.feature_base_sha = "ab" * 20
        manifest.save(path)

        assert json.loads(path.read_text(encoding="utf-8"))["featureBaseSha"] == "ab" * 20
        assert Manifest.load(path).feature_base_sha == "ab" * 20

    def test_an_older_manifest_without_the_key_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        _make_manifest([_component("comp-a")]).save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("featureBaseSha", None)
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert Manifest.load(path).feature_base_sha == ""

    def test_a_stamp_that_is_not_a_string_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        _make_manifest([_component("comp-a")]).save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["featureBaseSha"] = 123

        assert "featureBaseSha must be a string" in Manifest.validate_schema(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="featureBaseSha must be a string"):
            Manifest.load(path)


def _contract_manifest() -> Manifest:
    return Manifest(
        version="1",
        spec_file="",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=[],
    )


#: Passes only on a tree that does not carry the file the base moves by.
_NOT_MOVED = ContractConfig(
    mode=ContractMode.TIER.value, test_command="test ! -f moved.txt", timeout=60.0
)


class TestThePinnedCommit:
    def test_the_integrated_check_tests_the_commit_it_was_given(self, tmp_path: Path) -> None:
        pinned = _git_project(tmp_path, [])
        moved = _advance(tmp_path, "moved.txt")
        ui = PlainUI(no_color=True, file=io.StringIO())

        result = run_integrated_base_check(
            _contract_manifest(), ["a"], tmp_path, _NOT_MOVED, ui, pinned
        )
        # The control: the same check at the moved head sees the new file,
        # so the pass above is about the tree, not a command that cannot fail.
        control = run_integrated_base_check(
            _contract_manifest(), ["a"], tmp_path, _NOT_MOVED, ui, moved
        )

        assert result.passed is True, result.test_output
        assert result.tested_sha == pinned
        assert control.passed is False
        assert control.tested_sha == moved

    def test_an_unpinned_base_tests_nothing(self, tmp_path: Path) -> None:
        _git_project(tmp_path, [])
        ui = PlainUI(no_color=True, file=io.StringIO())

        result = run_integrated_base_check(
            _contract_manifest(), ["a"], tmp_path, _NOT_MOVED, ui, ""
        )

        assert result.passed is False
        assert "nothing was tested" in result.test_output
        assert result.tested_sha == ""

    def test_each_phase3_round_pins_the_base_it_resolved(self, tmp_path: Path) -> None:
        """Round 1 fails with a breaker, the base moves, round 2 passes.
        Each round hands Phase 3 the commit the base named when that round
        began, and the feature base stays where the run started."""
        start = _git_project(tmp_path, ["comp-a"])
        pinned: list[str] = []
        heads: list[str] = []

        def fake_contract(
            manifest: Manifest,
            root: Path,
            cfg: ContractConfig,
            ui: object,
            components_merged: bool = False,
            base_sha: str = "",
        ) -> list[ContractResult]:
            pinned.append(base_sha)
            if len(pinned) == 1:
                heads.append(_advance(tmp_path, "round1.txt"))
                return [ContractResult(False, 0, ["comp-a"], breaker="comp-a", test_output="x")]
            return [ContractResult(True, 0, ["comp-a"])]

        with patch("kstrl.factory.run_contract_testing", side_effect=fake_contract):
            result, _output = _run(
                tmp_path,
                _make_manifest([_component("comp-a")]),
                max_retries=1,
                contract_config=ContractConfig(mode=ContractMode.TIER.value),
            )

        assert result.contract_failures == []
        assert pinned == [start, heads[0]]
        assert _on_disk(tmp_path)["featureBaseSha"] == start

    def test_the_contract_entry_point_tests_the_commit_it_is_handed(self, tmp_path: Path) -> None:
        """Through ``run_contract_testing``, the entry point the factory
        calls: a given commit is tested as given, and no commit means
        nothing is tested, never the moving branch name."""
        pinned = _git_project(tmp_path, [])
        _advance(tmp_path, "moved.txt")
        done = _component("comp-a")
        done.status = ComponentStatus.COMPLETED.value
        manifest = _make_manifest([done])
        ui = PlainUI(no_color=True, file=io.StringIO())

        [given] = run_contract_testing(
            manifest, tmp_path, _NOT_MOVED, ui, components_merged=True, base_sha=pinned
        )
        [omitted] = run_contract_testing(manifest, tmp_path, _NOT_MOVED, ui, components_merged=True)

        assert given.passed is True, given.test_output
        assert given.tested_sha == pinned
        assert omitted.passed is False
        assert "nothing was tested" in omitted.test_output
        assert omitted.tested_sha == ""

    def test_a_round_whose_base_does_not_resolve_pins_nothing(self, tmp_path: Path) -> None:
        _setup_project(tmp_path, ["comp-a"])  # not a git repository
        pinned: list[str] = []

        def fake_contract(
            manifest: Manifest,
            root: Path,
            cfg: ContractConfig,
            ui: object,
            components_merged: bool = False,
            base_sha: str = "",
        ) -> list[ContractResult]:
            pinned.append(base_sha)
            return [ContractResult(True, 0, ["comp-a"])]

        with patch("kstrl.factory.run_contract_testing", side_effect=fake_contract):
            _result, output = _run(
                tmp_path,
                _make_manifest([_component("comp-a")]),
                contract_config=ContractConfig(mode=ContractMode.TIER.value),
            )

        assert pinned == [""]
        assert "Phase 3 base unresolved" in output


class TestBaseMoved:
    def test_it_reports_whether_the_base_left_the_pinned_commit(self, tmp_path: Path) -> None:
        pinned = _git_project(tmp_path, [])
        assert git.base_moved("main", pinned, tmp_path) == (False, pinned)

        head = _advance(tmp_path, "later.txt")

        assert git.base_moved("main", pinned, tmp_path) == (True, head)

    def test_a_base_that_does_not_resolve_raises(self, tmp_path: Path) -> None:
        pinned = _git_project(tmp_path, [])
        with pytest.raises(git.GitDiffError):
            git.base_moved("no-such-branch", pinned, tmp_path)
