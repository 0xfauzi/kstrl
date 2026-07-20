"""R3.3 - resume + partial-failure ergonomics.

Covers: run_id/completed_at persistence in the manifest, per-attempt
findings hygiene (cleared per attempt, superseded findings retained in
the evolution journal tagged ``attempt:<n>``), keep-worktrees-on-failure
evidence, the failure summary, and the ``ralph retry`` command.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.config import KstrlConfig
from kstrl.evolution import EvolutionConfig
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    run_factory,
)
from kstrl.findings import (
    Finding,
    finding_attempt,
    tag_finding_with_attempt,
)
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.review import ReviewConcern, ReviewResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

# ---------------------------------------------------------------------------
# Shared builders (pattern follows tests/test_review_gates.py)
# ---------------------------------------------------------------------------


def _write_prd(path: Path, story_ids: list[str]) -> None:
    path.write_text(json.dumps({
        "branchName": "test",
        "userStories": [
            {
                "id": sid, "title": f"Story {sid}",
                "acceptanceCriteria": ["AC1"], "priority": 1,
                "passes": True, "notes": "",
            }
            for sid in story_ids
        ],
    }))


def _scaffold(tmp_path: Path, comp_ids: list[str]) -> Path:
    (tmp_path / "scripts" / "ralph").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "ralph" / "prompt.md").write_text("p")
    (tmp_path / "scripts" / "ralph" / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for comp_id in comp_ids:
        feature_dir = tmp_path / "scripts" / "ralph" / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        _write_prd(feature_dir / "prd.json", ["US-001"])
    return tmp_path


def _make_manifest(ids: list[str]) -> Manifest:
    return Manifest(
        version="1", spec_file="s", project_name="t",
        base_branch="main", single_pr=False,
        components=[
            Component(
                id=i, title=i, description="", dependencies=[],
                prd_path=f"scripts/ralph/feature/{i}/prd.json",
                branch_name=f"ralph/{i}",
            )
            for i in ids
        ],
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts/ralph/prompt.md",
        prd_file=root / "scripts/ralph/prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _factory_config(**overrides: object) -> FactoryConfig:
    defaults: dict[str, object] = dict(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)  # type: ignore[arg-type]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed: {result.stderr}"
    )
    return result


def _init_git_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "r33@test")
    _git(root, "config", "user.name", "R33 Test")
    (root / ".gitignore").write_text("scripts/ralph/\n.ralph/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", ".gitignore", "README.md")
    _git(root, "commit", "-q", "-m", "init")


def _journal_events(root: Path) -> list[dict[str, object]]:
    journal = EvolutionConfig.load(root).journal_path
    if not journal.exists():
        return []
    return [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Attempt-tag helpers
# ---------------------------------------------------------------------------


class TestAttemptTagging:
    def test_tag_and_parse_round_trip(self) -> None:
        f = Finding.phase_skipped("review", "skipped")
        tagged = tag_finding_with_attempt(f, 3)
        assert "attempt:3" in tagged.tags
        assert finding_attempt(tagged) == 3

    def test_tagging_is_idempotent(self) -> None:
        f = tag_finding_with_attempt(
            Finding.phase_skipped("review", "skipped"), 1,
        )
        retagged = tag_finding_with_attempt(f, 2)
        assert retagged.tags == f.tags
        assert finding_attempt(retagged) == 1

    def test_untagged_finding_has_no_attempt(self) -> None:
        assert finding_attempt(Finding.phase_skipped("x", "y")) is None


# ---------------------------------------------------------------------------
# Manifest: new fields + reset_for_retry
# ---------------------------------------------------------------------------


class TestManifestRunMetadata:
    def test_run_id_and_completed_at_round_trip(self, tmp_path: Path) -> None:
        manifest = _make_manifest(["comp-a"])
        manifest.run_id = "20260718-abc"
        manifest.completed_at = "2026-07-18T00:00:00Z"
        comp = manifest.components[0]
        comp.failed_phase = "review"
        comp.failed_check = "criteria"
        comp.evidence_worktree = "/tmp/wt"
        comp.evidence_debug_dir = "/tmp/dbg"
        comp.journal_offset_start = 10
        comp.journal_offset_end = 250

        path = tmp_path / "m.json"
        manifest.save(path)
        loaded = Manifest.load(path)

        assert loaded.run_id == "20260718-abc"
        assert loaded.completed_at == "2026-07-18T00:00:00Z"
        loaded_comp = loaded.components[0]
        assert loaded_comp.failed_phase == "review"
        assert loaded_comp.failed_check == "criteria"
        assert loaded_comp.evidence_worktree == "/tmp/wt"
        assert loaded_comp.evidence_debug_dir == "/tmp/dbg"
        assert loaded_comp.journal_offset_start == 10
        assert loaded_comp.journal_offset_end == 250

    def test_pre_r33_manifest_loads_with_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps({
            "version": "1", "specFile": "s", "projectName": "t",
            "baseBranch": "main", "singlePr": False,
            "components": [{
                "id": "comp-a", "title": "a", "description": "",
                "dependencies": [], "prdPath": "p.json",
                "branchName": "ralph/comp-a",
            }],
        }))
        loaded = Manifest.load(path)
        assert loaded.run_id == ""
        assert loaded.completed_at == ""
        comp = loaded.components[0]
        assert comp.evidence_worktree == ""
        assert comp.journal_offset_start == -1

    def test_cascade_skip_sets_completed_at(self) -> None:
        manifest = _make_manifest(["comp-a", "comp-b"])
        manifest.components[1].dependencies = ["comp-a"]
        manifest.components[0].status = ComponentStatus.FAILED.value
        manifest.cascade_skip("comp-a")
        skipped = manifest.get_component("comp-b")
        assert skipped is not None
        assert skipped.status == ComponentStatus.SKIPPED.value
        assert skipped.completed_at != ""


class TestResetForRetry:
    def _failed_manifest(self) -> Manifest:
        manifest = _make_manifest(["comp-a", "comp-b", "comp-c"])
        a, b, c = manifest.components
        b.dependencies = ["comp-a"]
        a.status = ComponentStatus.FAILED.value
        a.error = "boom"
        a.retries = 3
        a.completed_at = "2026-07-18T00:00:00Z"
        a.failed_phase = "review"
        a.failed_check = "criteria"
        a.findings = [Finding.phase_skipped("security", "skipped")]
        a.review_findings = "text"
        b.status = ComponentStatus.SKIPPED.value
        b.error = "Dependency 'comp-a' failed"
        c.status = ComponentStatus.COMPLETED.value
        return manifest

    def test_resets_failed_component_and_cascade(self) -> None:
        manifest = self._failed_manifest()
        reset = manifest.reset_for_retry("comp-a")
        assert reset == ["comp-b"]
        a = manifest.get_component("comp-a")
        b = manifest.get_component("comp-b")
        c = manifest.get_component("comp-c")
        assert a is not None and b is not None and c is not None
        for comp in (a, b):
            assert comp.status == ComponentStatus.PENDING.value
            assert comp.error == ""
            assert comp.retries == 0
            assert comp.completed_at == ""
            assert comp.findings == []
            assert comp.failed_phase == ""
            assert comp.journal_offset_start == -1
        assert c.status == ComponentStatus.COMPLETED.value

    def test_rejects_non_failed_component(self) -> None:
        manifest = self._failed_manifest()
        with pytest.raises(ValueError, match="not 'failed'"):
            manifest.reset_for_retry("comp-c")
        with pytest.raises(ValueError, match="Unknown component"):
            manifest.reset_for_retry("nope")

    def test_dependent_skipped_by_other_failure_stays_skipped(self) -> None:
        manifest = self._failed_manifest()
        manifest.components.append(Component(
            id="comp-e", title="e", description="", dependencies=[],
            prd_path="p.json", branch_name="ralph/comp-e",
            status=ComponentStatus.FAILED.value, error="other failure",
        ))
        manifest.components.append(Component(
            id="comp-f", title="f", description="",
            dependencies=["comp-a", "comp-e"],
            prd_path="p.json", branch_name="ralph/comp-f",
            status=ComponentStatus.SKIPPED.value,
            error="Dependency 'comp-e' failed",
        ))
        reset = manifest.reset_for_retry("comp-a")
        assert reset == ["comp-b"]
        f = manifest.get_component("comp-f")
        assert f is not None
        assert f.status == ComponentStatus.SKIPPED.value


# ---------------------------------------------------------------------------
# Factory: completed_at + run_id persistence
# ---------------------------------------------------------------------------


class TestRunMetadataPersistence:
    def test_completed_at_and_run_id_set_on_success(
        self, tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config()
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert result.exit_code == 0
        assert manifest.run_id != ""
        assert manifest.completed_at != ""
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.completed_at != ""
        saved = json.loads(
            (root / "scripts" / "ralph" / "manifest.json").read_text()
        )
        assert saved["runId"] == manifest.run_id
        assert saved["completedAt"] == manifest.completed_at

    def test_completed_at_set_on_failure(self, tmp_path: Path) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config()
        failure = ComponentResult(
            "comp-a", success=False, iterations=1, error="agent died",
        )
        with patch(
            "kstrl.factory._run_component", return_value=failure,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert result.exit_code == 1
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.completed_at != ""
        assert comp.failed_phase == "engineer"
        assert comp.failed_check == "loop"
        assert manifest.completed_at != ""


# ---------------------------------------------------------------------------
# Factory: per-attempt findings + superseded journal entries
# ---------------------------------------------------------------------------


class TestPerAttemptFindings:
    def test_attempt1_findings_cleared_but_journaled(
        self, tmp_path: Path,
    ) -> None:
        """Attempt 1 fails hard review with a concern; attempt 2 passes.
        The final component findings carry only attempt:2 tags; the
        evolution journal keeps the attempt-1 findings in a
        findings_superseded event tagged attempt:1."""
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            review_mode="hard", max_retries=1,
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        failing_review = ReviewResult(
            passed=False, mode="hard",
            concerns=[ReviewConcern(
                category="test_quality", severity="fail",
                location="x.py:1", explanation="tautological test",
            )],
        )
        passing_review = ReviewResult(passed=True, mode="hard")
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.factory.run_review",
            side_effect=[failing_review, passing_review],
        ), patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )

        assert result.completed == ["comp-a"]
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.retries == 1

        # Final stream: attempt-2 findings only, no attempt-1 leftovers.
        assert comp.findings, "attempt 2 should record phase-skip findings"
        attempts = {finding_attempt(f) for f in comp.findings}
        assert attempts == {2}
        assert not any(
            f.category == "test_quality" for f in comp.findings
        ), "the superseded review concern must not survive into attempt 2"

        # Journal: superseded findings retained, tagged attempt:1.
        events = _journal_events(root)
        superseded = [
            e for e in events if e["event_type"] == "findings_superseded"
        ]
        assert len(superseded) == 1
        assert superseded[0]["component_id"] == "comp-a"
        assert superseded[0]["attempt"] == 1
        journaled = [
            Finding.from_dict(d)
            for d in superseded[0]["findings"]  # type: ignore[union-attr]
        ]
        assert any(f.category == "test_quality" for f in journaled)
        assert all(finding_attempt(f) == 1 for f in journaled)

        # record_run's component_result carries only the final stream.
        component_results = [
            e for e in events
            if e["event_type"] == "component_result"
            and e["component_id"] == "comp-a"
        ]
        assert component_results, "record_run must journal the component"
        final_findings = [
            Finding.from_dict(d)
            for d in component_results[-1]["findings"]  # type: ignore[index]
        ]
        assert all(finding_attempt(f) == 2 for f in final_findings)
        assert not any(
            f.category == "test_quality" for f in final_findings
        )


# ---------------------------------------------------------------------------
# Factory: keep-worktrees-on-failure + failure summary (real git)
# ---------------------------------------------------------------------------


class TestKeepWorktreesOnFailure:
    def _run_failing_factory(
        self, root: Path, keep: bool,
    ) -> tuple[Manifest, FactoryResult]:
        _init_git_repo(root)
        _scaffold(root, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            use_worktrees=True,
            keep_worktrees_on_failure=keep,
            progress_log_path=root / ".ralph" / "progress.jsonl",
        )
        failure = ComponentResult(
            "comp-a", success=False, iterations=1, error="agent died",
        )
        with patch(
            "kstrl.factory._run_component", return_value=failure,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        return manifest, result

    def test_failed_worktree_kept_and_summary_points_at_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest, result = self._run_failing_factory(tmp_path, keep=True)
        assert result.exit_code == 1
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.evidence_worktree != ""
        kept = Path(comp.evidence_worktree)
        assert kept.exists(), "failed worktree must survive cleanup"
        assert kept == tmp_path / ".ralph" / "worktrees" / manifest.run_id / "comp-a"

        # Evidence pointers persisted to disk.
        saved = json.loads(
            (tmp_path / "scripts" / "ralph" / "manifest.json").read_text()
        )
        saved_comp = saved["components"][0]
        assert saved_comp["evidenceWorktree"] == comp.evidence_worktree
        assert saved_comp["failedPhase"] == "engineer"

        # Journal offsets bracket the attempt in the progress log.
        assert comp.journal_offset_start >= 0
        assert comp.journal_offset_end >= comp.journal_offset_start

        # The failure summary names phase, check, and evidence.
        # PlainUI writes to stderr.
        out = capsys.readouterr().err
        assert "Failure summary" in out
        assert "phase=engineer" in out
        assert "check=loop" in out
        assert comp.evidence_worktree in out
        assert "ralph retry comp-a" in out

    def test_failed_worktree_removed_without_flag(
        self, tmp_path: Path,
    ) -> None:
        manifest, result = self._run_failing_factory(tmp_path, keep=False)
        assert result.exit_code == 1
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.evidence_worktree == ""
        run_dir = tmp_path / ".ralph" / "worktrees" / manifest.run_id
        assert not (run_dir / "comp-a").exists()

    def test_next_run_prune_preserves_kept_evidence(
        self, tmp_path: Path,
    ) -> None:
        manifest, _ = self._run_failing_factory(tmp_path, keep=True)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        kept = Path(comp.evidence_worktree)
        assert kept.exists()

        # A second run on the same manifest (comp-a still FAILED) must
        # not prune the recorded evidence worktree.
        config = _factory_config(use_worktrees=True)
        with patch(
            "kstrl.factory._run_component",
            side_effect=AssertionError("nothing should be scheduled"),
        ):
            run_factory(
                manifest, config, _base_config(tmp_path),
                PlainUI(no_color=True), tmp_path,
            )
        assert kept.exists(), "prune must preserve evidence worktrees"


# ---------------------------------------------------------------------------
# ralph retry CLI
# ---------------------------------------------------------------------------


class TestRetryCli:
    def _failed_repo(self, root: Path) -> Path:
        """Real git repo with a failed comp-a (branch + kept worktree)
        and a cascade-skipped comp-b, persisted to manifest.json."""
        _init_git_repo(root)
        _scaffold(root, ["comp-a", "comp-b"])
        # Branch from the failed attempt, with a commit not on main.
        _git(root, "checkout", "-q", "-b", "ralph/comp-a")
        (root / "half-done.txt").write_text("partial\n")
        _git(root, "add", "half-done.txt")
        _git(root, "commit", "-q", "-m", "failed attempt")
        _git(root, "checkout", "-q", "main")
        # Kept evidence worktree on that branch.
        wt = root / ".ralph" / "worktrees" / "run-old" / "comp-a"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(root, "worktree", "add", str(wt), "ralph/comp-a")

        manifest = _make_manifest(["comp-a", "comp-b"])
        a, b = manifest.components
        b.dependencies = ["comp-a"]
        a.status = ComponentStatus.FAILED.value
        a.error = "review failed"
        a.retries = 3
        a.failed_phase = "review"
        a.failed_check = "criteria"
        a.evidence_worktree = str(wt)
        b.status = ComponentStatus.SKIPPED.value
        b.error = "Dependency 'comp-a' failed"
        manifest_file = root / "scripts" / "ralph" / "manifest.json"
        manifest.save(manifest_file)
        return manifest_file

    def test_retry_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manifest_file = self._failed_repo(tmp_path)
        captured: dict[str, object] = {}

        def fake_run_factory(
            manifest: Manifest,
            factory_config: FactoryConfig,
            base_config: KstrlConfig,
            ui_impl: object,
            root_dir: Path,
            manifest_path: Path | None = None,
            **kwargs: object,
        ) -> FactoryResult:
            captured["manifest"] = manifest
            captured["manifest_path"] = manifest_path
            captured["config"] = factory_config
            return FactoryResult(exit_code=0)

        monkeypatch.setattr("kstrl.cli.run_factory", fake_run_factory)
        monkeypatch.setattr(
            "kstrl.cli._check_agent_preflight", lambda *a, **k: None,
        )
        runner = CliRunner()
        result = runner.invoke(cli, [
            "retry", "comp-a", "--root", str(tmp_path), "--yes",
        ])
        assert result.exit_code == 0, result.output

        # The factory re-entered with the SAME manifest file, components
        # reset to PENDING.
        assert captured["manifest_path"] == manifest_file
        entered = captured["manifest"]
        assert isinstance(entered, Manifest)
        for cid in ("comp-a", "comp-b"):
            comp = entered.get_component(cid)
            assert comp is not None
            assert comp.status == ComponentStatus.PENDING.value
            assert comp.retries == 0
            assert comp.error == ""

        # Saved manifest matches the reset state.
        saved = Manifest.load(manifest_file)
        saved_a = saved.get_component("comp-a")
        assert saved_a is not None
        assert saved_a.status == ComponentStatus.PENDING.value
        assert saved_a.evidence_worktree == ""

        # Failed-attempt branch and evidence worktree are gone, so the
        # stale-branch preflight cannot refuse the re-run.
        branch = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet",
             "refs/heads/ralph/comp-a"],
            cwd=tmp_path, capture_output=True, timeout=30,
        )
        assert branch.returncode != 0, "failed-attempt branch must be deleted"
        assert not (
            tmp_path / ".ralph" / "worktrees" / "run-old" / "comp-a"
        ).exists()

    def test_retry_rejects_non_failed_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manifest_file = self._failed_repo(tmp_path)
        manifest = Manifest.load(manifest_file)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.status = ComponentStatus.COMPLETED.value
        manifest.save(manifest_file)

        monkeypatch.setattr(
            "kstrl.cli._check_agent_preflight", lambda *a, **k: None,
        )
        runner = CliRunner()
        result = runner.invoke(cli, [
            "retry", "comp-a", "--root", str(tmp_path), "--yes",
        ])
        assert result.exit_code == 2
        assert "not 'failed'" in result.output

    def test_retry_unknown_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._failed_repo(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, [
            "retry", "ghost", "--root", str(tmp_path), "--yes",
        ])
        assert result.exit_code == 2
        assert "Unknown component" in result.output
