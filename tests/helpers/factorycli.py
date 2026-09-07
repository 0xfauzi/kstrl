"""The `ks factory` CLI path with ``run_factory`` replaced by a capture.

The assertion target is the exact ``FactoryConfig`` the orchestrator
would receive, built by the real click command, rather than a loader
called in isolation. That is what makes it a test of PRECEDENCE (flag >
env > toml > default) instead of a test of one reader.

Extracted from ``tests/test_config_control_plane.py`` on #195, when a
second file needed the same three pieces to prove that passing
``--pause-before-pr-merge`` records the flag as an EXPLICIT operator
request. Copying them would have left two harnesses to keep in step, and
that file is 40 lines under the 800-line ratchet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
from kstrl.factory import FactoryConfig, FactoryResult
from kstrl.manifest import Component, Manifest


def capture_run_factory(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``run_factory`` with a capturing fake; return the box."""
    box: dict[str, Any] = {}

    def fake_run_factory(
        manifest: Manifest,
        factory_config: FactoryConfig,
        base_config: Any,
        ui: Any,
        root_dir: Path,
        manifest_path: Path | None = None,
        **kwargs: Any,
    ) -> FactoryResult:
        box["manifest"] = manifest
        box["factory_config"] = factory_config
        box["base_config"] = base_config
        box["root_dir"] = root_dir
        return FactoryResult(exit_code=0)

    monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
    return box


def write_manifest(tmp_path: Path) -> Path:
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="cp-test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id="c1",
                title="c1",
                description="component one",
                dependencies=[],
                prd_path="scripts/kstrl/prd.json",
                branch_name="kstrl/factory/c1",
            ),
        ],
    )
    path = tmp_path / "manifest.json"
    manifest.save(path)
    return path


def invoke_factory(tmp_path: Path, *extra_args: str) -> Any:
    manifest_path = write_manifest(tmp_path)
    runner = CliRunner()
    return runner.invoke(
        cli_mod.cli,
        [
            "factory",
            "--manifest",
            str(manifest_path),
            "--root",
            str(tmp_path),
            "--yes",
            "--agent-cmd",
            "true",
            "--ui",
            "plain",
            *extra_args,
        ],
    )
