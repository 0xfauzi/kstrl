"""R2.4 `ralph status`: minimal manifest-backed component status view.

Session 7B (R3.2) extends the same skeleton with ProgressLog detail;
these tests pin the minimal contract: per component id, print status,
retries, branch, and timestamps when present.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ralph_py.cli import cli
from ralph_py.manifest import Component, Manifest


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
