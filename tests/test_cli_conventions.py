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
test reaches, in every module of ``kstrl/`` (#531): every literal 1 in an
``exit``/``_exit`` call or its ``code=`` keyword, a ``SystemExit``, an
``exit_code`` assignment or keyword, a ``return`` in ``kstrl/cli.py``, or a
return of a function an exit code is followed into, sits in a site listed
in ``FINDING_EXIT_SITES``. Its blind spots, stated and pinned by strict
xfails: a function named in ``NOT_FOLLOWED``, an exit code carried in a
container, and an exit code passed to a result class positionally. A
refusal that escapes as an uncaught exception exits 1 with a traceback
and no static walk sees it. The process table and
``tests/test_prelaunch_refusal_exit.py`` cover what they can run.
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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers.astwalk import (
    blind_spot,
    label,
    leaf_name,
    own_nodes,
    package_sources,
    parse,
    parsed,
)
from tests.helpers.fakegh import put_gh_on_path
from tests.helpers.gitrepo import git_in

CLI_MODULE = "cli.py"

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
    (("ci", "poll"), (), 2, "no manifest: nothing has run here"),
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
    (("learn", "repair"), (), 0, "a missing ledger has nothing to repair"),
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


# --- static census: a literal exit 1 is a finding, in every module --------

#: Every literal 1 that can become a process exit code anywhere in kstrl/,
#: by (module under kstrl/, top-level function or class, or "<module>"),
#: with how many. Each one reports a FINDING, or is a crash; a refusal
#: exits 2. A new literal 1 in any shape ``_exit_census`` follows fails
#: this census, in whichever module it lands (#531).
FINDING_EXIT_SITES: dict[tuple[str, str], int] = {
    ("baseline.py", "exit_code_for"): 1,  # `--fail-on-regression`: a regression
    ("calibration.py", "main"): 1,  # `python -m kstrl.calibration`: a detection drop
    ("cli.py", "_check_baseline_report"): 1,  # `--fail-on-regression`: a regression
    ("cli.py", "_check_report"): 2,  # `ks check`: a check failed
    ("cli.py", "ci_poll"): 1,  # a merge commit's CI failed or could not be read
    ("cli.py", "config_show"): 2,  # a rejected section, reported
    ("cli.py", "decompose"): 1,  # the architect's output could not be used
    ("cli.py", "factory"): 1,  # the architect's output could not be used
    ("cli.py", "health_cmd"): 1,  # a metric breached its control limits
    ("cli.py", "queue_sync"): 1,  # an issue could not be synced
    ("cli.py", "serve"): 1,  # work is waiting on a human
    ("doctor.py", "exit_code_for"): 1,  # `ks doctor`: not ready is a finding
    ("factory.py", "resolve_exit_code"): 5,  # a failed, unmerged, parked or unscheduled run
    ("init_cmd.py", "run_init"): 2,  # `ks init`: the PRD it validated is invalid
    ("learning_fixture.py", "main"): 1,  # `python -m kstrl.learning_fixture`: the check failed
    ("loop.py", "run_loop"): 5,  # an iteration ran and the loop ended short
    ("tui/bridge.py", "CommandHandle"): 2,  # the command raised: the exit a traceback gets
}

#: Callee names the census does not follow, and why. Following one would
#: census every function in kstrl/ that shares the name.
NOT_FOLLOWED: dict[str, str] = {
    "run": (
        "Textual's App.run returns what the app's own exit() was given, and "
        "those calls are in the census. A kstrl function named `run` that "
        "returns an exit code is the disclosed blind spot below."
    ),
}

#: Every function whose result becomes an exit code, as the census derives
#: them by following each exit site in kstrl/ into what it calls. Pinned,
#: so a walk that stopped following shows as a changed set, not a green
#: census.
EXIT_CODE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "_decompose_core",
        "_load_and_render",
        "_plain_fallback",
        "_target",
        "_understand_core",
        "exit_code",
        "exit_code_for",
        "main",
        "report_to_ladder",
        "resolve_exit_code",
        "run_embedded",
        "run_factory_embedded",
        "run_feature",
        "run_home_shell",
        "run_init",
    }
)

#: Exit-code values the census cannot follow to a literal or a function,
#: by site, with how many. A new one fails here, so a value the walk
#: cannot read is a row somebody explains rather than a gap.
UNRESOLVED_EXIT_VALUES: dict[tuple[str, str], int] = {
    # The command thread's own return, read back out of result_box: the
    # container blind spot below.
    ("tui/bridge.py", "CommandHandle"): 1,
    # A message carrying the init subprocess's code, whatever it was.
    ("tui/screens/init_wizard.py", "WizardDone"): 1,
}


@dataclass
class _ExitCensus:
    ones: Counter[tuple[str, str]] = field(default_factory=Counter)
    unresolved: Counter[tuple[str, str]] = field(default_factory=Counter)
    followed: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)

    def take(self, key: tuple[str, str], tokens: Iterable[str]) -> None:
        for token in tokens:
            if token == "1":
                self.ones[key] += 1
            elif token == "?":
                self.unresolved[key] += 1
            else:
                self.calls.add(token)


def _tops(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Each top-level statement, keyed by the def or class it defines."""
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield stmt.name, stmt
        else:
            yield "<module>", stmt


def _assigned(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """The targets and value of an assignment, or ([], None)."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


#: Calls whose first argument, or click's ``code=`` keyword, is a process
#: exit code: ``sys.exit``, ``ctx.exit``, Textual's ``App.exit``,
#: ``os._exit`` and ``SystemExit``.
EXIT_CALLS = frozenset({"exit", "_exit", "SystemExit"})


def _exit_value(node: ast.AST) -> ast.expr | None:
    """The exit code in ``<x>.exit(v)``, ``ctx.exit(code=v)``, ``os._exit(v)``,
    ``SystemExit(v)``, an assignment to ``exit_code`` or ``<x>.exit_code``,
    or an ``exit_code=`` keyword."""
    if isinstance(node, ast.Call) and leaf_name(node.func) in EXIT_CALLS:
        codes = [*node.args[:1], *(kw.value for kw in node.keywords if kw.arg == "code")]
        return codes[0] if codes else None
    if isinstance(node, ast.keyword) and node.arg == "exit_code":
        return node.value
    targets, value = _assigned(node)
    if any(leaf_name(target) == "exit_code" for target in targets):
        return value
    return None


def _is_text(value: ast.expr) -> bool:
    """``sys.exit("...")`` prints the text and exits 1."""
    return isinstance(value, ast.JoinedStr) or (
        isinstance(value, ast.Constant) and isinstance(value.value, str)
    )


def _bound(name: str, nodes: Iterable[ast.AST]) -> list[ast.expr]:
    """The values these nodes assign to the plain name ``name``."""
    found = []
    for node in nodes:
        targets, value = _assigned(node)
        if value is not None and any(isinstance(t, ast.Name) and t.id == name for t in targets):
            found.append(value)
    return found


def _reach(value: ast.expr, scope: ast.AST, module: ast.Module) -> Iterator[str]:
    """What an exit code is made of: "1" per literal 1, the name of each
    function whose result it is, and "?" for a part the walk cannot
    follow. Any other constant is a code that is not 1."""
    if isinstance(value, ast.Constant):
        if type(value.value) is int and value.value == 1:
            yield "1"
    elif isinstance(value, ast.IfExp):
        yield from _reach(value.body, scope, module)
        yield from _reach(value.orelse, scope, module)
    elif isinstance(value, ast.BoolOp):
        for part in value.values:
            yield from _reach(part, scope, module)
    elif isinstance(value, ast.Call):
        yield from _callees(value, scope)
    elif isinstance(value, ast.Attribute) and value.attr == "exit_code":
        yield "exit_code"
    elif isinstance(value, ast.Name):
        yield from _reach_name(value, scope, module)
    else:
        yield "?"


def _reach_name(name: ast.Name, scope: ast.AST, module: ast.Module) -> Iterator[str]:
    """A name's values: bound in its scope, else at module level."""
    values = _bound(name.id, ast.walk(scope)) or _bound(name.id, module.body)
    if not values:
        yield "?"
    for value in values:
        if isinstance(value, ast.Name) and value.id == name.id:
            yield "?"
        else:
            yield from _reach(value, scope, module)


def _callees(call: ast.Call, scope: ast.AST) -> Iterator[str]:
    """The function a call's result comes from, and each function defined
    in this scope that the call is handed (``run_embedded(_target)``)."""
    yield leaf_name(call.func) or "?"
    local = {
        n.name for n in ast.walk(scope) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for arg in call.args:
        if isinstance(arg, ast.Name) and arg.id in local:
            yield arg.id


def _sites(modules: dict[str, ast.Module]) -> Iterator[tuple[tuple[str, str], ast.AST, ast.Module]]:
    """Every top-level statement of every module, keyed by its site."""
    for path, tree in modules.items():
        for top_name, top in _tops(tree):
            yield (path, top_name), top, tree


def _functions_named(
    modules: dict[str, ast.Module], names: set[str]
) -> Iterator[tuple[tuple[str, str], ast.AST, ast.Module]]:
    """Every function called one of ``names``, nested or not, by site."""
    for key, top, tree in _sites(modules):
        for func in ast.walk(top):
            if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef) and func.name in names:
                yield key, func, tree


def _own_returns(func: ast.AST) -> Iterator[tuple[ast.Return, ast.expr]]:
    """The function's own returns with a value, not a nested function's."""
    for node in own_nodes(func):
        if isinstance(node, ast.Return) and node.value is not None:
            yield node, node.value


def _count_site(
    census: _ExitCensus, key: tuple[str, str], top: ast.AST, tree: ast.Module, counted: set[int]
) -> None:
    """One top-level statement's exit values, and cli.py's returns."""
    for node in ast.walk(top):
        value = _exit_value(node)
        if value is not None:
            census.take(key, _reach(value, top, tree))
            census.ones[key] += _is_text(value)
        elif key[0] == CLI_MODULE and isinstance(node, ast.Return) and node.value:
            counted.add(id(node))
            census.ones[key] += list(_reach(node.value, top, tree)).count("1")


def _exit_census(modules: dict[str, ast.Module]) -> _ExitCensus:
    """Every literal 1 that reaches an exit code, following each value into
    the functions it comes from until no new one turns up. Every return in
    kstrl/cli.py is counted as an exit code, as before #531."""
    census = _ExitCensus()
    counted: set[int] = set()
    for key, top, tree in _sites(modules):
        _count_site(census, key, top, tree, counted)
    while pending := census.calls - census.followed - NOT_FOLLOWED.keys():
        census.followed |= pending
        for key, func, tree in _functions_named(modules, pending):
            for ret, value in _own_returns(func):
                tokens = _reach(value, func, tree)
                census.take(key, (t for t in tokens if t != "1" or id(ret) not in counted))
    census.skipped = census.calls & NOT_FOLLOWED.keys()
    census.ones = +census.ones
    return census


def _kstrl_modules() -> dict[str, ast.Module]:
    return {label(path): parsed(path) for path in package_sources()}


def test_the_census_sees_every_shape_of_exit_one() -> None:
    """The control: the walk must count every shape kstrl/ uses, in and out
    of cli.py, or an empty census would pass for the wrong reason."""
    cli_source = (
        "import sys\n"
        "def a():\n    sys.exit(1)\n"
        "def b(ok):\n    sys.exit(0 if ok else 1)\n"
        "class C:\n    def m(self, ctx):\n        ctx.exit(1)\n"
        "def d():\n    sys.exit(2)\n"
        "def e():\n    raise SystemExit(1)\n"
        "def f(bad):\n    return 1 if bad else 0\n"
        "def g():\n    return 2\n"
        "def h():\n    sys.exit('boom')\n"
        "def i():\n    code = decide()\n    sys.exit(code or 0)\n"
        "def j(r):\n    sys.exit(r.exit_code)\n"
        "def k(ctx):\n    ctx.exit(code=1)\n"
        "def m():\n    os._exit(1)\n"
    )
    other_source = (
        "EXIT_BAD = 1\n"
        "def refuse(result):\n    result.exit_code = 1\n"
        "def launch():\n    return Result(completed=False, exit_code=1)\n"
        "def decide(x):\n    if x:\n        return EXIT_BAD\n    return helper()\n"
        "def helper():\n    return 1\n"
        "def unrelated():\n    return 1\n"
        "class Handle:\n    @property\n    def exit_code(self):\n        return 1\n"
    )
    census = _exit_census({CLI_MODULE: parse(cli_source), "other.py": parse(other_source)})
    assert census.ones == Counter(
        {
            **dict.fromkeys([(CLI_MODULE, n) for n in "abCefhkm"], 1),
            **dict.fromkeys([("other.py", n) for n in ("refuse", "launch", "decide")], 1),
            ("other.py", "helper"): 1,
            ("other.py", "Handle"): 1,
        }
    )
    assert census.followed == {"decide", "helper", "exit_code"}
    assert census.unresolved == Counter()


def test_every_literal_exit_one_in_kstrl_is_a_listed_finding() -> None:
    census = _exit_census(_kstrl_modules())
    assert dict(census.ones) == FINDING_EXIT_SITES


def test_the_census_follows_every_function_an_exit_code_comes_from() -> None:
    census = _exit_census(_kstrl_modules())
    assert census.followed == EXIT_CODE_FUNCTIONS
    assert census.skipped == set(NOT_FOLLOWED)


def test_every_exit_value_the_census_cannot_follow_is_listed() -> None:
    census = _exit_census(_kstrl_modules())
    assert dict(census.unresolved) == UNRESOLVED_EXIT_VALUES


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="`run` is not followed")
def test_a_function_named_run_that_returns_an_exit_code_is_missed() -> None:
    blind_spot(
        lambda text: _exit_census({"other.py": parse(text)}).ones[("other.py", "run")] > 0,
        "import sys\ndef cmd():\n    sys.exit(run())\ndef run():\n    return 1\n",
    )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="a container is not followed")
def test_an_exit_code_carried_in_a_container_is_missed() -> None:
    blind_spot(
        lambda text: _exit_census({"other.py": parse(text)}).ones[("other.py", "cmd")] > 0,
        "import sys\ndef cmd(box):\n    box.append(1)\n    sys.exit(box[0])\n",
    )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="a positional exit_code is missed")
def test_an_exit_code_passed_positionally_is_missed() -> None:
    blind_spot(
        lambda text: _exit_census({"other.py": parse(text)}).ones[("other.py", "refuse")] > 0,
        "def refuse():\n    return LoopResult(False, 0, 1)\n",
    )
