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
from kstrl.launch_record import run_limits
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from tests.helpers import gitrepo
from tests.helpers.run_limits import every_limit_argv, limit_option

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
        assert re.search(r"Cost ceiling:\s*no limit\n", out), out


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

        retried = _ks(
            root,
            "retry",
            "storage",
            "--max-cost-usd",
            "7",
            # No record, so every other run limit is stated too (#526).
            *every_limit_argv(skip={"max_cost_usd"}),
        )
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        assert f"No launch record for run {run_id}" in out
        assert "Cost ceiling: $7.0" in _execution_header(out)

    def test_a_ceiling_that_came_from_env_and_is_gone_is_refused(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, *RUN_FLAGS, env=_env(KSTRL_FACTORY_MAX_COST_USD="5"))

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert f"--max-cost-usd: run {run_id} ran under --max-cost-usd 5.0" in out
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
        assert record["limits"] == {
            **run_limits(FactoryConfig(), TimeoutConfig()),
            "max_cost_usd": 5.0,
        }
        assert "maxCostUsd" not in record
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
    assert re.search(r"--max-cost-usd:\s*5.0\n", result.output), result.output
    assert re.search(r"--max-total-tokens:\s*no limit\n", result.output), result.output
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
        "max_total_tokens",
        "max_adversarial_calls",
        "agent_timeout",
        "component_timeout",
        "max_parallel",
        "yes",
        "ui",
        "no_color",
    }
    # #526: the retry can state every run limit, spelled as `ks factory` spells it.
    for name in run_limits(FactoryConfig(), TimeoutConfig()):
        assert name in retry, f"`ks retry` cannot state the run limit {name!r}"
        assert factory[name].opts == ["--" + name.replace("_", "-")], name
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


#: One environment variable per run limit, so a test can set each limit
#: somewhere a retry does not replay it (#526).
LIMIT_ENV = {
    "max_cost_usd": "KSTRL_FACTORY_MAX_COST_USD",
    "max_total_tokens": "KSTRL_FACTORY_MAX_TOTAL_TOKENS",
    "max_adversarial_calls": "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS",
    "agent_timeout": "KSTRL_TIMEOUT_AGENT_ITERATION",
    "component_timeout": "KSTRL_TIMEOUT_COMPONENT",
}


class TestRetryKeepsEveryRunLimit:
    """#526: every run limit is carried by `ks retry` or refused, not only the cost ceiling."""

    def test_every_run_limit_has_an_environment_case(self) -> None:
        assert set(LIMIT_ENV) == set(run_limits(FactoryConfig(), TimeoutConfig())), (
            "a run limit was added to launch_record.run_limits: add its environment "
            "variable to LIMIT_ENV so the tests below drive it"
        )

    @pytest.mark.parametrize("name", sorted(LIMIT_ENV))
    def test_a_limit_from_the_environment_that_is_gone_is_refused(
        self, tmp_path: Path, name: str
    ) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, *RUN_FLAGS, env=_env(**{LIMIT_ENV[name]: "600"}))

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 2, out
        assert REFUSAL in out
        assert f"{limit_option(name)}: run {run_id} ran under {limit_option(name)} 600" in out, out
        others = [other for other in LIMIT_ENV if other != name]
        assert not any(f"{limit_option(other)}: run" in out for other in others), out
        # Refused BEFORE the reset: the manifest still says failed.
        assert _status(root, "storage") == ComponentStatus.FAILED.value

    @pytest.mark.parametrize("name", sorted(LIMIT_ENV))
    def test_a_limit_stated_on_the_retry_is_kept(self, tmp_path: Path, name: str) -> None:
        root = _repo(tmp_path)
        _failed_run(root, *RUN_FLAGS, env=_env(**{LIMIT_ENV[name]: "600"}))

        retried = _ks(root, "retry", "storage", limit_option(name), "700")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        resuming = next(ln for ln in out.splitlines() if "Resuming with the flags" in ln)
        assert f"{limit_option(name)} 700" in resuming, resuming
        # The stated value reached the factory the retry re-entered.
        assert "700" in _execution_header(out), out

    def test_every_limit_the_execution_header_states_is_recorded(self, tmp_path: Path) -> None:
        """A limit the header prints but ``run_limits`` lacks reads `no limit` here."""
        root = _repo(tmp_path)
        limits = run_limits(FactoryConfig(), TimeoutConfig())
        run_id = _failed_run(root, *every_limit_argv("600"), *RUN_FLAGS)

        record = json.loads(
            (root / ".kstrl" / "runs" / run_id / "launch.json").read_text(encoding="utf-8")
        )
        assert record["limits"] == dict.fromkeys(limits, 600)
        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        header = _execution_header(out)
        assert "no limit" not in header, header

    def test_a_record_written_before_526_still_loads(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        run_id = _failed_run(root, *RUN_FLAGS)
        path = root / ".kstrl" / "runs" / run_id / "launch.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["limits"]
        record["maxCostUsd"] = 0.0
        path.write_text(json.dumps(record), encoding="utf-8")

        refused = _ks(root, "retry", "storage")
        out = refused.stdout + refused.stderr
        assert refused.returncode == 2, out
        assert "cannot be read" not in out and "is not the record" not in out, out
        unknown = [name for name in LIMIT_ENV if name != "max_cost_usd"]
        for name in unknown:
            assert (
                f"{limit_option(name)}: run {run_id} left no launch record of this limit" in out
            ), out
        assert "--max-cost-usd: run" not in out, out

        retried = _ks(root, "retry", "storage", *every_limit_argv(skip={"max_cost_usd"}))
        out = retried.stdout + retried.stderr
        assert retried.returncode == 1, out
        assert f"Resuming with the flags of run {run_id}:" in out

    def test_a_recorded_limit_this_version_does_not_know_is_refused(self, tmp_path: Path) -> None:
        """A record from a newer kstrl names a limit this one lacks: refused, not ignored."""
        root = _repo(tmp_path)
        run_id = _failed_run(root, *RUN_FLAGS)
        path = root / ".kstrl" / "runs" / run_id / "launch.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["limits"]["max_widgets"] = 3
        path.write_text(json.dumps(record), encoding="utf-8")

        refused = _ks(root, "retry", "storage")
        out = refused.stdout + refused.stderr
        assert refused.returncode == 2, out
        assert f"--max-widgets: run {run_id} ran under --max-widgets 3" in out, out
        assert _status(root, "storage") == ComponentStatus.FAILED.value

    @pytest.mark.parametrize(
        ("option", "value"), [("--max-cost-usd", "-1"), ("--max-total-tokens", "-5")]
    )
    def test_a_bad_ceiling_on_the_retry_is_refused_before_anything_changes(
        self, tmp_path: Path, option: str, value: str
    ) -> None:
        root = _repo(tmp_path)
        _failed_run(root, *RUN_FLAGS)

        refused = _ks(root, "retry", "storage", option, value)
        out = refused.stdout + refused.stderr
        assert refused.returncode == 2, out
        assert f"{option} must be >= 0" in out, out
        # Refused BEFORE the reset: the manifest still says failed.
        assert _status(root, "storage") == ComponentStatus.FAILED.value


def test_the_tui_line_states_every_set_limit_not_only_the_cost_ceiling() -> None:
    """#552 after #551: the retry screen's line is built from `plan.limits`,
    so a token or timeout limit the retry keeps is stated, not dropped."""
    from kstrl.retry_plan import ResumePlan, limits_line

    limits = dict.fromkeys(run_limits(FactoryConfig(), TimeoutConfig()), 0.0)
    limits.update(max_total_tokens=5000.0, agent_timeout=90.0)
    plan = ResumePlan("r", True, (), tuple(limits.items()), 2)
    assert limits_line(plan) == "--max-total-tokens 5000, --agent-timeout 90"
    unset = ResumePlan("r", True, (), tuple(dict.fromkeys(limits, 0.0).items()), 2)
    assert limits_line(unset) == "no run limit"
