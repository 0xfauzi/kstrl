"""The engineer runs in the sandbox of its own harness (#700, owner decision
2026-10-09).

End to end: the real ``ks`` CLI in its own process group, on a real git
repository, with stub agents that append one line to a file per call.

``[sandbox] enabled`` is true by default, so a codex engineer with no
``[sandbox]`` section starts under ``--sandbox workspace-write``, with
network denied and the git paths a commit writes as its writable roots
(measured: codex holds every ``.git`` read-only, so a commit fails without
them). A custom agent command has no sandbox surface, and neither has a role
under ``enabled = false``. Between #701 and #700 such a run was refused with
exit 2; now it runs, and a warning names the role on the terminal and in the
run's events.jsonl. Nothing is refused.

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
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_isolation_stack import NO_PROVER

#: A hang fails loudly instead of reading as a pass.
FUSE_SECONDS = 120.0

#: The #701 refusal, which no run may print any more.
REFUSAL = "Refusing to run: [sandbox]"
SANDBOX_TOML = "[sandbox]\nenabled = true\n"
SANDBOX_OFF_TOML = "[sandbox]\nenabled = false\n"
CODEX_TOML = '[agent]\ntype = "codex"\n'
#: Spelled out rather than imported, so the operator-visible lines are pinned
#: and the tests collect (and fail on behaviour) against a tree without them.
CUSTOM = "the {role} is a custom agent command, which runs with no sandbox"
CUSTOM_REVIEWER = "the {role} is a custom command; it CANNOT be sandboxed or held read-only"
SWITCHED_OFF = "the {role} runs with no sandbox: [sandbox] enabled = false"
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
    """An agent that records each call's argv in ``<path>.argv``, one argument
    a line and a ``--`` line after it, and completes."""
    argv = path.with_suffix(".argv")
    return write_executable(
        path,
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" -- >> '{argv}'\n"
        "cat >/dev/null\necho '<promise>COMPLETE</promise>'\n",
    )


def _runs(tmp_path: Path, name: str) -> list[list[str]]:
    """The argv of each call of the stub ``name`` that ran a prompt: not the
    ``codex exec --help`` probe the codex adapter makes first."""
    path = (tmp_path / name).with_suffix(".argv")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    calls = [call.splitlines() for call in text.split("--\n")[:-1]]
    return [call for call in calls if "--help" not in call]


def _engineer_argv(tmp_path: Path) -> list[str]:
    """The argv of the engineer's first ``codex exec`` call: not a reviewer,
    which runs ``--sandbox read-only`` (#266). The stub makes no progress, so
    the engineer is called until the no-progress breaker stops it."""
    runs = [call for call in _runs(tmp_path, "bin/codex") if "read-only" not in call]
    assert runs, "the engineer never ran"
    return runs[0]


def _events(root: Path) -> str:
    """Every events.jsonl the runs under ``root`` wrote, joined."""
    paths = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    assert paths, "no run wrote an events.jsonl"
    return "".join(path.read_text(encoding="utf-8") for path in paths)


def _env(tmp_path: Path, extra: dict[str, str], clis: tuple[str, ...]) -> dict[str, str]:
    """No kstrl knob from the caller, no real agent CLI, a stub of each of
    ``clis`` first."""
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
    for cli in clis:
        _stub(bindir / cli)
    env["PATH"] = os.pathsep.join([str(bindir), *kept])
    assert shutil.which("git", path=env["PATH"]), env["PATH"]
    env["KSTRL_AGENT_PROBE"] = "0"
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    env.update(extra)
    return env


def _ks(
    tmp_path: Path,
    toml: str,
    argv: tuple[str, ...],
    extra: dict[str, str],
    *,
    confirm: bool = False,
    clis: tuple[str, ...] = ("codex",),
) -> str:
    """Run ``ks`` bounded by the fuse; returns combined output, asserts no
    traceback. ``confirm`` confirms the ``[stack]`` in ``toml`` first."""
    root = _project(tmp_path, toml)
    if confirm:
        confirm_stack(root)
    custom = _stub(tmp_path / "custom.sh")
    args = [a.format(root=root, custom=custom) for a in argv]
    child = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        env=_env(tmp_path, {k: v.format(custom=custom) for k, v in extra.items()}, clis),
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


#: (id, kstrl.toml, argv, extra env, the warning that names the role, the stub
#: the run must have called: the custom command, or the stub codex engineer).
_UNCONFINED = [
    (
        "factory-engineer",
        SANDBOX_TOML,
        _FACTORY,
        {"AGENT_CMD": "{custom}"},
        CUSTOM.format(role="engineer"),
        "custom",
    ),
    (
        "run-engineer",
        SANDBOX_TOML,
        _RUN,
        {"AGENT_CMD": "{custom}"},
        CUSTOM.format(role="engineer"),
        "custom",
    ),
    (
        "factory-code-reviewer",
        SANDBOX_TOML + CODEX_TOML,
        (*_FACTORY, "--review-mode", "hard", "--review-agent-cmd", "{custom}"),
        {},
        CUSTOM_REVIEWER.format(role="review reviewer"),
        "bin/codex",
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
        CUSTOM_REVIEWER.format(role="security reviewer"),
        "bin/codex",
    ),
    (
        "understand-agent",
        "",
        _UNDERSTAND,
        {"AGENT_CMD": "{custom}"},
        CUSTOM.format(role="understand agent"),
        "custom",
    ),
    (
        "feature-engineer",
        "",
        _FEATURE,
        {"AGENT_CMD": "{custom}"},
        CUSTOM.format(role="engineer"),
        "custom",
    ),
    (
        "feature-repair-agent",
        CODEX_TOML,
        (*_FEATURE, "--repair-agent-cmd", "{custom}"),
        {},
        CUSTOM.format(role="repair agent"),
        "bin/codex",
    ),
    (
        "factory-sandbox-off",
        SANDBOX_OFF_TOML + CODEX_TOML,
        _FACTORY,
        {},
        SWITCHED_OFF.format(role="engineer"),
        "bin/codex",
    ),
    (
        "understand-sandbox-off",
        CODEX_TOML,
        _UNDERSTAND,
        {"KSTRL_SANDBOX_ENABLED": "0"},
        SWITCHED_OFF.format(role="understand agent"),
        "bin/codex",
    ),
    (
        "feature-sandbox-off",
        SANDBOX_OFF_TOML + CODEX_TOML,
        _FEATURE,
        {},
        SWITCHED_OFF.format(role="engineer"),
        "bin/codex",
    ),
]


@pytest.mark.parametrize(
    ("toml", "argv", "extra", "warning", "called"),
    [case[1:] for case in _UNCONFINED],
    ids=[case[0] for case in _UNCONFINED],
)
def test_a_role_with_no_sandbox_runs_and_the_run_records_a_warning_naming_it(
    tmp_path: Path,
    toml: str,
    argv: tuple[str, ...],
    extra: dict[str, str],
    warning: str,
    called: str,
) -> None:
    """RED before #700: exit 2, the #701 refusal, and no agent call."""
    out = _ks(tmp_path, toml, argv, extra)

    assert not out.startswith("exit=2\n"), out
    assert REFUSAL not in out, out
    assert warning in out, out
    assert warning in _events(tmp_path / "proj"), out
    # The warning never prints the command: it can carry a credential.
    assert all("custom.sh" not in line for line in out.splitlines() if warning in line), out
    assert _runs(tmp_path, called), out


def test_with_no_sandbox_section_a_codex_engineer_runs_in_its_sandbox(tmp_path: Path) -> None:
    """RED before #700: the default was off, so the engineer's argv had no
    --sandbox. Now it runs under workspace-write with network denied, and its
    writable roots are the worktree's git dir and the common dir's objects,
    refs and logs: never the common dir itself, whose hooks and config a
    sandboxed command must not write."""
    out = _ks(tmp_path, CODEX_TOML, _FACTORY, {})

    argv = _engineer_argv(tmp_path)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write", argv
    assert "sandbox_workspace_write.network_access=false" in argv, argv
    (roots_arg,) = [a for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")]
    roots = [Path(p) for p in json.loads(roots_arg.split("=", 1)[1])]
    common = (tmp_path / "proj" / ".git").resolve()
    assert roots[0].parent == common / "worktrees", roots
    assert roots[1:] == [common / "objects", common / "refs", common / "logs"], roots
    assert "runs with no sandbox" not in out, out
    assert REFUSAL not in out, out


#: A confirmed [stack] whose commands may write one path outside the worktree.
STACK_TOML = (
    '[stack]\ninstructions = "Built by a kstrl test."\nsetup = ""\nenv = []\n'
    'writable = ["tool-cache"]\n[stack.checks]\ntests = "true"\n'
)


def test_a_confirmed_stack_s_writable_paths_reach_the_engineer_sandbox(tmp_path: Path) -> None:
    """RED before #700: the engineer's argv had no --sandbox. The engineer runs
    the stack's checks, so its sandbox may write what the test zone may
    (measured: ``uv run`` fails on its cache without it). The path is the
    stack's relative entry under the project root, after the git paths."""
    out = _ks(tmp_path, CODEX_TOML + STACK_TOML, _FACTORY, NO_PROVER, confirm=True)
    root = tmp_path / "proj"

    argv = _engineer_argv(tmp_path)
    (roots_arg,) = [a for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")]
    roots = [Path(p).resolve() for p in json.loads(roots_arg.split("=", 1)[1])]
    assert roots[-1] == (root / "tool-cache").resolve(), (roots, out)


def test_a_codex_engineer_in_a_plain_checkout_may_write_its_git_dir(tmp_path: Path) -> None:
    """RED before #700: the engineer's argv had no --sandbox. ``ks run`` works
    in the checkout itself, whose git dir holds the index lock, so the one
    writable root is the whole git dir (measured: codex cannot commit there
    with only objects, refs and logs)."""
    out = _ks(tmp_path, CODEX_TOML, _RUN, {})

    argv = _engineer_argv(tmp_path)
    (roots_arg,) = [a for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")]
    roots = [Path(p).resolve() for p in json.loads(roots_arg.split("=", 1)[1])]
    assert roots == [(tmp_path / "proj" / ".git").resolve()], (roots, out)


CLAUDE_TOML = '[agent]\ntype = "claude-code"\n'


def test_a_claude_engineer_may_write_the_stack_s_paths_and_no_git_path(tmp_path: Path) -> None:
    """RED before #700: the engineer's argv had no --settings. claude commits
    with no extra path and keeps hooks and config read-only (measured), so
    its sandbox gets the confirmed stack's writable paths and nothing else,
    with outbound network denied."""
    argv_ = (*_FACTORY, "--review-mode", "skip", "--security-mode", "skip")
    out = _ks(tmp_path, CLAUDE_TOML + STACK_TOML, argv_, NO_PROVER, confirm=True, clis=("claude",))

    runs = _runs(tmp_path, "bin/claude")
    assert runs, out
    argv = runs[0]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] is True, settings
    allowed = [Path(p).resolve() for p in settings["sandbox"]["filesystem"]["allowWrite"]]
    assert allowed == [(tmp_path / "proj" / "tool-cache").resolve()], (settings, out)
    assert "--dangerously-skip-permissions" not in argv, argv
