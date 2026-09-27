"""A run limit that is not a finite number >= 0 is refused where it is given (#571).

End to end: the real ``ks factory`` and ``ks retry`` in their own process,
against a real git repository, with a stub engineer that appends one line
to a file each time it is called. Before #571, ``ks factory
--agent-timeout nan`` ran the engineer and wrote ``NaN`` into the launch
record, and ``ks retry`` then refused that record as unreadable: a limit
that could bound nothing was accepted at launch and discovered only at
retry. ``inf`` crashed the run after launch with "timestamp out of range
for platform time_t", and ``-1`` was read as "no limit".

Every run limit ``launch_record.run_limits`` records is driven through
all three doors: the command-line option, kstrl.toml and the environment.
The assertion is exit 2 naming the limit and the value, zero agent calls,
and no run started.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.factory import FactoryConfig
from kstrl.launch_record import run_limits
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.timeout import TimeoutConfig
from tests.helpers import gitrepo
from tests.helpers.executables import write_executable
from tests.helpers.procs import kill_group
from tests.helpers.run_limits import limit_names, limit_option

#: Generous for a refusal measured at 0.2 to 0.4 s; a hang fails loudly.
FUSE_SECONDS = 120.0

#: The entry check's refusal line for a kstrl.toml or environment value.
REFUSAL = "configuration rejected before anything was started"

#: Every run limit, and where kstrl.toml and the environment set it.
DOORS: dict[str, tuple[str, str, str]] = {
    "max_cost_usd": ("factory", "max_cost_usd", "KSTRL_FACTORY_MAX_COST_USD"),
    "max_total_tokens": ("factory", "max_total_tokens", "KSTRL_FACTORY_MAX_TOTAL_TOKENS"),
    "max_adversarial_calls": (
        "factory",
        "max_adversarial_calls",
        "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS",
    ),
    "agent_timeout": ("timeout", "agent_iteration", "KSTRL_TIMEOUT_AGENT_ITERATION"),
    "component_timeout": ("timeout", "component_total", "KSTRL_TIMEOUT_COMPONENT"),
}

#: Phase 1 fails `storage` on its PRD alone: every verify command is `true`.
RUN_FLAGS = (
    "--no-tui",
    "--yes",
    "--ui",
    "plain",
    "--no-color",
    "--no-prs",
    "--max-retries",
    "0",
    "--max-parallel",
    "1",
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


def _is_float_limit(name: str) -> bool:
    return isinstance(run_limits(FactoryConfig(), TimeoutConfig())[name], float)


def _bad_values(name: str) -> tuple[str, ...]:
    """Values the option must refuse. An int option already refuses nan and inf."""
    return ("nan", "inf", "-inf", "-1") if _is_float_limit(name) else ("-1",)


def _repo(tmp_path: Path, toml: str = "") -> Path:
    """A committed repository whose one component, `storage`, fails Phase 1."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["a"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    prd = root / "scripts" / "kstrl" / "feature" / "storage" / "prd.json"
    prd.parent.mkdir(parents=True)
    prd.write_text(
        json.dumps({"branchName": "kstrl/factory/storage", "userStories": [story]}),
        encoding="utf-8",
    )
    (root / "kstrl.toml").write_text(toml, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": "storage",
                "title": "storage",
                "description": "",
                "dependencies": [],
                "prdPath": "scripts/kstrl/feature/storage/prd.json",
                "branchName": "kstrl/factory/storage",
            }
        ],
    }
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


def _env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's environment minus every kstrl knob, plus the counting stub."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KSTRL_", "FACTORY_")) and k not in ("AGENT_CMD", "MODEL")
    }
    calls = tmp_path / "agent.calls"
    stub = write_executable(
        tmp_path / "agent.sh",
        f"#!/bin/sh\necho call >> '{calls}'\ncat >/dev/null\necho '<promise>COMPLETE</promise>'\n",
    )
    env["AGENT_CMD"] = str(stub)
    env["KSTRL_AGENT_PROBE"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env.update(extra or {})
    return env


def _agent_calls(tmp_path: Path) -> int:
    calls = tmp_path / "agent.calls"
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


def _ks(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """The real CLI, bounded; the group is killed if it outlives the fuse."""
    child = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, _ = child.communicate(timeout=FUSE_SECONDS)
    except subprocess.TimeoutExpired:
        kill_group(child.pid)
        child.communicate()
        pytest.fail(f"`ks {args[0]}` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    return subprocess.CompletedProcess(child.args, child.returncode, out, "")


def _factory(root: Path, env: dict[str, str], *flags: str) -> subprocess.CompletedProcess[str]:
    manifest = root / "scripts" / "kstrl" / "manifest.json"
    return _ks(
        root, env, "factory", "--manifest", str(manifest), "--root", str(root), *RUN_FLAGS, *flags
    )


def _retry(root: Path, env: dict[str, str], *flags: str) -> subprocess.CompletedProcess[str]:
    return _ks(
        root,
        env,
        "retry",
        "storage",
        "--root",
        str(root),
        "--yes",
        "--ui",
        "plain",
        "--no-color",
        *flags,
    )


def _manifest(root: Path) -> Manifest:
    return Manifest.load(root / "scripts" / "kstrl" / "manifest.json")


def _status(root: Path) -> str:
    comp = _manifest(root).get_component("storage")
    assert comp is not None
    return comp.status


def _assert_no_run_started(root: Path, tmp_path: Path, out: str) -> None:
    assert "Traceback" not in out, out
    assert _agent_calls(tmp_path) == 0, out
    assert _manifest(root).run_id == "", out
    assert list(root.glob(".kstrl/runs/*/launch.json")) == [], out


def _failed_run(root: Path, tmp_path: Path) -> str:
    """Run the factory once so `storage` fails; return the run id."""
    first = _factory(root, _env(tmp_path))
    assert first.returncode == 1, first.stdout
    assert _status(root) == ComponentStatus.FAILED.value
    return _manifest(root).run_id


def test_every_run_limit_has_its_doors() -> None:
    """A limit added to run_limits fails here until its toml key and env var are named."""
    assert sorted(DOORS) == sorted(limit_names())


_OPTION_CASES = [(name, value) for name in limit_names() for value in _bad_values(name)]


@pytest.mark.parametrize(
    ("name", "value"), _OPTION_CASES, ids=[f"{n}={v}" for n, v in _OPTION_CASES]
)
def test_a_bad_limit_option_on_factory_exits_2_before_any_agent_call(
    tmp_path: Path, name: str, value: str
) -> None:
    root = _repo(tmp_path)

    result = _factory(root, _env(tmp_path), limit_option(name), value)

    assert result.returncode == 2, result.stdout
    assert f"Invalid value for '{limit_option(name)}'" in result.stdout, result.stdout
    assert f"got {float(value) if _is_float_limit(name) else int(value)}" in result.stdout
    _assert_no_run_started(root, tmp_path, result.stdout)


_CONFIG_CASES = [
    (name, door, value)
    for name in DOORS
    for door in ("toml", "env")
    for value in ("nan", "inf", "-1")
]


@pytest.mark.parametrize(
    ("name", "door", "value"),
    _CONFIG_CASES,
    ids=[f"{n}-{d}={v}" for n, d, v in _CONFIG_CASES],
)
def test_a_bad_limit_in_config_exits_2_before_any_agent_call(
    tmp_path: Path, name: str, door: str, value: str
) -> None:
    section, key, env_var = DOORS[name]
    if door == "toml":
        root = _repo(tmp_path, f"[{section}]\n{key} = {value}\n")
        env = _env(tmp_path)
    else:
        root = _repo(tmp_path)
        env = _env(tmp_path, {env_var: value})

    result = _factory(root, env)

    assert result.returncode == 2, result.stdout
    assert REFUSAL in result.stdout, result.stdout
    assert f"[{section}]" in result.stdout, result.stdout
    assert (key if door == "toml" else env_var) in result.stdout, result.stdout
    assert value in result.stdout, result.stdout
    _assert_no_run_started(root, tmp_path, result.stdout)


@pytest.mark.parametrize("name", limit_names())
def test_a_bad_limit_option_on_retry_is_refused_before_anything_changes(
    tmp_path: Path, name: str
) -> None:
    root = _repo(tmp_path)
    _failed_run(root, tmp_path)
    before = _agent_calls(tmp_path)
    value = _bad_values(name)[0]

    result = _retry(root, _env(tmp_path), limit_option(name), value)

    assert result.returncode == 2, result.stdout
    assert f"Invalid value for '{limit_option(name)}'" in result.stdout, result.stdout
    assert "Traceback" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) == before
    assert _status(root) == ComponentStatus.FAILED.value


@pytest.mark.parametrize("door", ["toml", "env"])
def test_a_bad_limit_in_config_is_refused_by_retry(tmp_path: Path, door: str) -> None:
    root = _repo(tmp_path)
    _failed_run(root, tmp_path)
    before = _agent_calls(tmp_path)
    extra: dict[str, str] = {}
    if door == "toml":
        (root / "kstrl.toml").write_text("[timeout]\nagent_iteration = nan\n", encoding="utf-8")
    else:
        extra = {"KSTRL_TIMEOUT_AGENT_ITERATION": "nan"}

    result = _retry(root, _env(tmp_path, extra))

    assert result.returncode == 2, result.stdout
    assert REFUSAL in result.stdout, result.stdout
    assert "agent_iteration" in result.stdout, result.stdout
    assert "Traceback" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) == before
    assert _status(root) == ComponentStatus.FAILED.value


def test_a_record_written_before_571_with_a_non_finite_limit_is_still_refused(
    tmp_path: Path,
) -> None:
    """The launch-record reader keeps refusing a non-finite recorded limit."""
    root = _repo(tmp_path)
    run_id = _failed_run(root, tmp_path)
    before = _agent_calls(tmp_path)
    path = root / ".kstrl" / "runs" / run_id / "launch.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["limits"]["agent_timeout"] = math.nan
    path.write_text(json.dumps(record), encoding="utf-8")

    result = _retry(root, _env(tmp_path))

    assert result.returncode == 2, result.stdout
    assert "limits['agent_timeout'] is nan, not a finite number" in result.stdout, result.stdout
    assert _agent_calls(tmp_path) == before
    assert _status(root) == ComponentStatus.FAILED.value


def test_zero_and_positive_limits_still_reach_the_engineer(tmp_path: Path) -> None:
    """Control: the stub counts, and 0 (no limit) and a real limit are not refused."""
    root = _repo(tmp_path, "[timeout]\nagent_iteration = 0\ncomponent_total = 600\n")
    env = _env(tmp_path, {"KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS": "0"})

    result = _factory(
        root, env, "--max-cost-usd", "0", "--agent-timeout", "300", "--max-total-tokens", "0"
    )

    assert result.returncode == 1, result.stdout
    assert REFUSAL not in result.stdout, result.stdout
    assert "Invalid value" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) >= 1, result.stdout


def test_a_record_written_before_571_with_a_negative_recorded_flag_is_refused(
    tmp_path: Path,
) -> None:
    """A flag recorded before #571 (`--agent-timeout -1`) is refused on replay.

    Before #571 `ks factory --agent-timeout -1` ran and recorded the flag.
    `ks retry` replays recorded flags through each option's own click type,
    so the replay is refused, with the launch record's remedy, before the
    retry changes anything.
    """
    root = _repo(tmp_path)
    first = _factory(root, _env(tmp_path), "--agent-timeout", "300")
    assert first.returncode == 1, first.stdout
    run_id = _manifest(root).run_id
    before = _agent_calls(tmp_path)
    path = root / ".kstrl" / "runs" / run_id / "launch.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["flags"]["agent_timeout"] == 300.0, record["flags"]
    # What `ks factory --agent-timeout -1` wrote before #571: both halves.
    record["flags"]["agent_timeout"] = -1.0
    record["limits"]["agent_timeout"] = -1.0
    path.write_text(json.dumps(record), encoding="utf-8")

    result = _retry(root, _env(tmp_path))

    assert result.returncode == 2, result.stdout
    assert "--agent-timeout must be >= 0, got -1.0" in result.stdout, result.stdout
    assert "to retry without the recorded flags" in result.stdout, result.stdout
    assert "Traceback" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) == before
    assert _status(root) == ComponentStatus.FAILED.value


# #583: a limit the operator set that kstrl cannot read is refused, never
# read as "no limit". `KSTRL_AGENT_BUDGET_USD=lots` read as no ceiling, and
# `ks run --sleep nan` ran the engineer and then failed after the spend.


@pytest.mark.parametrize("value", ["lots", "5usd", "$5"])
def test_an_unreadable_agent_budget_in_the_environment_exits_2_before_any_agent_call(
    tmp_path: Path, value: str
) -> None:
    root = _repo(tmp_path)

    result = _factory(root, _env(tmp_path, {"KSTRL_AGENT_BUDGET_USD": value}))

    assert result.returncode == 2, result.stdout
    assert REFUSAL in result.stdout, result.stdout
    assert f"KSTRL_AGENT_BUDGET_USD={value}" in result.stdout, result.stdout
    _assert_no_run_started(root, tmp_path, result.stdout)


@pytest.mark.parametrize(
    ("literal", "shown"),
    [('"lots"', "'lots'"), ("true", "True"), ("[5]", "[5]")],
    ids=["string", "bool", "array"],
)
def test_an_unreadable_agent_budget_in_kstrl_toml_exits_2_before_any_agent_call(
    tmp_path: Path, literal: str, shown: str
) -> None:
    root = _repo(tmp_path, f"[agent]\nbudget_usd = {literal}\n")

    result = _factory(root, _env(tmp_path))

    assert result.returncode == 2, result.stdout
    assert REFUSAL in result.stdout, result.stdout
    assert f"budget_usd = {shown}" in result.stdout, result.stdout
    _assert_no_run_started(root, tmp_path, result.stdout)


@pytest.mark.parametrize(
    ("toml", "env"),
    [
        ("", {"KSTRL_AGENT_BUDGET_USD": ""}),
        ("", {"KSTRL_AGENT_BUDGET_USD": "0"}),
        ("", {"KSTRL_AGENT_BUDGET_USD": "2.5"}),
        ('[agent]\nbudget_usd = ""\n', {}),
        ("[agent]\nbudget_usd = 0\n", {}),
    ],
    ids=["env-empty", "env-zero", "env-set", "toml-empty", "toml-zero"],
)
def test_an_unset_or_readable_agent_budget_still_reaches_the_engineer(
    tmp_path: Path, toml: str, env: dict[str, str]
) -> None:
    """Control: "" and 0 still mean no ceiling, and a number is accepted."""
    root = _repo(tmp_path, toml)

    result = _factory(root, _env(tmp_path, env))

    assert result.returncode == 1, result.stdout
    assert REFUSAL not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) >= 1, result.stdout


def _loop_args(command: str, root: Path) -> tuple[str, ...]:
    """What each looping command needs to reach the engineer on this repo."""
    if command == "run":
        prd = root / "scripts" / "kstrl" / "feature" / "storage" / "prd.json"
        return ("run", "1", "--prd", str(prd), "--no-verify", "--branch", "")
    return (command,)


_SLEEP_CASES = [(c, v) for c in ("run", "understand", "feature") for v in ("nan", "inf", "-1")]


@pytest.mark.parametrize(
    ("command", "value"), _SLEEP_CASES, ids=[f"{c}={v}" for c, v in _SLEEP_CASES]
)
def test_a_bad_sleep_exits_2_before_any_agent_call(
    tmp_path: Path, command: str, value: str
) -> None:
    root = _repo(tmp_path)

    result = _ks(
        root,
        _env(tmp_path),
        *_loop_args(command, root),
        "--root",
        str(root),
        "--ui",
        "plain",
        "--no-color",
        f"--sleep={value}",
    )

    assert result.returncode == 2, result.stdout
    assert "Invalid value for '--sleep'" in result.stdout, result.stdout
    assert f"got {float(value)}" in result.stdout, result.stdout
    assert "Traceback" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) == 0, result.stdout


def test_a_zero_sleep_still_reaches_the_engineer(tmp_path: Path) -> None:
    """Control: the stub counts under `ks run`, and 0 is not refused."""
    root = _repo(tmp_path)

    result = _ks(
        root,
        _env(tmp_path),
        *_loop_args("run", root),
        "--root",
        str(root),
        "--ui",
        "plain",
        "--no-color",
        "--sleep=0",
    )

    assert "Invalid value" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) >= 1, result.stdout
