"""Every claude session that kstrl starts loads no MCP server, reads no
user-level setting, and gets only the tools of its role, in every role and
with the sandbox on or off (#700, #803).

End to end: the real ``ks`` with a stub ``claude`` that records the argv of
each call. The helpers are the ones of tests/test_sandbox_engineer_e2e.py.
The static guard at the end fails when a claude spawn in ``kstrl/`` does not
take its flags from ``kstrl.sandbox.claude_session_argv`` or
``claude_session_flags``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import kstrl
from tests.test_sandbox_engineer_e2e import (
    _FACTORY,
    _UNDERSTAND,
    CLAUDE_TOML,
    SANDBOX_OFF_TOML,
    SANDBOX_TOML,
    _ks,
    _runs,
)

_FLAG = "--strict-mcp-config"

#: Spelled out rather than imported, so that a change to the tools of a role
#: must change this test too. The writer's network is open by default (#700).
_WRITER_TOOLS = (
    "Bash,Edit,Glob,Grep,Monitor,NotebookEdit,Read,Task,TaskCreate,TaskGet,"
    "TaskList,TaskStop,TaskUpdate,Write,WebFetch,WebSearch"
)
_READER_TOOLS = "Bash,Glob,Grep,Read"

#: (id, ks argv). The factory with both review gates on runs the engineer, the
#: code reviewer and the security reviewer; `ks understand` runs the
#: understand role.
_ROLES = [
    ("factory", (*_FACTORY, "--review-mode", "hard", "--security-mode", "hard")),
    ("understand", _UNDERSTAND),
]


def _value(call: list[str], flag: str) -> str | None:
    """The argv value after ``flag``, or None when ``flag`` is absent."""
    return call[call.index(flag) + 1] if flag in call else None


@pytest.mark.parametrize(
    "sandbox", [SANDBOX_TOML, SANDBOX_OFF_TOML], ids=["sandbox-on", "sandbox-off"]
)
@pytest.mark.parametrize("argv", [row[1] for row in _ROLES], ids=[row[0] for row in _ROLES])
def test_every_claude_session_kstrl_starts_gets_strict_mcp_config(
    tmp_path: Path, sandbox: str, argv: tuple[str, ...]
) -> None:
    """RED before #803: each session got the 26 tools of claude 2.1.296, among
    them CronCreate, RemoteTrigger, Workflow and EnterWorktree, and it read the
    operator's user settings, so the operator's hooks ran in it (measured). Now
    each session gets ``--setting-sources project`` and ``--tools`` with the
    tools of its role, and a reader's sandboxed Bash needs a permission rule."""
    ran = _ks(tmp_path, CLAUDE_TOML + sandbox, argv, {}, clis=("claude",))
    runs = _runs(tmp_path, "bin/claude")
    assert runs, ran
    missing = [call for call in runs if _FLAG not in call]
    assert not missing, (missing, ran)
    sources = [_value(call, "--setting-sources") for call in runs]
    assert set(sources) == {"project"}, (sources, ran)
    tools = [_value(call, "--tools") for call in runs]
    assert set(tools) <= {_WRITER_TOOLS, _READER_TOOLS}, (tools, ran)
    assert _WRITER_TOOLS in tools, (tools, ran)
    for call in runs:
        if _value(call, "--tools") == _READER_TOOLS:
            settings = json.loads(_value(call, "--settings") or "{}")
            assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False, (call, ran)
    if argv is _ROLES[0][1]:
        # The engineer, and at least one reviewer, which has no write guard.
        assert len(runs) >= 2, (runs, ran)
        assert _READER_TOOLS in tools, (tools, ran)


# ---------------------------------------------------------------------------
# Static guard: each claude spawn in kstrl/ carries the session flags (#803)
# ---------------------------------------------------------------------------
#
# Closed by construction: the census counts each string constant ``claude``
# (and each one that starts ``claude -``, a shell command) in kstrl/, and each
# name ``ClaudeAgentOptions``. A new spawn shape adds a constant or a name,
# which changes the census, and the test is red until somebody classifies the
# new site. A spawn is a constant that is the first item of a list or tuple
# display: that display must also hold ``*claude_session_argv(...)``. Each
# ``ClaudeAgentOptions`` must be in a function that sets ``extra_args`` to a
# call of ``claude_session_flags``.

_KSTRL = Path(kstrl.__file__).parent

#: (file relative to kstrl/, kind) -> count. ``spawn`` is an argv display that
#: starts with ``claude``, ``other`` is each other constant (an alias, a
#: message, ``shutil.which``), ``sdk`` is a ``ClaudeAgentOptions`` name.
EXPECTED_CLAUDE_SITES = {
    ("agents/__init__.py", "other"): 1,
    ("agents/claude_code.py", "other"): 1,
    ("agents/claude_code.py", "spawn"): 1,
    ("agents/liveness.py", "spawn"): 1,
    ("agents/sdk_runner.py", "sdk"): 1,
    ("cli_preflight.py", "other"): 1,
}

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _is_call_to(node: ast.AST, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


def _claude_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "claude" or node.value.startswith("claude -"))
    )


def _names_sdk_options(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "ClaudeAgentOptions") or (
        isinstance(node, ast.Attribute) and node.attr == "ClaudeAgentOptions"
    )


def _sets_session_extra_args(scope: ast.AST) -> bool:
    """Whether ``scope`` sets ``extra_args`` to a ``claude_session_flags`` call,
    as a dict key or as a keyword."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "extra_args"
                    and _is_call_to(value, "claude_session_flags")
                ):
                    return True
        if (
            isinstance(node, ast.keyword)
            and node.arg == "extra_args"
            and _is_call_to(node.value, "claude_session_flags")
        ):
            return True
    return False


def _nodes_with_scope(tree: ast.AST) -> list[tuple[ast.AST, ast.AST | None]]:
    """Each node of ``tree`` with its innermost enclosing function, or None."""
    found: list[tuple[ast.AST, ast.AST | None]] = []

    def visit(node: ast.AST, scope: ast.AST | None) -> None:
        found.append((node, scope))
        inner = node if isinstance(node, _FUNCTIONS) else scope
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(tree, None)
    return found


def _is_spawn(node: ast.AST) -> bool:
    """A list or tuple display whose first item is a claude constant."""
    return (
        isinstance(node, (ast.List, ast.Tuple))
        and bool(node.elts)
        and _claude_constant(node.elts[0])
    )


def _classify(
    node: ast.AST, scope: ast.AST | None, heads: set[int]
) -> tuple[str | None, str | None]:
    """``(kind, offence)`` of one node: kind None when it is not a site."""
    if _is_spawn(node):
        assert isinstance(node, (ast.List, ast.Tuple))
        carries = any(
            isinstance(elt, ast.Starred) and _is_call_to(elt.value, "claude_session_argv")
            for elt in node.elts
        )
        return "spawn", None if carries else "argv without claude_session_argv"
    if _claude_constant(node):
        return (None if id(node) in heads else "other"), None
    if _names_sdk_options(node):
        carries = scope is not None and _sets_session_extra_args(scope)
        return "sdk", None if carries else "no extra_args=claude_session_flags(...)"
    return None, None


def claude_sites(root: Path) -> tuple[dict[tuple[str, str], int], list[str]]:
    """``(census, offenders)`` for the ``.py`` files under ``root``."""
    census: dict[tuple[str, str], int] = {}
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        nodes = _nodes_with_scope(ast.parse(path.read_text(encoding="utf-8")))
        heads = {
            id(node.elts[0])
            for node, _ in nodes
            if isinstance(node, (ast.List, ast.Tuple)) and _is_spawn(node)
        }
        for node, scope in nodes:
            kind, offence = _classify(node, scope, heads)
            if kind is not None:
                census[(rel, kind)] = census.get((rel, kind), 0) + 1
            if offence is not None:
                offenders.append(f"{rel}:{getattr(node, 'lineno', 0)}: {offence}")
    return census, offenders


def test_each_claude_spawn_in_kstrl_carries_the_session_flags() -> None:
    census, offenders = claude_sites(_KSTRL)
    assert census == EXPECTED_CLAUDE_SITES, (
        "the census of claude spawns in kstrl/ changed. Classify each new site: an "
        "argv that starts with 'claude' must hold *claude_session_argv(...), and a "
        "ClaudeAgentOptions must get extra_args=claude_session_flags(...).",
        census,
    )
    assert not offenders, offenders


def test_the_static_guard_flags_a_spawn_without_the_session_flags(tmp_path: Path) -> None:
    """The control of the guard: it counts and flags each kind of offender,
    and it does not flag a clean argv or a clean SDK call."""
    (tmp_path / "bad.py").write_text(
        "def a():\n"
        "    return ['claude', '--print']\n"
        "def b(sdk):\n"
        "    return sdk.ClaudeAgentOptions(extra_args={'strict-mcp-config': None})\n"
        "c = 'claude --print'\n",
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text(
        "def a():\n"
        "    return ['claude', *claude_session_argv('probe', network=False)]\n"
        "def b(sdk):\n"
        "    kw = {'extra_args': claude_session_flags('writer', network=True)}\n"
        "    return sdk.ClaudeAgentOptions(**kw)\n",
        encoding="utf-8",
    )
    census, offenders = claude_sites(tmp_path)
    assert census == {
        ("bad.py", "spawn"): 1,
        ("bad.py", "sdk"): 1,
        ("bad.py", "other"): 1,
        ("good.py", "spawn"): 1,
        ("good.py", "sdk"): 1,
    }, census
    assert [line.split(":")[0] for line in offenders] == ["bad.py", "bad.py"], offenders
