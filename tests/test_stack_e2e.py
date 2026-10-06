"""#696 slice 2: an opt-in ``[stack]`` and the one runner that judges its checks.

A ``[stack]`` table in kstrl.toml holds instructions for the models and the
commands kstrl runs without knowing what they are. A check passes when it
exits 0 and fails on any other exit (decision 4): 126, 127, a timeout and
undecodable output fail UNMEASURED, every other exit is a measured failure.
With a stack the base branch refuses on any check that did not pass, on a
failed setup and on anything ``git status`` shows after the checks; the
checks see only the neutral environment plus the names the stack declares
(decision 11); Phase 3 runs every check (decision 10); and the stack is the
only source of verification commands.

End to end: a real git repository under ``tmp_path`` after the real
``ks init``, and the real CLI (``ks factory``, ``ks doctor --measure``) as a
subprocess in its own process group under a fuse, with a stub engineer that
records each call and the prompt it was given. Confirmation of a stack is
slice 3; here a ``[stack]`` is used as it is loaded.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.procs import kill_group
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_isolation_rung import NONO, runs_a_stack

#: Real time for one CLI run; a hang fails loudly instead of waiting.
FUSE_SECONDS = 180.0

PY = sys.executable

#: What `cargo test` prints and returns for a failing test: no kstrl parser
#: reads it, and its exit status is 101.
CARGO_RED = (
    "printf 'running 1 test\\ntest it_works ... FAILED\\n"
    "error: test failed, to rerun pass --lib\\n'; exit 101"
)

INSTRUCTIONS = "Rust 1.80 with cargo. Keep each module's tests beside it."

HEADLINE = "Refusing to run: the base branch fails a gate Phase 1 runs"

PRD_PATH = "scripts/kstrl/feature/{comp}/prd.json"


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    calls: int
    prompts: str


def _stack(
    checks: dict[str, str],
    *,
    setup: str = "",
    env: list[str] | None = None,
    instructions: str = INSTRUCTIONS,
    rung: dict[str, Any] | None = None,
) -> str:
    """A ``[stack]`` table. JSON strings are TOML basic strings, and a JSON
    list of strings or a JSON boolean is the same TOML value. ``rung`` holds
    the #700 keys (writable, readable, browser)."""
    lines = [
        "[stack]",
        f"instructions = {json.dumps(instructions)}",
        f"setup = {json.dumps(setup)}",
        f"env = {json.dumps(env or [])}",
        *(f"{key} = {json.dumps(value)}" for key, value in (rung or {}).items()),
        "[stack.checks]",
        *(f"{json.dumps(name)} = {json.dumps(command)}" for name, command in checks.items()),
    ]
    return "\n".join(lines) + "\n"


def _child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment without kstrl's own settings, plus ``env``."""
    child_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KSTRL_", "FACTORY_")) and k not in ("AGENT_CMD", "MODEL")
    }
    child_env.update(KSTRL_AGENT_PROBE="0", KSTRL_NO_TUI="1", KSTRL_KNOWLEDGE_ENABLED="0")
    if NONO:
        # #700 slice 2: `ks factory` under a [stack] runs every command in a
        # rung proven through this nono, and refuses without one.
        child_env["KSTRL_NONO"] = NONO
    child_env.update(env or {})
    return child_env


def _commit(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git_in(root, "add", name)
    git_in(root, "commit", "-q", "-m", f"change {name}")


def _repo(
    tmp_path: Path, stack: str, *, comps: tuple[str, ...] = ("greeter",), confirm: bool = True
) -> Path:
    """A repository on ``main`` after the real ``ks init``, with ``stack``
    appended to its kstrl.toml (and confirmed, unless ``confirm`` is False)
    and one planned component per ``comps``, everything committed. Cargo.toml
    is there because `ks doctor` wants a build manifest; nothing here builds it."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    story = {
        "id": "US-001",
        "title": "Hello",
        "acceptanceCriteria": ["prints hello"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    seeded = {"Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n'}
    for comp in comps:
        seeded[PRD_PATH.format(comp=comp)] = json.dumps(
            {"branchName": f"kstrl/factory/{comp}", "userStories": [story]}
        )
    for name, text in seeded.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root), "--ui", "plain"])
    assert result.exit_code == 0, result.output
    with (root / "kstrl.toml").open("a", encoding="utf-8") as fh:
        fh.write("\n" + stack)
    if confirm:
        confirm_stack(root)
    manifest = {
        "version": "1",
        "specFile": "",
        "projectName": "demo",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": comp,
                "title": comp,
                "description": "",
                "dependencies": [],
                "prdPath": PRD_PATH.format(comp=comp),
                "branchName": f"kstrl/factory/{comp}",
            }
            for comp in comps
        ],
    }
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "seed")
    return root


def _spawn(args: list[str], root: Path, env: dict[str, str] | None) -> tuple[int, str]:
    """The real CLI in its own process group, killed on the fuse."""
    child = subprocess.Popen(
        [PY, "-m", "kstrl", *args],
        cwd=root,
        env=_child_env(env),
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
    return child.returncode, out


def _factory(
    tmp_path: Path,
    root: Path,
    *extra: str,
    env: dict[str, str] | None = None,
    contract: str = "skip",
    engineer: str = "",
) -> Run:
    """The real `ks factory --manifest`. The stub engineer appends one line
    per call to a log and the prompt it was given to another, runs
    ``engineer`` in its worktree, and says COMPLETE."""
    calls = tmp_path / "engineer.calls"
    prompts = tmp_path / "engineer.prompts"
    stub = write_executable(
        tmp_path / "engineer.sh",
        f"#!/bin/sh\necho call >> '{calls}'\ncat >> '{prompts}'\n{engineer}\n"
        "echo '<promise>COMPLETE</promise>'\n",
    )
    code, out = _spawn(
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(stub)),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--security-mode", "skip"),
            *("--contract-check", contract),
            *extra,
        ],
        root,
        env,
    )
    count = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
    said = prompts.read_text(encoding="utf-8") if prompts.exists() else ""
    return Run(code, out, count, said)


def _measure(root: Path, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """The real `ks doctor --measure --json`: its exit code and document."""
    code, out = _spawn(["doctor", "--root", str(root), "--measure", "--json"], root, env)
    document: dict[str, Any] = json.loads(out[out.index("{") :])
    return code, document


def _record(root: Path) -> dict[str, Any]:
    """The one base-gates record the runs under ``root`` wrote."""
    (path,) = sorted((root / ".kstrl" / "runs").glob("*/base-gates.json"))
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _row(reading: dict[str, Any], name: str) -> dict[str, Any]:
    (row,) = [row for row in reading["checks"] if row["name"] == name]
    return row


def test_a_red_base_the_stack_checks_is_not_ready_and_a_green_one_is(tmp_path: Path) -> None:
    """1. A check exiting 101 with output no kstrl parser reads is a MEASURED
    failure, so `ks doctor --measure` is not-ready. Without a stack the same
    command is an unmeasured row and the verdict is ready-with-warnings."""
    red = _repo(tmp_path / "red", _stack({"tests": CARGO_RED, "lint": "true"}))
    green = _repo(tmp_path / "green", _stack({"tests": "true", "lint": "true"}))

    red_code, red_doc = _measure(red)
    green_code, green_doc = _measure(green)

    assert red_code == 1, red_doc
    reading = red_doc["base_gates"]
    assert reading["refused"] is True, reading
    row = _row(reading, "stack:tests")
    assert (row["passed"], row["measured"]) == (False, True), row
    assert "exited 101" in row["message"]
    assert _row(reading, "stack:lint")["passed"] is True
    assert green_code == 0, green_doc
    assert green_doc["base_gates"]["refused"] is False
    assert [r["name"] for r in green_doc["base_gates"]["checks"]] == ["stack:tests", "stack:lint"]


@runs_a_stack
def test_ks_factory_refuses_a_red_base_before_the_engineer_and_records_the_stack(
    tmp_path: Path,
) -> None:
    """2. The refusal `ks factory` takes on the same reading: exit 2, no
    engineer call, and base-gates.json names the stack it measured under,
    the digest `ks doctor --measure` reports for the same table."""
    root = _repo(tmp_path, _stack({"tests": CARGO_RED}))

    run = _factory(tmp_path, root)
    _code, document = _measure(root)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert "stack:tests fails on main" in run.out
    assert run.calls == 0, run.out
    record = _record(root)
    assert record["refused"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", record["stackDigest"]), record
    assert record["stackDigest"] == document["base_gates"]["stackDigest"]
    assert record["verifyDigest"] == document["base_gates"]["verifyDigest"]


@pytest.mark.parametrize(
    ("command", "env"),
    [
        ("no-such-tool-696 --check", {}),
        ("sleep 30", {"KSTRL_TIMEOUT_VERIFY": "2"}),
    ],
    ids=["exit-127", "timed-out"],
)
def test_a_check_that_measured_nothing_still_refuses_the_base(
    tmp_path: Path, command: str, env: dict[str, str]
) -> None:
    """3. A command the shell could not run (127) and one that timed out fail
    UNMEASURED. Without a stack the base lets such a row through with a
    warning; under a stack any check that did not pass refuses."""
    root = _repo(tmp_path, _stack({"tests": command}))

    code, document = _measure(root, env)

    assert code == 1, document
    reading = document["base_gates"]
    row = _row(reading, "stack:tests")
    assert (row["passed"], row["measured"]) == (False, False), row
    assert reading["refused"] is True, reading
    assert any("stack:tests fails on main" in reason for reason in reading["reasons"]), reading


@runs_a_stack
def test_a_failing_setup_refuses_the_base_before_the_engineer(tmp_path: Path) -> None:
    """4. Without a stack a failed worktree setup is a warning and the run
    goes on; under a stack the base refuses on it."""
    root = _repo(tmp_path, _stack({"tests": "true"}, setup="echo cannot-install >&2; exit 3"))

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert run.calls == 0, run.out
    record = _record(root)
    assert record["refused"] is True, record
    assert any("setup fails on main" in reason for reason in record["reasons"]), record
    assert "cannot-install" in record["setupError"]


def test_what_a_check_leaves_untracked_refuses_until_the_project_ignores_it(
    tmp_path: Path,
) -> None:
    """5. A check that writes out/ leaves a path git does not ignore, which
    every engineer would then carry as an out-of-scope edit. The refusal
    names the path; once .gitignore ignores out/, the same base is ready."""
    root = _repo(tmp_path, _stack({"build": "mkdir -p out && echo built > out/artifact.txt"}))

    code, document = _measure(root)
    _commit(root, ".gitignore", (root / ".gitignore").read_text(encoding="utf-8") + "out/\n")
    ignored_code, ignored = _measure(root)

    assert code == 1, document
    assert document["base_gates"]["refused"] is True
    assert any("out/artifact.txt" in reason for reason in document["base_gates"]["reasons"])
    assert document["base_gates"]["leftBehind"] == ["?? out/artifact.txt"]
    assert ignored_code == 0, ignored
    assert ignored["base_gates"]["refused"] is False
    assert ignored["base_gates"]["leftBehind"] == []


def test_a_check_sees_the_declared_names_and_a_secret_name_is_refused(tmp_path: Path) -> None:
    """6. The checks get the neutral environment plus the names the stack
    declares: an undeclared name and Python's VIRTUAL_ENV are gone. A
    declared name shaped like a secret is refused when kstrl.toml loads."""
    # Declared writable: `ks doctor --measure` also replays the recipe inside
    # the rung (#700 slice 3), where an undeclared path refuses the write.
    written = tmp_path / "seen"
    written.mkdir()
    seen = written / "env.seen"
    setup_seen = written / "setup.seen"
    root = _repo(
        tmp_path / "declared",
        _stack(
            {"env": f"env > '{seen}'"},
            setup=f"env > '{setup_seen}'",
            env=["DEMO_DECLARED"],
            rung={"writable": [str(written)]},
        ),
    )
    secret = _repo(
        tmp_path / "secret", _stack({"tests": "true"}, env=["DEMO_API_TOKEN"]), confirm=False
    )
    process_env = {
        "DEMO_DECLARED": "declared-value",
        "DEMO_UNDECLARED": "undeclared-value",
        "VIRTUAL_ENV": "/nonexistent-venv",
    }

    code, document = _measure(root, process_env)
    refused = _factory(tmp_path / "secret", secret)

    assert code == 0, document
    names = {line.split("=", 1)[0] for line in seen.read_text(encoding="utf-8").splitlines()}
    assert "DEMO_DECLARED" in names
    assert "DEMO_UNDECLARED" not in names
    assert "VIRTUAL_ENV" not in names
    setup_names = {
        line.split("=", 1)[0] for line in setup_seen.read_text(encoding="utf-8").splitlines()
    }
    assert "DEMO_DECLARED" in setup_names
    assert "VIRTUAL_ENV" not in setup_names
    assert refused.code == 2, refused.out
    assert "DEMO_API_TOKEN" in refused.out
    assert refused.calls == 0


_SOURCES = {
    "verify-toml": ("verify", 'test_command = "true"', {}, (), "[verify] test_command"),
    "verify-tool-toml": ("verify", 'lint_tool = "ruff"', {}, (), "[verify] lint_tool"),
    "factory-toml": (
        "factory",
        'worktree_setup_command = "true"',
        {},
        (),
        "[factory] worktree_setup_command",
    ),
    "contract-toml": ("contract", 'test_command = "true"', {}, (), "[contract] test_command"),
    "breaker-toml": ("breaker", 'test_command = "true"', {}, (), "[breaker] test_command"),
    "verify-env": ("", "", {"KSTRL_VERIFY_TEST_CMD": "true"}, (), "KSTRL_VERIFY_TEST_CMD"),
    "setup-env": (
        "",
        "",
        {"KSTRL_FACTORY_WORKTREE_SETUP_COMMAND": "true"},
        (),
        "KSTRL_FACTORY_WORKTREE_SETUP_COMMAND",
    ),
    "contract-env": ("", "", {"KSTRL_CONTRACT_TEST_CMD": "true"}, (), "KSTRL_CONTRACT_TEST_CMD"),
    "breaker-env": ("", "", {"KSTRL_BREAKER_TEST_CMD": "true"}, (), "KSTRL_BREAKER_TEST_CMD"),
    "test-flag": ("", "", {}, ("--test-command", "true"), "--test-command"),
    "contract-flag": ("", "", {}, ("--contract-test-cmd", "true"), "--contract-test-cmd"),
}


@pytest.mark.parametrize("source", sorted(_SOURCES))
def test_a_second_command_source_beside_a_stack_is_refused(tmp_path: Path, source: str) -> None:
    """7. With a stack its checks are the only verification commands: every
    other place a command can come from exits 2 before the engineer, and
    the refusal names the source."""
    section, line, env, flags, named = _SOURCES[source]
    root = _repo(tmp_path, _stack({"tests": "true"}))
    if section:
        toml = root / "kstrl.toml"
        text = toml.read_text(encoding="utf-8")
        header = f"\n[{section}]\n"
        text = (
            text.replace(header, f"{header}{line}\n", 1)
            if header in text
            else f"{text}\n[{section}]\n{line}\n"
        )
        _commit(root, "kstrl.toml", text)

    run = _factory(tmp_path, root, *flags, env=env)

    assert run.code == 2, run.out
    assert named in run.out, run.out
    assert run.calls == 0, run.out


@pytest.mark.parametrize(
    ("table", "message"),
    [
        (_stack({}), "checks is empty"),
        (_stack({"tests": "true"}).replace('"tests" = "true"', '"tests" = 1'), "checks.tests"),
        (_stack({"tests": "true"}, instructions=" "), "instructions must be non-empty"),
        (_stack({"tests": "true"}).replace('setup = ""\n', ""), "setup is required"),
        (_stack({"tests": "true"}).replace("env = []", 'env = "PATH"'), "env must be a list"),
        (_stack({"tests": "true"}).replace("env = []", "env = []\nname = 'x'"), "name is not"),
        (_stack({"tests": "true"}, rung={"writable": "~/.cache"}), "writable must be a list"),
        (_stack({"tests": "true"}, rung={"readable": [""]}), "readable[0] must be a non-empty"),
        (_stack({"tests": "true"}, rung={"browser": "yes"}), "browser must be true or false"),
    ],
    ids=[
        "empty-checks",
        "non-string-command",
        "blank-instructions",
        "no-setup",
        "env",
        "key",
        "writable",
        "readable",
        "browser",
    ],
)
def test_a_malformed_stack_is_refused_once_with_an_indexed_reason(
    tmp_path: Path, table: str, message: str
) -> None:
    """8. A stack kstrl would have to guess about is refused before anything
    runs, with the reason indexed, said once, and no traceback."""
    root = _repo(tmp_path, table, confirm=False)

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert run.out.count(message) == 1, run.out
    assert "[stack]" in run.out
    assert "Traceback" not in run.out
    assert run.calls == 0, run.out


@runs_a_stack
def test_phase_3_runs_every_check_on_the_merged_tree(tmp_path: Path) -> None:
    """9. Each engineer adds one marker file. Each component alone passes
    both checks, and the LAST check fails only where both markers meet: on
    the tree Phase 3 merges. Phase 3 runs every check, so it fails there."""
    checks = {
        "tests": 'test "$DEMO_DECLARED" = yes',
        "merged": "test ! -f comp-a.marker || test ! -f comp-b.marker",
    }
    root = _repo(tmp_path, _stack(checks, env=["DEMO_DECLARED"]), comps=("comp-a", "comp-b"))
    engineer = (
        'touch "$(basename "$PWD").marker" && git add -A && git commit -q -m marker >/dev/null 2>&1'
    )

    run = _factory(
        tmp_path, root, contract="final", engineer=engineer, env={"DEMO_DECLARED": "yes"}
    )

    assert run.calls == 2, run.out
    assert "Phase 1 FAILED" not in run.out, run.out
    assert "contract tests FAILED" in run.out, run.out
    # The bisection runs the same checks with the same env: merging comp-a
    # alone passes, so the breaker is comp-b, on the check that failed.
    assert "breaker 'comp-b'" in run.out, run.out
    assert "stack:merged: `" in run.out, run.out
    assert run.code != 0, run.out


@runs_a_stack
def test_the_engineer_is_told_the_stack_and_every_check(tmp_path: Path) -> None:
    """10. The instructions and each check, name and command, reach the
    engineer's prompt, and the block naming the three resolved gate
    commands, which would state Python defaults, is not there."""
    checks = {"tests": "true", "lint": "test -f Cargo.toml"}
    root = _repo(tmp_path, _stack(checks))

    run = _factory(tmp_path, root)

    assert run.calls == 1, run.out
    assert INSTRUCTIONS in run.prompts
    for name, command in checks.items():
        assert f"- {name}: `{command}`" in run.prompts, run.prompts[-2000:]
    assert "# Verification Commands (resolved by kstrl)" not in run.prompts
    # kstrl.toml sets no [contract] test_command; the stack blanks it.
    assert "[contract] test_command" not in run.out, run.out


@runs_a_stack
def test_phase_1_runs_every_check_on_each_component(tmp_path: Path) -> None:
    """11. Phase 1 runs the stack's checks on the component's own tree. The
    base passes both checks; the engineer's commit makes the LAST one fail,
    so Phase 1 fails on it, by name. A Phase 1 that selected none of the
    stack's checks would pass on nothing."""
    checks = {"tests": "true", "own": "test ! -f made.marker"}
    root = _repo(tmp_path, _stack(checks))
    engineer = "touch made.marker && git add -A && git commit -q -m marker >/dev/null 2>&1"

    run = _factory(tmp_path, root, engineer=engineer)

    assert run.calls == 1, run.out
    assert "Phase 1 FAILED" in run.out, run.out
    assert "stack:own" in run.out, run.out
    assert run.code != 0, run.out
