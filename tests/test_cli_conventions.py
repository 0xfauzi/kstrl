"""The command-line conventions every `ks` command keeps (#452).

Three contracts, each held by walking the real click tree rather than
by a list someone remembers to extend:

1. Shared options. Every leaf command takes `--ui` and `--no-color`
   unless it is listed in ``UI_OPTION_EXEMPT`` with the reason; every
   `--yes` has `-y`; every `--root` says what it defaults to; every
   `--manifest` and `--progress-log` names its default.
2. The exit-code contract. 0: the command did what was asked and found
   nothing that needs the operator (an empty answer counts). 1: it did
   what was asked and the answer is a finding. 2: it could not do what
   was asked (a usage error, a rejected configuration, a missing input,
   a feature that is off, a refusal before any work starts). Every
   leaf is run as a real process in a fresh `git init` directory and
   must exit with the code ``EMPTY_REPO_EXITS`` gives it, unless it is
   listed in ``NOT_INVOKED`` with the reason.
3. No escape sequences on a pipe. The same processes write to pipes,
   so none of their output may carry an ANSI escape; the pty test is
   the positive control that colour still reaches a terminal.

The static census at the end holds the exit contract where no process
test reaches: every literal 1 in an ``exit`` call, a ``SystemExit`` or a
``return`` in ``kstrl/cli.py`` sits in a function listed in
``FINDING_EXIT_SITES``. Its blind spot, stated: exit codes returned by
any module other than ``kstrl/cli.py`` (``kstrl/factory.py`` returns the
run's own exit code, ``kstrl/init_cmd.run_init`` returns 1 for a PRD it
validated and found invalid). The process table covers what it can run.
"""

from __future__ import annotations

import ast
import os
import re
import select
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers.fakegh import put_gh_on_path
from tests.helpers.gitrepo import git_in

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_SOURCE = REPO_ROOT / "kstrl" / "cli.py"

UI_CHOICES = ("auto", "rich", "plain", "gum")

#: Leaf commands that take neither `--ui` nor `--no-color`, and why.
UI_OPTION_EXEMPT: dict[tuple[str, ...], str] = {
    ("dash",): (
        "a full-screen Textual app: there is no line renderer to choose, "
        "and Textual honours NO_COLOR itself"
    ),
    ("doctor",): (
        "its report is unstyled text on stdout, so there is no colour to "
        "turn off, and --json is its machine form"
    ),
}

#: (command path, extra argv, expected exit code, why). Run in a fresh
#: `git init` directory with no commits, no kstrl.toml and no run.
EMPTY_REPO_EXITS: tuple[tuple[tuple[str, ...], tuple[str, ...], int, str], ...] = (
    (("autonomy", "demote"), ("--reason", "r"), 0, "already at the lowest level"),
    (("autonomy", "history"), (), 0, "an empty history is an answer"),
    (("autonomy", "promote"), ("--actor", "a", "--ack", "b"), 2, "refused without a terminal"),
    (("autonomy", "replay"), (), 2, "too little history to replay anything"),
    (("autonomy", "status"), (), 0, "reports the starting level"),
    (("check",), (), 2, "git cannot produce a diff in a repo with no commits"),
    (("config", "show"), (), 0, "every value resolves to its default"),
    (("dash",), (), 2, "needs a terminal"),
    (("decompose",), (), 2, "usage: --spec is required"),
    (("doctor",), (), 1, "not-ready is a finding"),
    (("evolve",), (), 0, "no failure patterns yet"),
    (("factory",), (), 2, "needs --spec or --manifest"),
    (("health",), (), 0, "too little history to call a breach"),
    (("inbox", "approve"), ("x",), 2, "no such inbox item"),
    (("inbox", "ls"), (), 0, "an empty inbox"),
    (("inbox", "reject"), ("x", "--comment", "c"), 2, "no such inbox item"),
    (("inbox", "retry"), ("x",), 2, "no such inbox item"),
    (("inbox", "show"), ("x",), 2, "no such inbox item"),
    (("inbox", "snooze"), ("x",), 2, "no such inbox item"),
    (("init",), ("--ui", "plain"), 0, "scaffolds the project"),
    (("learn", "playbook"), (), 0, "an empty playbook is an answer"),
    (("queue", "add"), ("spec.md",), 0, "queues the spec"),
    (("queue", "ls"), (), 0, "an empty queue"),
    (("queue", "pause"), (), 0, "pauses intake"),
    (("queue", "resume"), (), 0, "resumes intake"),
    (("queue", "retry"), ("x",), 2, "no such queue item"),
    (("queue", "rm"), ("x", "--yes"), 2, "no such queue item"),
    (("queue", "show"), ("x",), 2, "no such queue item"),
    (("queue", "sync"), (), 2, "GitHub intake is off"),
    (("retry",), ("x",), 2, "no manifest: nothing has run here"),
    (("signals", "ls"), (), 0, "no signals recorded"),
    (("signals", "poll"), (), 2, "signals polling is off"),
    (("status",), (), 2, "no manifest: nothing has run here"),
)

#: Leaf commands the process table does not run, and why.
NOT_INVOKED: dict[tuple[str, ...], str] = {
    ("run",): "calls the agent when one is on PATH; its refusals are in tests/test_preflight.py",
    ("understand",): "calls the agent when one is on PATH; see tests/test_preflight.py",
    ("feature",): "calls the agent when one is on PATH; see tests/test_preflight.py",
    ("serve",): "a daemon, and --dry-run asks gh about open PRs; see tests/test_serve_cli.py",
}

#: Every function in kstrl/cli.py that passes a literal 1 to an exit
#: call, with how many times. Each one reports a FINDING; a refusal
#: exits 2. A new `sys.exit(1)` anywhere else fails this census.
FINDING_EXIT_SITES: dict[str, int] = {
    "_check_report": 2,  # `ks check`: a check failed
    "_check_baseline_report": 1,  # `--fail-on-regression`: a regression
    "config_show": 2,  # a rejected section, reported
    "decompose": 1,  # the architect's output could not be used
    "factory": 1,  # the architect's output could not be used
    "health_cmd": 1,  # a metric breached its control limits
    "queue_sync": 1,  # an issue could not be synced
    "serve": 1,  # work is waiting on a human
}


def _leaves(
    group: click.Group, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    for name in sorted(group.commands):
        command = group.commands[name]
        path = (*prefix, name)
        if isinstance(command, click.Group):
            yield from _leaves(command, path)
        else:
            yield path, command


LEAVES: dict[tuple[str, ...], click.Command] = dict(_leaves(cli))


def _options(command: click.Command) -> dict[str, click.Option]:
    return {
        opt: param
        for param in command.params
        if isinstance(param, click.Option)
        for opt in param.opts
    }


# --- 1. shared options ----------------------------------------------------


def test_the_walk_reaches_nested_leaves() -> None:
    """The control for every test below: a walk that stopped at the top
    level would see no `queue rm` and pass everything vacuously."""
    assert ("queue", "rm") in LEAVES
    assert ("autonomy", "replay") in LEAVES
    assert ("status",) in LEAVES


@pytest.mark.parametrize("path", sorted(LEAVES), ids=" ".join)
def test_every_leaf_takes_ui_and_no_color_unless_exempt(path: tuple[str, ...]) -> None:
    options = _options(LEAVES[path])
    if path in UI_OPTION_EXEMPT:
        assert "--ui" not in options and "--no-color" not in options, (
            f"ks {' '.join(path)} is listed as exempt but takes the option; drop it from the list"
        )
        return
    assert "--ui" in options, f"ks {' '.join(path)} has no --ui"
    ui = options["--ui"]
    assert isinstance(ui.type, click.Choice)
    assert tuple(ui.type.choices) == UI_CHOICES
    assert ui.default == "auto"
    assert "--no-color" in options, f"ks {' '.join(path)} has no --no-color"
    assert options["--no-color"].is_flag


def test_every_yes_option_has_the_short_flag() -> None:
    missing = [
        " ".join(path)
        for path, command in LEAVES.items()
        if "--yes" in _options(command) and "-y" not in _options(command)["--yes"].opts
    ]
    assert missing == []
    assert any("--yes" in _options(command) for command in LEAVES.values())


def test_every_root_option_says_it_defaults_to_the_current_directory() -> None:
    wrong = [
        " ".join(path)
        for path, command in LEAVES.items()
        if "--root" in _options(command)
        and not (_options(command)["--root"].help or "").endswith("(defaults to current directory)")
    ]
    assert wrong == []


_NAMES_A_DEFAULT = re.compile(r"\((?:default: [^)]*<root>/|no default)")


@pytest.mark.parametrize("option", ["--manifest", "--progress-log"])
def test_manifest_and_progress_log_help_names_the_default(option: str) -> None:
    seen = 0
    for path, command in LEAVES.items():
        param = _options(command).get(option)
        if param is None:
            continue
        seen += 1
        assert _NAMES_A_DEFAULT.search(param.help or ""), (
            f"ks {' '.join(path)} {option}: {param.help}"
        )
    assert seen >= 2


def test_factory_and_retry_describe_the_progress_log_the_same_way() -> None:
    """`ks retry` hands --progress-log straight to `ks factory`, so the
    two help texts describe one log and must say the same thing."""
    factory_help = _options(LEAVES[("factory",)])["--progress-log"].help
    retry_help = _options(LEAVES[("retry",)])["--progress-log"].help
    assert factory_help == retry_help


# --- 2. exit codes --------------------------------------------------------


def test_every_leaf_is_in_the_exit_table_or_listed_as_not_invoked() -> None:
    tabled = [path for path, _args, _code, _why in EMPTY_REPO_EXITS]
    assert len(tabled) == len(set(tabled))
    assert set(tabled).isdisjoint(NOT_INVOKED)
    assert set(tabled) | set(NOT_INVOKED) == set(LEAVES)


@pytest.mark.parametrize("path", sorted(LEAVES), ids=" ".join)
def test_a_bad_flag_is_a_usage_error_on_every_leaf(path: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli, [*path, "--no-such-flag"])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output


@pytest.fixture
def empty_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `git init` directory with no commits, and a `gh` that answers
    `auth status` without reaching GitHub."""
    put_gh_on_path(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    (repo / "spec.md").write_text("# spec\n", encoding="utf-8")
    return repo


def _child_env() -> dict[str, str]:
    """The test process's environment minus every kstrl setting an
    operator may have exported, so a row cannot change because the
    machine has, say, KSTRL_SIGNALS_ENABLED=1. The probe stays off: a
    child with the probe on could spawn a real agent CLI and bill it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.pop("NO_COLOR", None)
    env.update(KSTRL_NO_TUI="1", KSTRL_AGENT_PROBE="0")
    return env


def _run_ks(repo: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kstrl", *argv],
        cwd=repo,
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("path", "args", "expected", "why"),
    EMPTY_REPO_EXITS,
    ids=[" ".join(row[0]) for row in EMPTY_REPO_EXITS],
)
def test_the_exit_code_on_an_empty_repo_follows_the_contract(
    empty_repo: Path, path: tuple[str, ...], args: tuple[str, ...], expected: int, why: str
) -> None:
    proc = _run_ks(empty_repo, [*path, *args])
    assert proc.returncode == expected, f"{why}\n{proc.stdout}\n{proc.stderr}"
    assert "\x1b[" not in proc.stdout + proc.stderr, "an escape sequence reached a pipe"


# --- 3. colour reaches a terminal, and NO_COLOR removes it ---------------


def _read_until_exit(master: int, proc: subprocess.Popen[bytes]) -> str:
    chunks: list[bytes] = []
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                break
            continue
        try:
            data = os.read(master, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


def _stderr_on_a_terminal(repo: Path, argv: list[str], env: dict[str, str]) -> str:
    master, slave = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "kstrl", *argv],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=slave,
    )
    os.close(slave)
    try:
        return _read_until_exit(master, proc)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=120)
        os.close(master)


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
@pytest.mark.parametrize(("no_color", "coloured"), [(False, True), (True, False)])
def test_plain_output_is_coloured_only_on_a_terminal_without_no_color(
    empty_repo: Path, no_color: bool, coloured: bool
) -> None:
    env = _child_env()
    if no_color:
        env["NO_COLOR"] = "1"
    text = _stderr_on_a_terminal(empty_repo, ["queue", "ls", "--ui", "plain"], env)
    assert "Queue is empty" in text
    assert ("\x1b[" in text) is coloured, repr(text)


# --- static census: a literal exit 1 is a finding -------------------------


def _exit_one_census(source: str) -> Counter[str]:
    """Literal ``1`` inside the argument of any ``<x>.exit(...)`` call,
    counted by the top-level function or class that holds the call."""
    census: Counter[str] = Counter()
    for top in ast.parse(source).body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        census[top.name] += sum(_literal_ones_in_exit_calls(top))
    return +census


def _exit_value(node: ast.AST) -> ast.AST | None:
    """The exit-code expression of ``<x>.exit(v)``, ``SystemExit(v)`` or
    ``return v``, or None. Every literal ``return 1`` in kstrl/cli.py is an
    exit code a caller hands to an exit call, so returns are counted too;
    a ``return 1`` that is not an exit code costs one row in the table."""
    if isinstance(node, ast.Return):
        return node.value
    if not (isinstance(node, ast.Call) and node.args):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "exit":
        return node.args[0]
    if isinstance(func, ast.Name) and func.id == "SystemExit":
        return node.args[0]
    return None


def _literal_ones_in_exit_calls(node: ast.AST) -> Iterator[int]:
    for sub in ast.walk(node):
        value = _exit_value(sub)
        if value is None:
            continue
        yield sum(
            1
            for leaf in ast.walk(value)
            if isinstance(leaf, ast.Constant) and type(leaf.value) is int and leaf.value == 1
        )


def test_the_census_sees_every_shape_of_exit_one() -> None:
    """The control: the walk must count the shapes kstrl/cli.py uses,
    or an empty census would pass for the wrong reason."""
    source = (
        "import sys\n"
        "def a():\n    sys.exit(1)\n"
        "def b(ok):\n    sys.exit(0 if ok else 1)\n"
        "class C:\n    def m(self, ctx):\n        ctx.exit(1)\n"
        "def d():\n    sys.exit(2)\n"
        "def e():\n    raise SystemExit(1)\n"
        "def f(bad):\n    return 1 if bad else 0\n"
        "def g():\n    return 2\n"
    )
    assert _exit_one_census(source) == Counter({"a": 1, "b": 1, "C": 1, "e": 1, "f": 1})


def test_every_literal_exit_one_in_the_cli_is_a_listed_finding() -> None:
    census = _exit_one_census(CLI_SOURCE.read_text(encoding="utf-8"))
    assert dict(census) == FINDING_EXIT_SITES
