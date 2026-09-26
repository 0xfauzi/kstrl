"""A closed config value kstrl will refuse is refused before any agent call (#562).

End to end: the real ``ks run`` and ``ks factory`` in their own process,
with a stub engineer that appends one line to a file each time it is
called. Before #562, ``[factory] review_mode = "soft"`` passed config
loading, the engineer ran, and Phase 2 then crashed with
``ValueError: 'soft' is not a valid ReviewMode``: exit 1 after one paid
call. ``[agent] type`` and ``[security] agent_type`` were not refused at
all while a custom command was set.

Every CLOSED field in ``tests/helpers/closed_vocabulary.py`` is driven
through both commands at each of its doors. The assertion is exit 2 with
the entry check's own refusal line, and zero agent calls. Exit 2 alone
is not enough: click's usage errors exit 2 as well.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.config import STRING_KEYS
from tests.helpers import gitrepo
from tests.helpers.closed_vocabulary import BAD_VALUE, CLOSED, ClosedField
from tests.helpers.executables import write_executable
from tests.helpers.procs import kill_group

#: Generous for a refusal measured at 0.13 to 0.17 s; a hang fails loudly.
FUSE_SECONDS = 120.0

REFUSAL = "configuration rejected before anything was started"

_STORY = {
    "id": "US-001",
    "title": "t",
    "acceptanceCriteria": ["a"],
    "priority": 1,
    "passes": False,
    "notes": "",
}


def _project(tmp_path: Path, toml: str) -> Path:
    """A committed repo with a loop PRD, one factory component and kstrl.toml."""
    root = tmp_path / "proj"
    kdir = root / "scripts" / "kstrl"
    comp = kdir / "feature" / "comp-a"
    comp.mkdir(parents=True)
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (kdir / "prompt.md").write_text("do the thing\n", encoding="utf-8")
    (kdir / "prd.json").write_text(
        json.dumps({"branchName": "kstrl/test", "userStories": [_STORY]}), encoding="utf-8"
    )
    (comp / "prd.json").write_text(
        json.dumps({"branchName": "kstrl/factory/comp-a", "userStories": [_STORY]}),
        encoding="utf-8",
    )
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "t",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": "comp-a",
                "title": "A",
                "description": "",
                "dependencies": [],
                "prdPath": "scripts/kstrl/feature/comp-a/prd.json",
                "branchName": "kstrl/factory/comp-a",
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "kstrl.toml").write_text(toml, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    return root


def _env(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    """The caller's environment minus every kstrl knob, plus the counting stub."""
    string_key_vars = {env_var for _s, _k, env_var, _f, _p in STRING_KEYS}
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KSTRL_", "FACTORY_")) and k not in string_key_vars
    }
    calls = tmp_path / "agent.calls"
    stub = write_executable(
        tmp_path / "agent.sh",
        f"#!/bin/sh\necho call >> '{calls}'\ncat >/dev/null\necho '<promise>COMPLETE</promise>'\n",
    )
    env["AGENT_CMD"] = str(stub)
    env["KSTRL_AGENT_PROBE"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    env.update(extra)
    return env


def _agent_calls(tmp_path: Path) -> int:
    calls = tmp_path / "agent.calls"
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


def _ks(root: Path, env: dict[str, str], command: str) -> subprocess.CompletedProcess[str]:
    """The real CLI, bounded; the group is killed if it outlives the fuse."""
    if command == "run":
        args = ["run", "1", "--root", str(root), "--ui", "plain", "--no-verify", "--branch", ""]
    else:
        args = [
            "factory",
            "--manifest",
            str(root / "manifest.json"),
            "--root",
            str(root),
            "--ui",
            "plain",
            "--no-verify",
            "--no-prs",
            "--max-retries",
            "0",
            "--max-parallel",
            "1",
            "--contract-check",
            "skip",
        ]
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
        pytest.fail(f"`ks {command}` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    return subprocess.CompletedProcess(child.args, child.returncode, out, "")


def _toml_line(field: ClosedField, value: str) -> str:
    literal = json.dumps([value] if field.is_list else value)
    return f"[{field.section}]\n{field.key} = {literal}\n"


_CASES = [
    (command, key, door)
    for command in ("run", "factory")
    for key, field in CLOSED.items()
    for door in ("toml", "env")
    if door == "toml" or field.env is not None
]


@pytest.mark.parametrize(
    ("command", "key", "door"),
    _CASES,
    ids=[f"{c}-{k[0]}.{k[1]}-{d}" for c, k, d in _CASES],
)
def test_a_bad_closed_value_exits_2_before_any_agent_call(
    tmp_path: Path, command: str, key: tuple[str, str], door: str
) -> None:
    field = CLOSED[key]
    if door == "toml":
        root = _project(tmp_path, _toml_line(field, BAD_VALUE))
        env = _env(tmp_path, {})
        names = field.key
    else:
        assert field.env is not None
        root = _project(tmp_path, "")
        env = _env(tmp_path, {field.env: BAD_VALUE})
        names = field.env

    result = _ks(root, env, command)

    assert result.returncode == 2, result.stdout
    assert REFUSAL in result.stdout, result.stdout
    assert names in result.stdout, result.stdout
    assert repr(BAD_VALUE) in result.stdout, result.stdout
    for value in field.accepted:
        assert value in result.stdout, (value, result.stdout)
    assert "Traceback" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) == 0


#: The three fields #562 moved to load time, each with an accepted value.
_GOOD_TOML = (
    '[agent]\ntype = "auto"\n'
    '[factory]\nreview_mode = "advisory"\n'
    '[security]\nagent_type = "codex"\n'
)


@pytest.mark.parametrize("command", ["run", "factory"])
def test_accepted_values_still_reach_the_engineer(tmp_path: Path, command: str) -> None:
    """Control: the stub counts, and a good config is not refused."""
    root = _project(tmp_path, _GOOD_TOML)

    result = _ks(root, _env(tmp_path, {}), command)

    assert REFUSAL not in result.stdout, result.stdout
    assert "Traceback" not in result.stdout, result.stdout
    assert _agent_calls(tmp_path) >= 1, result.stdout
