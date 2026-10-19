"""TUI surface B1 and #485: ``ks retry`` prints the plan, including what it leaves out.

Both tests drive the real ``ks retry`` through CliRunner, answered Quit at
the confirmation, and read the rendered plan: a FAILED sibling the retry
does not touch is named with its own ``ks retry`` command, and the line
says ``(none)`` when nothing is left out.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import kstrl.cli.cli as cli_mod
import kstrl.cli.cli_architect as cli_architect_mod
import kstrl.cli.cli_factory as cli_factory_mod
import kstrl.cli.cli_preflight as cli_preflight_mod
from kstrl.plan.manifest import Component, ComponentStatus, Manifest
from tests.helpers.run_limits import every_limit_argv
from tests.test_retry_carries_flags import _RecordingChannel


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


def _skipped_on_one_failure() -> Manifest:
    return _manifest_of(
        _component("a", [], ComponentStatus.FAILED),
        _component("b", ["a"], ComponentStatus.SKIPPED),
    )


class TestTheRetryCommandPrintsWhatItLeavesOut:
    """#485 through the real `ks retry`, answered Quit at the confirmation."""

    def _retry(
        self,
        tmp_path: Path,
        manifest: Manifest,
        component_id: str,
        monkeypatch: pytest.MonkeyPatch,
        code: int = 0,
    ) -> str:
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest_file.parent.mkdir(parents=True)
        manifest.save(manifest_file)
        monkeypatch.setattr(cli_architect_mod, "UiInteractionChannel", _RecordingChannel)
        monkeypatch.setattr(cli_factory_mod, "UiInteractionChannel", _RecordingChannel)
        monkeypatch.setattr(cli_preflight_mod, "UiInteractionChannel", _RecordingChannel)
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
        assert result.exit_code == code, result.output
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


    def test_a_component_whose_dependency_failed_is_refused_before_anything_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user retried a component whose dependency had failed: its branch
        was deleted, then nothing was scheduled. It is refused, naming the
        order that works, and the manifest is left as it was."""
        manifest = _manifest_of(
            _component("a", [], ComponentStatus.FAILED),
            _component("b", ["a"], ComponentStatus.FAILED),
        )

        out = self._retry(tmp_path, manifest, "b", monkeypatch, code=2)

        assert "run ks retry a first, then ks retry b" in " ".join(out.split()), out
        saved = Manifest.load(tmp_path / "scripts" / "kstrl" / "manifest.json")
        assert [(c.id, c.status) for c in saved.components] == [("a", "failed"), ("b", "failed")]


def test_the_branch_probe_asks_for_a_branch_not_any_ref(tmp_path: Path) -> None:
    """Plant P4: without ``refs/heads/`` a TAG named like the failed
    branch answers 0, and prepare_retry would run ``git branch -D``
    on a branch that does not exist."""
    from kstrl.factory.retry_plan import _failed_branch_probe
    from tests.helpers.gitrepo import git_in, set_identity

    git_in(tmp_path, "init", "-q", "-b", "main")
    set_identity(tmp_path)
    git_in(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    git_in(tmp_path, "tag", "kstrl/api")
    assert _failed_branch_probe(tmp_path, "kstrl/api") != 0
    git_in(tmp_path, "branch", "kstrl/web")
    assert _failed_branch_probe(tmp_path, "kstrl/web") == 0
