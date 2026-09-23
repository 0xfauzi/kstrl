"""`ks retry` re-enters the factory with the flags of the run it resumes (#436).

The defect: `ks factory --max-cost-usd 5 --max-parallel 1` failed a
component, and `ks retry` re-entered the factory with env > kstrl.toml >
defaults, so the resumed run had no cost ceiling and four-way parallelism
and nothing said so. The fix records the run's command-line flags in
``.kstrl/runs/<run_id>/launch.json`` before the run spends anything, and
`ks retry` replays them through `ks factory` itself.

The end-to-end tests drive the real CLI in a subprocess against a real git
repository, with a stub engineer (``AGENT_CMD``) and no LLM. A component
fails because its PRD story is not marked as passing, which Phase 1
checks; every verify command is ``true`` so nothing else can fail it.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.interaction import PromptRequest, PromptResponse
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from tests.helpers import gitrepo

#: The flags the original run is launched with in the end-to-end tests.
#: Every verify command is `true` so Phase 1 fails on the PRD alone.
RUN_FLAGS = (
    "--max-parallel",
    "1",
    "--max-retries",
    "0",
    "--no-prs",
    "--review-mode",
    "skip",
    "--contract-check",
    "skip",
    "--test-command",
    "true",
    "--typecheck-command",
    "true",
    "--lint-command",
    "true",
)

REFUSAL = "Refusing to run: the retry cannot carry over the configuration of the run it resumes"


def _prd(passes: bool, branch: str) -> str:
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": passes,
        "notes": "",
    }
    return json.dumps({"branchName": branch, "userStories": [story]})


def _repo(tmp_path: Path) -> Path:
    """A git repo with `storage` (fails Phase 1) and `cli` (depends on it)."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    components = []
    for cid, passes, deps in (("storage", False, []), ("cli", True, ["storage"])):
        prd = root / "scripts" / "kstrl" / "feature" / cid / "prd.json"
        prd.parent.mkdir(parents=True)
        prd.write_text(_prd(passes, f"kstrl/factory/{cid}"), encoding="utf-8")
        components.append(
            {
                "id": cid,
                "title": cid,
                "description": "",
                "dependencies": deps,
                "prdPath": f"scripts/kstrl/feature/{cid}/prd.json",
                "branchName": f"kstrl/factory/{cid}",
            }
        )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": components,
    }
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


def _env(**extra: str) -> dict[str, str]:
    """The caller's environment with every kstrl knob removed, plus a stub engineer."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    env["AGENT_CMD"] = "echo '<promise>COMPLETE</promise>'"
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    env.update(extra)
    return env


def _ks(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl",
            *args,
            "--root",
            str(root),
            "--yes",
            "--ui",
            "plain",
            "--no-color",
        ],
        cwd=root,
        env=env if env is not None else _env(),
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _factory(
    root: Path, *flags: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    manifest = root / "scripts" / "kstrl" / "manifest.json"
    return _ks(root, "factory", "--manifest", str(manifest), "--no-tui", *flags, env=env)


def _manifest(root: Path) -> Manifest:
    return Manifest.load(root / "scripts" / "kstrl" / "manifest.json")


def _status(root: Path, cid: str) -> str:
    comp = _manifest(root).get_component(cid)
    assert comp is not None
    return comp.status


def _execution_header(output: str) -> str:
    """The `== Factory: Execution ==` section, up to the first component start."""
    section = output.split("== Factory: Execution ==", 1)[1]
    return section.split("Starting:", 1)[0]


def _failed_run(root: Path, *flags: str, env: dict[str, str] | None = None) -> str:
    """Run the factory once so `storage` fails; return the run id."""
    first = _factory(root, *flags, env=env)
    assert first.returncode == 1, first.stdout + first.stderr
    assert _status(root, "storage") == ComponentStatus.FAILED.value
    return _manifest(root).run_id


class TestRetryReplaysTheRunsFlags:
    def test_retry_runs_under_the_original_ceiling_and_parallelism(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", "--keep-worktrees-on-failure", *RUN_FLAGS)

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        # The component fails again (its PRD still does not pass), so 1.
        assert retried.returncode == 1, out
        assert f"Resuming with the flags of run {run_id}:" in out
        assert "--max-cost-usd 5.0" in out
        assert "--no-prs" in out, out
        resuming = next(ln for ln in out.splitlines() if "Resuming with the flags" in ln)
        assert "--keep-worktrees-on-failure" in resuming, resuming
        header = _execution_header(out)
        assert re.search(r"Max parallel:\s*1\n", header), header
        assert re.search(r"Max retries:\s*0\n", header), header
        assert re.search(r"Review mode:\s*skip\n", header), header
        assert "Cost ceiling: $5.0" in header, header

        # A retry of the retry still carries them: the resumed run wrote
        # its own record with the flags it replayed.
        second_id = _manifest(root).run_id
        assert second_id != run_id
        again = _ks(root, "retry", "storage")
        out = again.stdout + again.stderr
        assert again.returncode == 1, out
        assert f"Resuming with the flags of run {second_id}:" in out
        header = _execution_header(out)
        assert re.search(r"Max parallel:\s*1\n", header), header
        assert "Cost ceiling: $5.0" in header, header

    def test_a_flag_on_the_retry_wins_over_the_recorded_one(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)

        retried = _ks(root, "retry", "storage", "--max-cost-usd", "9")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        assert "Cost ceiling: $9.0" in _execution_header(out)

    def test_an_uncapped_run_retries_uncapped_and_says_so(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        _failed_run(root, *RUN_FLAGS)

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        assert re.search(r"Cost ceiling:\s*disabled\n", out), out


class TestRetryRefusesToDropTheCeiling:
    def test_no_record_and_no_ceiling_is_refused_before_anything_changes(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        # A run that predates #436 left no record.
        record = root / ".kstrl" / "runs" / _manifest(root).run_id / "launch.json"
        record.unlink()

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert "left no launch record" in out
        assert "--max-cost-usd" in out
        # Refused BEFORE the reset: the manifest still says failed.
        assert _status(root, "storage") == ComponentStatus.FAILED.value
        assert _status(root, "cli") == ComponentStatus.SKIPPED.value

    def test_no_record_with_a_ceiling_on_the_retry_proceeds(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        (root / ".kstrl" / "runs" / run_id / "launch.json").unlink()

        retried = _ks(root, "retry", "storage", "--max-cost-usd", "7")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        assert f"No launch record for run {run_id}" in out
        assert "Cost ceiling: $7.0" in _execution_header(out)

    def test_a_ceiling_that_came_from_env_and_is_gone_is_refused(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        _failed_run(root, *RUN_FLAGS, env=_env(KSTRL_FACTORY_MAX_COST_USD="5"))

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert "ran under a cost ceiling of $5.0" in out
        assert _status(root, "storage") == ComponentStatus.FAILED.value


class TestTheRecordIsTheRunsOwn:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("runId", "someone-else"), ("manifest", "/elsewhere/manifest.json")],
    )
    def test_a_record_of_another_run_or_manifest_is_refused(
        self, tmp_path: Path, field: str, value: str
    ) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        path = root / ".kstrl" / "runs" / run_id / "launch.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record[field] = value
        path.write_text(json.dumps(record), encoding="utf-8")

        retried = _ks(root, "retry", "storage", "--max-cost-usd", "5")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert f"is not the record of this run: {field} is {value!r}" in out
        assert _status(root, "storage") == ComponentStatus.FAILED.value

    def test_an_unreadable_record_is_refused(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        (root / ".kstrl" / "runs" / run_id / "launch.json").write_text("{", encoding="utf-8")

        retried = _ks(root, "retry", "storage", "--max-cost-usd", "5")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert "cannot be read" in out
        assert _status(root, "storage") == ComponentStatus.FAILED.value

    def test_a_record_that_cannot_be_opened_is_refused(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        path = root / ".kstrl" / "runs" / run_id / "launch.json"
        path.unlink()
        path.mkdir()

        retried = _ks(root, "retry", "storage", "--max-cost-usd", "5")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert "cannot be read" in out
        assert _status(root, "storage") == ComponentStatus.FAILED.value

    def test_a_flag_value_that_is_not_a_scalar_is_refused(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        path = root / ".kstrl" / "runs" / run_id / "launch.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["flags"]["max_parallel"] = [1]
        path.write_text(json.dumps(record), encoding="utf-8")

        retried = _ks(root, "retry", "storage", "--max-cost-usd", "5")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert "not a string, number or boolean" in out
        assert _status(root, "storage") == ComponentStatus.FAILED.value

    def test_the_record_names_its_run_and_manifest(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
        record = json.loads(
            (root / ".kstrl" / "runs" / run_id / "launch.json").read_text(encoding="utf-8")
        )
        assert record["runId"] == run_id
        assert record["manifest"] == str((root / "scripts" / "kstrl" / "manifest.json").resolve())
        assert record["maxCostUsd"] == 5.0
        assert record["flags"]["max_parallel"] == 1
        assert "yes" not in record["flags"]

    def test_a_run_that_cannot_write_its_record_does_not_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path)
        # A directory where the record goes makes the write fail.
        (root / ".kstrl" / "runs" / "run-x" / "launch.json").mkdir(parents=True)

        def started(*args: object, **kwargs: object) -> None:
            raise AssertionError("a component started")

        # Never reach an engineer, on this tree or on one without the fix.
        monkeypatch.setattr("kstrl.factory._run_component", started)
        base = KstrlConfig.load(root)
        base.agent_cmd = "false"
        buffer = io.StringIO()
        result = run_factory(
            _manifest(root),
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip", max_retries=0),
            base,
            PlainUI(no_color=True, file=buffer),
            root,
            manifest_path=root / "scripts" / "kstrl" / "manifest.json",
            run_id="run-x",
        )
        out = buffer.getvalue()
        assert result.exit_code == 2, out
        assert "Refusing to run: the run's launch record cannot be written" in out
        assert "Starting:" not in out


class _RecordingChannel:
    """Stands in for the terminal: records the question and answers Quit."""

    headers: list[str] = []

    def __init__(self, ui: Any) -> None:
        pass

    def can_prompt(self) -> bool:
        return True

    def request(self, req: PromptRequest) -> PromptResponse:
        _RecordingChannel.headers.append(req.header)
        return PromptResponse(request_id=req.request_id, choice=1, answered=True)


def test_the_confirmation_names_every_component_the_retry_reenters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _failed_run(root, "--max-cost-usd", "5", *RUN_FLAGS)
    _RecordingChannel.headers = []
    monkeypatch.setattr(cli_mod, "UiInteractionChannel", _RecordingChannel)
    for name in [k for k in os.environ if k.startswith("KSTRL_")]:
        monkeypatch.delenv(name)

    result = CliRunner().invoke(
        cli_mod.cli, ["retry", "storage", "--root", str(root), "--ui", "plain", "--no-color"]
    )
    assert result.exit_code == 0, result.output
    assert _RecordingChannel.headers == [
        "Re-enter the factory to retry 'storage' and the dependents it reset: 'cli'?"
    ]
    # Quit exits at once, so what is on the output was printed BEFORE the
    # question: the operator sees the ceiling and parallelism first.
    assert "Cost ceiling: $5.0" in result.output, result.output
    assert re.search(r"Max parallel:\s*1\n", result.output), result.output


def test_retry_flags_are_pinned_against_factory() -> None:
    """Every `ks factory` option is accepted by retry, replayed, or listed as never replayed."""
    from kstrl.launch_record import NOT_REPLAYED

    factory = {p.name: p for p in cli_mod.cli.commands["factory"].params}
    retry = {p.name: p for p in cli_mod.cli.commands["retry"].params}
    # Replayed from the run's launch record by `ks retry`.
    replayed = {
        "max_retries",
        "create_prs",
        "verify_command",
        "test_command",
        "typecheck_command",
        "lint_command",
        "no_verify",
        "dead_code_cleanup",
        "dead_code_command",
        "mutation_testing",
        "mutation_threshold",
        "review_mode",
        "review_agent_cmd",
        "review_model",
        "security_mode",
        "security_agent_cmd",
        "security_model",
        "security_fail_threshold",
        "contract_check",
        "contract_test_cmd",
        "agent_timeout",
        "component_timeout",
        "max_adversarial_calls",
        "max_total_tokens",
        "pause_before_pr_merge",
        "no_worktrees",
        "agent_cmd",
        "model",
        "reasoning",
        "agent_type",
        "sleep",
        # Also retry options: a value given on the retry wins.
        "max_parallel",
        "max_cost_usd",
        "keep_worktrees_on_failure",
    }
    retry_only = {"component_id"}
    for name in factory:
        assert name in replayed or name in NOT_REPLAYED, (
            f"`ks factory` has --{name.replace('_', '-')}, which `ks retry` neither "
            "replays nor lists as never replayed: add it to `replayed` in this test "
            "(the launch record carries it) or to NOT_REPLAYED in "
            "kstrl/launch_record.py with a reason"
        )
        assert not (name in replayed and name in NOT_REPLAYED), name
    for name in set(replayed) | set(NOT_REPLAYED):
        assert name in factory, f"{name} is listed but `ks factory` has no such option"
    for name, param in retry.items():
        if name in retry_only:
            continue
        assert name in factory, f"`ks retry` has --{name} that `ks factory` lacks"
        assert param.opts == factory[name].opts, name
        assert type(param.type) is type(factory[name].type), name
    assert set(retry) - retry_only == {
        "root",
        "manifest_path",
        "progress_log",
        "keep_worktrees_on_failure",
        "force_lock",
        "max_cost_usd",
        "max_parallel",
        "yes",
        "ui",
        "no_color",
    }
    for name, reason in NOT_REPLAYED.items():
        assert reason.strip(), name
    # A replayed value goes through JSON and back as one scalar token, so
    # a Path, multiple=True or nargs>1 option cannot be replayed.
    for name in replayed:
        param = factory[name]
        assert isinstance(param, click.Option), name
        assert not isinstance(param.type, click.Path), (
            f"--{name.replace('_', '-')} takes a path, which the launch record cannot carry"
        )
        assert not param.multiple and param.nargs == 1, (
            f"--{name.replace('_', '-')} takes several values, which the launch record cannot carry"
        )
