"""A role the sandbox cannot reach is refused before any agent call (#701).

End to end: the real ``ks`` CLI in its own process group, on a real git
repository, with stub agents that append one line to a file per call.
A custom agent command has no sandbox surface. Before #701 every command
that could start one under ``[sandbox] enabled = true`` printed a warning
and ran it anyway (the engineer, ``ks run``, ``ks understand``,
``ks feature``), or said nothing (the ``ks feature`` repair agent). Each
case here asserts exit 2, the refusal headline, the role it names, and
zero agent calls.

The sandboxable engineer is a stub executable named ``codex`` on a PATH
holding no real ``claude`` or ``codex``. ``KSTRL_AGENT_PROBE=0`` keeps
the liveness probe from calling it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import gitrepo
from tests.helpers.executables import write_executable
from tests.helpers.procs import kill_group

#: A hang fails loudly instead of reading as a pass.
FUSE_SECONDS = 120.0

#: Spelled out rather than imported, so the operator-visible line is pinned
#: and the test collects (and fails on behaviour) against a tree without it.
REFUSAL = (
    "Refusing to run: [sandbox] is enabled and kstrl cannot apply it to a role this run would start"
)
SANDBOX_TOML = "[sandbox]\nenabled = true\n"
CODEX_TOML = '[agent]\ntype = "codex"\n'
AGENT_CLIS = ("claude", "codex")

_STORY = {
    "id": "US-001",
    "title": "t",
    "acceptanceCriteria": ["a"],
    "priority": 1,
    "passes": False,
    "notes": "",
}

_FACTORY = (
    "factory",
    "--manifest",
    "{root}/manifest.json",
    "--root",
    "{root}",
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
)
_RUN = ("run", "1", "--root", "{root}", "--ui", "plain", "--no-verify", "--branch", "")
_UNDERSTAND = ("understand", "1", "--root", "{root}", "--ui", "plain", "--no-tui")
_FEATURE = (
    "feature",
    "--root",
    "{root}",
    "--prd",
    "{root}/scripts/kstrl/prd.json",
    "--ui",
    "plain",
    "--no-tui",
    "--no-verify",
    "--implementation-auto-run",
    "--understand-iterations",
    "1",
)


def _project(tmp_path: Path, toml: str) -> Path:
    """A committed repo every command here accepts, with ``toml`` as kstrl.toml."""
    root = tmp_path / "proj"
    kdir = root / "scripts" / "kstrl"
    comp = kdir / "feature" / "comp-a"
    comp.mkdir(parents=True)
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    for name in ("prompt.md", "understand_prompt.md", "feature_understand_prompt.md"):
        (kdir / name).write_text("do the thing\n", encoding="utf-8")
    (kdir / "codebase_map.md").write_text("map\n", encoding="utf-8")
    (kdir / "prd.json").write_text(
        json.dumps({"branchName": "kstrl/test", "userStories": [_STORY]}), encoding="utf-8"
    )
    (comp / "prd.json").write_text(
        json.dumps({"branchName": "kstrl/factory/comp-a", "userStories": [_STORY]}),
        encoding="utf-8",
    )
    manifest = {
        "version": "1",
        "specFile": "",
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


def _stub(path: Path) -> Path:
    """An agent that records each call in ``<path>.calls`` and completes."""
    calls = path.with_suffix(".calls")
    return write_executable(
        path,
        f"#!/bin/sh\necho call >> '{calls}'\ncat >/dev/null\necho '<promise>COMPLETE</promise>'\n",
    )


def _calls(tmp_path: Path, name: str) -> int:
    path = tmp_path / f"{name}.calls"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def _env(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    """No kstrl knob from the caller, no real agent CLI, a stub ``codex`` first."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KSTRL_", "FACTORY_")) and k not in ("AGENT_CMD", "MODEL")
    }
    kept = [
        d
        for d in env.get("PATH", "").split(os.pathsep)
        if d and not any(os.access(os.path.join(d, n), os.X_OK) for n in AGENT_CLIS)
    ]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "codex")
    env["PATH"] = os.pathsep.join([str(bindir), *kept])
    assert shutil.which("git", path=env["PATH"]), env["PATH"]
    env["KSTRL_AGENT_PROBE"] = "0"
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    env.update(extra)
    return env


def _ks(tmp_path: Path, toml: str, argv: tuple[str, ...], extra: dict[str, str]) -> str:
    """Run ``ks`` bounded by the fuse; returns combined output, asserts no traceback."""
    root = _project(tmp_path, toml)
    custom = _stub(tmp_path / "custom.sh")
    args = [a.format(root=root, custom=custom) for a in argv]
    child = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        env=_env(tmp_path, {k: v.format(custom=custom) for k, v in extra.items()}),
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
        pytest.fail(f"`ks {argv[0]}` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    assert "Traceback" not in out, out
    return f"exit={child.returncode}\n{out}"


#: (id, kstrl.toml, argv, extra env, the role the refusal names).
_REFUSED = [
    ("factory-engineer", SANDBOX_TOML, _FACTORY, {"AGENT_CMD": "{custom}"}, "engineer"),
    ("run-engineer", SANDBOX_TOML, _RUN, {"AGENT_CMD": "{custom}"}, "engineer"),
    (
        "factory-code-reviewer",
        SANDBOX_TOML + CODEX_TOML,
        (*_FACTORY, "--review-mode", "hard", "--review-agent-cmd", "{custom}"),
        {},
        "code reviewer",
    ),
    (
        "factory-security-reviewer",
        SANDBOX_TOML + CODEX_TOML,
        (
            *_FACTORY,
            "--review-mode",
            "skip",
            "--security-mode",
            "hard",
            "--security-agent-cmd",
            "{custom}",
        ),
        {},
        "security reviewer",
    ),
    (
        # #700 slice 7: the designer is the [review] selection, started for
        # --design-acceptance even with review off.
        "factory-verification-designer",
        SANDBOX_TOML + CODEX_TOML,
        (
            *_FACTORY,
            "--review-mode",
            "skip",
            "--review-agent-cmd",
            "{custom}",
            "--design-acceptance",
        ),
        {},
        "verification designer",
    ),
    (
        "understand-agent",
        "",
        _UNDERSTAND,
        {"AGENT_CMD": "{custom}", "KSTRL_SANDBOX_ENABLED": "1"},
        "understand agent",
    ),
    (
        "feature-engineer",
        "",
        _FEATURE,
        {"AGENT_CMD": "{custom}", "KSTRL_SANDBOX_ENABLED": "1"},
        "engineer",
    ),
    (
        "feature-repair-agent",
        CODEX_TOML,
        (*_FEATURE, "--repair-agent-cmd", "{custom}"),
        {"KSTRL_SANDBOX_ENABLED": "1"},
        "repair agent",
    ),
    (
        # The operator typed the repair agent; it is refused even when no
        # repair run is allowed (#701 plan decision 3).
        "feature-repair-agent-no-repair-runs",
        CODEX_TOML,
        (*_FEATURE, "--repair-max-runs", "0", "--repair-agent-cmd", "{custom}"),
        {"KSTRL_SANDBOX_ENABLED": "1"},
        "repair agent",
    ),
]


@pytest.mark.parametrize(
    ("toml", "argv", "extra", "role"),
    [case[1:] for case in _REFUSED],
    ids=[case[0] for case in _REFUSED],
)
def test_a_role_the_sandbox_cannot_reach_exits_2_before_any_agent_call(
    tmp_path: Path, toml: str, argv: tuple[str, ...], extra: dict[str, str], role: str
) -> None:
    """RED before #701: a warning (or nothing), then the agent runs."""
    out = _ks(tmp_path, toml, argv, extra)

    assert out.startswith("exit=2\n"), out
    assert REFUSAL in out, out
    assert f"the {role} is a custom agent command" in out, out
    # The command is never printed: it can carry a credential.
    assert "custom.sh" not in out, out
    assert _calls(tmp_path, "custom") == 0, out
    assert _calls(tmp_path, "bin/codex") == 0, out
    if argv[0] in ("factory", "run"):
        # Like every other pre-spend refusal of a factory run, this one comes
        # after the launch record that `ks retry` reads.
        assert list((tmp_path / "proj" / ".kstrl" / "runs").glob("*/launch.json")), out


def test_without_the_sandbox_a_custom_engineer_still_runs(tmp_path: Path) -> None:
    """Control: with the sandbox off nothing is refused and the stub is called."""
    out = _ks(tmp_path, "", _FACTORY, {"AGENT_CMD": "{custom}"})

    assert REFUSAL not in out, out
    assert _calls(tmp_path, "custom") >= 1, out


def test_a_custom_reviewer_for_a_phase_that_never_runs_is_not_refused(tmp_path: Path) -> None:
    """Control: review is off, so its custom command starts no role, and the
    sandboxable engineer runs."""
    out = _ks(
        tmp_path,
        SANDBOX_TOML + CODEX_TOML,
        (*_FACTORY, "--review-mode", "skip", "--review-agent-cmd", "{custom}"),
        {},
    )

    assert REFUSAL not in out, out
    assert _calls(tmp_path, "custom") == 0, out
    assert _calls(tmp_path, "bin/codex") >= 1, out


def test_a_custom_security_reviewer_for_a_phase_that_never_runs_is_not_refused(
    tmp_path: Path,
) -> None:
    """Control: security is off, so its custom command starts no role, and the
    sandboxable engineer runs."""
    out = _ks(
        tmp_path,
        SANDBOX_TOML + CODEX_TOML,
        (*_FACTORY, "--security-mode", "skip", "--security-agent-cmd", "{custom}"),
        {},
    )

    assert REFUSAL not in out, out
    assert _calls(tmp_path, "custom") == 0, out
    assert _calls(tmp_path, "bin/codex") >= 1, out
