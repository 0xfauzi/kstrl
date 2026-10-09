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
the liveness probe from calling it. Where the codex CLI is installed, one
test's stub runs the engineer's git commands under the real ``codex
sandbox`` with the arguments kstrl gave it.
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


def _writable_roots(argv: list[str]) -> list[Path]:
    """The resolved ``writable_roots`` of one ``codex exec`` argv."""
    (arg,) = [a for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")]
    return [Path(p).resolve() for p in json.loads(arg.split("=", 1)[1])]


def _events(root: Path) -> str:
    """Every events.jsonl the runs under ``root`` wrote, joined."""
    paths = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    assert paths, "no run wrote an events.jsonl"
    return "".join(path.read_text(encoding="utf-8") for path in paths)


def _env(
    tmp_path: Path, extra: dict[str, str], clis: tuple[str, ...], codex_out: Path | None = None
) -> dict[str, str]:
    """No kstrl knob from the caller, no real agent CLI, a stub of each of
    ``clis`` first. With ``codex_out``, the ``codex`` stub is
    :func:`_sandboxed_codex`, which writes its output there."""
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
        if cli == "codex" and codex_out is not None:
            _sandboxed_codex(bindir / cli, codex_out)
        else:
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
    codex_out: Path | None = None,
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
        env=_env(tmp_path, {k: v.format(custom=custom) for k, v in extra.items()}, clis, codex_out),
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
    --sandbox. Now it runs under workspace-write with network denied. Its
    writable roots are pinned by the tests at the end of this file."""
    out = _ks(tmp_path, CODEX_TOML, _FACTORY, {})

    argv = _engineer_argv(tmp_path)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write", argv
    assert "sandbox_workspace_write.network_access=false" in argv, argv
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


def test_an_unconfirmed_stack_s_writable_paths_never_reach_the_engineer_sandbox(
    tmp_path: Path,
) -> None:
    """The engineer can edit kstrl.toml, so a ``[stack]`` that no person
    confirmed grants its sandbox nothing. ``ks feature`` with no
    verification still starts the codex engineer under such a stack; its
    writable roots are git paths of the checkout, never ``tool-cache``."""
    out = _ks(tmp_path, CODEX_TOML + STACK_TOML, _FEATURE, {})

    roots = _writable_roots(_engineer_argv(tmp_path))
    git = (tmp_path / "proj" / ".git").resolve()
    assert roots, out
    assert all(git in root.parents for root in roots), (roots, out)


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


def _covers(roots: list[Path], path: Path) -> bool:
    """True when a codex writable root grants a write to ``path``: a root is a
    path prefix (measured with codex-cli 0.156.1)."""
    return any(path == root or root in path.parents for root in roots)


#: (id, argv, True when the engineer works in a kstrl worktree).
_LAYOUTS = [("run", _RUN, False), ("feature", _FEATURE, False), ("factory", _FACTORY, True)]


@pytest.mark.parametrize(
    ("argv", "worktree"),
    [case[1:] for case in _LAYOUTS],
    ids=[case[0] for case in _LAYOUTS],
)
def test_a_codex_engineer_may_write_what_a_commit_writes_and_no_hook_or_config(
    tmp_path: Path, argv: tuple[str, ...], worktree: bool
) -> None:
    """RED before this change: in a plain checkout the one writable root was
    the whole git dir, and in a kstrl worktree it was the worktree's git dir.
    A sandboxed command could then write a hook, the config, or a
    ``commondir`` that points git at a config of its own, and git obeys each
    one outside every sandbox (measured). Now the roots grant the paths a
    commit writes, its lock files too, and none of those."""
    out = _ks(tmp_path, CODEX_TOML, argv, {})

    engineer = _engineer_argv(tmp_path)
    roots = _writable_roots(engineer)
    common = (tmp_path / "proj" / ".git").resolve()
    cwd = Path(engineer[engineer.index("-C") + 1])
    git_dir = common / "worktrees" / cwd.name if worktree else common
    commit_writes = [
        git_dir / "index.lock",
        git_dir / "HEAD.lock",
        git_dir / "COMMIT_EDITMSG",
        git_dir / "logs" / "HEAD",
        common / "objects" / "ab" / "cdef",
        common / "refs" / "heads" / "main.lock",
        common / "logs" / "refs" / "heads" / "main",
        # A commit deletes AUTO_MERGE, and a ref deletion rewrites
        # packed-refs, each through its lock (measured with codex-cli 0.156.1).
        git_dir / "AUTO_MERGE.lock",
        common / "packed-refs.lock",
    ]
    forbidden = [
        common / "hooks" / "pre-commit",
        common / "config",
        common / "info" / "attributes",
        git_dir / "commondir",
        git_dir / "config.worktree",
        git_dir / "gitdir",
    ]
    assert [p for p in commit_writes if not _covers(roots, p)] == [], (roots, out)
    assert [p for p in forbidden if _covers(roots, p)] == [], (roots, out)


#: The real codex CLI, found before ``_env`` takes it off the PATH.
REAL_CODEX = shutil.which("codex")

#: What the sandboxed engineer does: a commit, then a write to a hook, the
#: config and the ``commondir`` of its git dir. The ``commondir`` write keeps
#: the content git reads, so a write that succeeds does not break the repo.
_PLANT = (
    "printf 'stub\\n' >> stub-engineer.txt && git add stub-engineer.txt"
    " && git commit -qm stub-engineer; echo commit=$?\n"
    'touch "$(git rev-parse --path-format=absolute --git-common-dir)/hooks/planted"; echo hook=$?\n'
    "git config --local kstrl.planted 1; echo config=$?\n"
    'gd="$(git rev-parse --path-format=absolute --git-dir)"\n'
    'c="$(cat "$gd/commondir" 2>/dev/null || echo .)"\n'
    'printf \'%s\\n\' "$c" > "$gd/commondir"; echo commondir=$?\n'
)


def _sandboxed_codex(path: Path, out: Path) -> Path:
    """A ``codex`` that records its argv like :func:`_stub` and, as the
    engineer, runs :data:`_PLANT` in its ``-C`` directory under the real
    ``codex sandbox`` with the engineer's ``--sandbox`` and ``-c`` arguments.

    codex also lets a command write ``$TMPDIR``, and pytest's ``tmp_path`` is
    under it, so the sandbox gets a ``$TMPDIR`` that holds no project."""
    argv = path.with_suffix(".argv")
    scratch = out.parent / "sandbox-tmp"
    return write_executable(
        path,
        f"""#!{sys.executable}
import os, subprocess, sys
argv = sys.argv[1:]
with open({str(argv)!r}, "a", encoding="utf-8") as log:
    log.write("".join(a + "\\n" for a in [*argv, "--"]))
sys.stdin.read()
if "--help" not in argv and "read-only" not in argv:
    mode = argv[argv.index("--sandbox") + 1]
    cmd = [{REAL_CODEX!r}, "sandbox", "-c", 'sandbox_mode="' + mode + '"']
    for i, a in enumerate(argv[:-1]):
        if a == "-c":
            cmd += ["-c", argv[i + 1]]
    os.makedirs({str(scratch)!r}, exist_ok=True)
    done = subprocess.run(
        [*cmd, "--", "sh", "-c", {_PLANT!r}],
        cwd=argv[argv.index("-C") + 1],
        env={{**os.environ, "TMPDIR": {str(scratch)!r}}},
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    with open({str(out)!r}, "a", encoding="utf-8") as log:
        log.write(done.stdout + done.stderr)
print("<promise>COMPLETE</promise>")
""",
    )


@pytest.mark.skipif(REAL_CODEX is None, reason="the codex CLI is not installed")
@pytest.mark.parametrize(
    ("argv", "worktree"),
    [case[1:] for case in _LAYOUTS if case[0] != "feature"],
    ids=[case[0] for case in _LAYOUTS if case[0] != "feature"],
)
def test_under_the_real_codex_sandbox_the_engineer_commits_and_a_planted_hook_write_fails(
    tmp_path: Path, argv: tuple[str, ...], worktree: bool
) -> None:
    """RED before this change: the planted hook, config and ``commondir``
    writes succeeded (``hook=0``). The real codex sandbox enforces the
    roots kstrl gives the engineer: the commit succeeds, every planted
    write fails, and nothing planted is on the disk after the run."""
    out = tmp_path / "engineer.out"
    ran = _ks(tmp_path, CODEX_TOML, argv, {}, codex_out=out)

    text = out.read_text(encoding="utf-8") if out.exists() else ""
    common = tmp_path / "proj" / ".git"
    assert "commit=0" in text, (text, ran)
    # A commit exits 0 when it cannot remove AUTO_MERGE, but says so.
    assert "cannot lock ref" not in text, (text, ran)
    assert "hook=0" not in text and "hook=" in text, (text, ran)
    assert "config=0" not in text and "config=" in text, (text, ran)
    assert "commondir=0" not in text and "commondir=" in text, (text, ran)
    assert not (common / "hooks" / "planted").exists(), text
    assert "kstrl.planted" not in (common / "config").read_text(encoding="utf-8"), text
    assert not (common / "commondir").exists(), text
    log = subprocess.run(
        ["git", "log", "--all", "--format=%s"],
        cwd=tmp_path / "proj",
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    ).stdout
    assert "stub-engineer" in log, (log, text, ran)
