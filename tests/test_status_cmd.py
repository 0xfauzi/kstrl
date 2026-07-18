"""`ralph status`: manifest + ProgressLog component status view.

R2.4 pinned the minimal manifest contract (per component id: status,
retries, branch, timestamps). R3.2 joins the ProgressLog onto the same
skeleton: phase, attempt, last-event age, usage totals and evidence
paths for the latest run in the log.
"""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

from ralph_py.cli import cli
from ralph_py.manifest import Component, Manifest
from ralph_py.observability import ProgressLog


def _synthetic_manifest() -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="demo",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id="comp-a",
                title="Component A",
                description="",
                dependencies=[],
                prd_path="scripts/ralph/feature/comp-a/prd.json",
                branch_name="ralph/factory/comp-a",
                status="completed",
                retries=0,
                started_at="2026-07-18T10:00:00",
                completed_at="2026-07-18T10:20:00",
                pr_url="https://github.com/x/y/pull/12",
            ),
            Component(
                id="comp-b",
                title="Component B",
                description="",
                dependencies=["comp-a"],
                prd_path="scripts/ralph/feature/comp-b/prd.json",
                branch_name="ralph/factory/comp-b",
                status="failed",
                retries=2,
                error="tests failed",
            ),
        ],
    )


def _invoke_status(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli, ["status", *args, "--ui", "plain", "--no-color"],
    )
    return result.exit_code, result.output


class TestStatusCommand:
    def test_renders_synthetic_manifest(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "scripts" / "ralph" / "manifest.json"
        _synthetic_manifest().save(manifest_path)

        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 0
        assert "demo" in output
        assert "comp-a: completed" in output
        assert "comp-b: failed" in output
        assert "ralph/factory/comp-a" in output
        assert "ralph/factory/comp-b" in output
        assert "2026-07-18T10:00:00" in output
        assert "2026-07-18T10:20:00" in output
        assert "2" in output  # comp-b retries
        assert "tests failed" in output
        assert "https://github.com/x/y/pull/12" in output

    def test_falls_back_to_run_manifest(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "scripts" / "ralph" / "run-manifest.json"
        _synthetic_manifest().save(manifest_path)

        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 0
        assert "run-manifest.json" in output
        assert "comp-a: completed" in output

    def test_prefers_factory_manifest_over_run_manifest(
        self, tmp_path: Path,
    ) -> None:
        factory_manifest = _synthetic_manifest()
        factory_manifest.project_name = "factory-project"
        factory_manifest.save(tmp_path / "scripts" / "ralph" / "manifest.json")
        run_manifest = _synthetic_manifest()
        run_manifest.project_name = "run-project"
        run_manifest.save(tmp_path / "scripts" / "ralph" / "run-manifest.json")

        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 0
        assert "factory-project" in output
        assert "run-project" not in output

    def test_explicit_manifest_flag(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "custom.json"
        _synthetic_manifest().save(manifest_path)

        exit_code, output = _invoke_status(
            "--root", str(tmp_path), "--manifest", str(manifest_path),
        )

        assert exit_code == 0
        assert "comp-a: completed" in output

    def test_no_manifest_errors_with_hint(self, tmp_path: Path) -> None:
        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 1
        assert "No manifest found" in output
        assert "ralph factory" in output

    def test_corrupt_manifest_fails_cleanly(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "scripts" / "ralph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("{not json")

        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 1
        assert "Failed to load manifest" in output


def _write_progress_log(root: Path, run_id: str = "run-2") -> Path:
    """Synthetic two-run log: the stale run must not leak into output."""
    log_path = root / ".ralph" / "progress.jsonl"
    old = ProgressLog(log_path, run_id="run-1")
    old.factory_started("demo", 2)
    old.component_failed("comp-a", "stale failure from run-1")
    log = ProgressLog(log_path, run_id=run_id)
    log.factory_started("demo", 2)
    log.component_started("comp-a")
    log.component_usage("comp-a", "engineer", {
        "calls": 4, "known_calls": 4, "unreported_calls": 0,
        "total_tokens": 5000, "cost_usd": 1.25,
    })
    log.verification_result("comp-a", passed=True)
    log.review_result("comp-a", passed=True, mode="hard")
    log.component_started("comp-b")
    log.component_retrying("comp-b", attempt=2, reason="tests failed")
    return log_path


class TestStatusProgressLogJoin:
    """R3.2: the log's phase / attempt / age / usage / evidence render."""

    def test_joins_log_detail_onto_components(self, tmp_path: Path) -> None:
        _synthetic_manifest().save(
            tmp_path / "scripts" / "ralph" / "manifest.json",
        )
        _write_progress_log(tmp_path)

        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 0
        assert "run-2" in output
        assert "progress.jsonl" in output
        # comp-a: last phase-bearing event is the hard review.
        assert re.search(r"phase:\s+review", output)
        assert "5000 tokens" in output
        assert "$1.2500" in output
        assert "4 calls" in output
        # comp-b: retrying on attempt 2.
        assert re.search(r"phase:\s+retrying", output)
        assert re.search(r"attempt:\s+2", output)
        # Last-event ages render (events were just written).
        assert "ago" in output

    def test_latest_run_wins(self, tmp_path: Path) -> None:
        _synthetic_manifest().save(
            tmp_path / "scripts" / "ralph" / "manifest.json",
        )
        _write_progress_log(tmp_path)

        _, output = _invoke_status("--root", str(tmp_path))

        assert "run-1" not in output
        assert "stale failure from run-1" not in output

    def test_evidence_paths_listed_when_present(self, tmp_path: Path) -> None:
        _synthetic_manifest().save(
            tmp_path / "scripts" / "ralph" / "manifest.json",
        )
        _write_progress_log(tmp_path)
        worktree = tmp_path / ".ralph" / "worktrees" / "run-2" / "comp-a"
        debug_dir = tmp_path / ".ralph" / "debug" / "run-2" / "comp-b"
        worktree.mkdir(parents=True)
        debug_dir.mkdir(parents=True)

        _, output = _invoke_status("--root", str(tmp_path))

        assert str(worktree) in output
        assert str(debug_dir) in output
        # No evidence line for paths that do not exist.
        assert str(
            tmp_path / ".ralph" / "debug" / "run-2" / "comp-a",
        ) not in output

    def test_run_state_line(self, tmp_path: Path) -> None:
        _synthetic_manifest().save(
            tmp_path / "scripts" / "ralph" / "manifest.json",
        )
        log_path = _write_progress_log(tmp_path)

        _, output = _invoke_status("--root", str(tmp_path))
        assert "in flight" in output

        ProgressLog(log_path, run_id="run-2").factory_completed(
            completed=2, failed=0, skipped=0,
        )
        _, output = _invoke_status("--root", str(tmp_path))
        assert "finished" in output

    def test_explicit_progress_log_flag(self, tmp_path: Path) -> None:
        _synthetic_manifest().save(
            tmp_path / "scripts" / "ralph" / "manifest.json",
        )
        custom = tmp_path / "elsewhere.jsonl"
        ProgressLog(custom, run_id="run-x").component_started("comp-a")

        exit_code, output = _invoke_status(
            "--root", str(tmp_path), "--progress-log", str(custom),
        )

        assert exit_code == 0
        assert "run-x" in output
        assert re.search(r"phase:\s+engineer", output)

    def test_manifest_only_when_no_log(self, tmp_path: Path) -> None:
        """Without a log the R2.4 manifest-only view still renders."""
        _synthetic_manifest().save(
            tmp_path / "scripts" / "ralph" / "manifest.json",
        )

        exit_code, output = _invoke_status("--root", str(tmp_path))

        assert exit_code == 0
        assert "comp-a: completed" in output
        assert "phase:" not in output
        assert "Run state" not in output
