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
import os
from pathlib import Path

import pytest

import kstrl
from tests.helpers.executables import write_executable
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
    "Bash,Edit,Glob,Grep,Monitor,NotebookEdit,Read,Skill,Task,TaskCreate,TaskGet,"
    "TaskList,TaskStop,TaskUpdate,Write,WebFetch,WebSearch"
)

#: The claude user settings of the test operator: a login (an apiKeyHelper and
#: an env key) and keys that kstrl must not copy (#803).
_HELPER = "/opt/operator/bin/claude-key-helper"
_ENV_KEY = "sk-ant-operator-env-key"
_USER_SETTINGS = {
    "apiKeyHelper": _HELPER,
    "env": {"KSTRL_E2E_LOGIN": _ENV_KEY},
    "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "operator-user-hook"}]}]},
    "permissions": {"allow": ["Bash(operator-allowed:*)"]},
    "enabledPlugins": {"operator-plugin@market": True},
    "model": "operator-model",
}


def _login_claude(path: Path, out: Path) -> Path:
    """A ``claude`` that records its argv like the stub of
    tests/test_sandbox_engineer_e2e.py, and the ``KSTRL_E2E_LOGIN`` of its
    environment in ``out``, one line a call."""
    argv = path.with_suffix(".argv")
    return write_executable(
        path,
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" -- >> '{argv}'\n"
        f"printf '%s\\n' \"${{KSTRL_E2E_LOGIN:-none}} ${{KSTRL_E2E_NUMBER:-none}}\" >> '{out}'\n"
        "cat >/dev/null\necho '<promise>COMPLETE</promise>'\n",
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
    tools of its role, and a reader's sandboxed Bash needs a permission rule.
    RED before the #803 follow-up: an apiKeyHelper or an env key in the user
    settings did not reach the session ("Not logged in", measured), and the
    writer had no Skill tool. Now each session gets the apiKeyHelper in its
    ``--settings`` and the env key in its environment, and no other key."""
    config = tmp_path / "operator-claude"
    config.mkdir()
    # A PATH there must not choose the claude that kstrl starts, an entry that
    # no environment can hold must not stop the session, and a number goes in
    # as text, as claude does (measured, #803).
    other = tmp_path / "operator-bin"
    other.mkdir()
    shadow = tmp_path / "shadow.out"
    write_executable(other / "claude", f"#!/bin/sh\necho ran >> '{shadow}'\n")
    env = {
        **_USER_SETTINGS["env"],
        "PATH": f"{other}{os.pathsep}{os.environ['PATH']}",
        "KSTRL_E2E_NUMBER": 5,
        "KSTRL_E2E=BAD": "x",
        "KSTRL_E2E_NUL": "a\0b",
    }
    user_settings = {**_USER_SETTINGS, "env": env}
    (config / "settings.json").write_text(json.dumps(user_settings), encoding="utf-8")
    login = tmp_path / "login.out"
    ran = _ks(
        tmp_path,
        CLAUDE_TOML + sandbox,
        argv,
        {"CLAUDE_CONFIG_DIR": str(config)},
        clis=("claude",),
        claude_out=login,
        claude_stub=_login_claude,
    )
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
    _assert_login_only(runs, login, ran)
    assert not shadow.exists(), ran
    if argv is _ROLES[0][1]:
        # The engineer, and at least one reviewer, which has no write guard.
        assert len(runs) >= 2, (runs, ran)
        assert _READER_TOOLS in tools, (tools, ran)


def _assert_login_only(runs: list[list[str]], login: Path, ran: str) -> None:
    """Each call got the operator's login and no other user setting."""
    for call in runs:
        settings = json.loads(_value(call, "--settings") or "{}")
        assert settings.get("apiKeyHelper") == _HELPER, (call, ran)
        assert call.count("--settings") == 1, (call, ran)
        text = "\n".join(call)
        for leak in (_ENV_KEY, "operator-user-hook", "operator-allowed", "operator-plugin"):
            assert leak not in text, (leak, call, ran)
        assert "operator-model" not in text, (call, ran)
    lines = login.read_text(encoding="utf-8").splitlines()
    assert lines == [f"{_ENV_KEY} 5"] * len(runs), ran


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
# call of ``claude_session_flags``. Each SDK session call (``query`` that is
# not ``self.query``, and ``ClaudeSDKClient``) is counted too and must get
# ``options``: with no options, the SDK starts claude with none of the flags,
# and no ``ClaudeAgentOptions`` name is in the source.

_KSTRL = Path(kstrl.__file__).parent

#: (file relative to kstrl/, kind) -> count. ``spawn`` is an argv display that
#: starts with ``claude``, ``other`` is each other constant (an alias, a
#: message, ``shutil.which``), ``sdk`` is a ``ClaudeAgentOptions`` name, and
#: ``sdk_call`` is an SDK session call.
EXPECTED_CLAUDE_SITES = {
    ("agents/claude_code.py", "other"): 1,
    ("agents/claude_code.py", "spawn"): 1,
    ("agents/liveness.py", "spawn"): 1,
    ("agents/sdk_runner.py", "sdk"): 1,
    ("agents/sdk_runner.py", "sdk_call"): 1,
    ("cli_preflight.py", "other"): 1,
    ("config.py", "other"): 1,
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


def _is_sdk_session_call(node: ast.AST) -> bool:
    """A call of ``query`` or of ``ClaudeSDKClient``, by name or as an
    attribute. ``self.query`` was a Textual query and went with the TUI."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in ("query", "ClaudeSDKClient")
    if isinstance(func, ast.Attribute):
        return func.attr in ("query", "ClaudeSDKClient")
    return False


def _assignments(scope: ast.AST, name: str) -> list[ast.AST]:
    """Each value that ``scope`` gives to ``name``, as a dict key or as a
    keyword of a call. The keywords of a call of the session functions are
    their own arguments, not options, so they are not counted."""
    values: list[ast.AST] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Dict):
            values.extend(
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == name
            )
        if isinstance(node, ast.Call) and not any(
            _is_call_to(node, own) for own in ("claude_session_flags", "claude_session_argv")
        ):
            values.extend(kw.value for kw in node.keywords if kw.arg == name)
    return values


def _sdk_offence(scope: ast.AST | None) -> str | None:
    """The offence of the scope of a ``ClaudeAgentOptions``, or None.

    ``extra_args`` must be a ``claude_session_flags`` call and ``env`` a
    ``claude_session_env`` call (#803). ``settings``, when it is set, must be
    None: claude reads only the last ``--settings`` flag (measured), and the
    SDK writes ``settings`` before ``extra_args``, so a second JSON there
    would be lost or would drop the login."""
    if scope is None:
        return "ClaudeAgentOptions outside a function"
    if not any(_is_call_to(v, "claude_session_flags") for v in _assignments(scope, "extra_args")):
        return "no extra_args=claude_session_flags(...)"
    if not any(_is_call_to(v, "claude_session_env") for v in _assignments(scope, "env")):
        return "no env=claude_session_env()"
    if any(
        not (isinstance(v, ast.Constant) and v.value is None)
        for v in _assignments(scope, "settings")
    ):
        return "settings is not None: the session flags carry the settings"
    return None


def _module_env_offence(tree: ast.AST) -> str | None:
    """A module that spawns claude must pass ``env=claude_session_env()`` to a
    call, so that the session gets the operator's login (#803)."""
    if any(_is_call_to(v, "claude_session_env") for v in _assignments(tree, "env")):
        return None
    return "a claude spawn in a module with no env=claude_session_env()"


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
    return _classify_sdk(node, scope)


def _classify_sdk(node: ast.AST, scope: ast.AST | None) -> tuple[str | None, str | None]:
    """``(kind, offence)`` of one node for the two SDK kinds."""
    if _is_sdk_session_call(node):
        assert isinstance(node, ast.Call)
        carries = any(keyword.arg == "options" for keyword in node.keywords)
        return "sdk_call", None if carries else "SDK session call without options"
    if _names_sdk_options(node):
        return "sdk", _sdk_offence(scope)
    return None, None


def _file_sites(rel: str, tree: ast.AST) -> tuple[list[tuple[str, str]], list[str]]:
    """``(sites, offenders)`` of one parsed file: each site is ``(rel, kind)``."""
    nodes = _nodes_with_scope(tree)
    heads = {
        id(node.elts[0])
        for node, _ in nodes
        if isinstance(node, (ast.List, ast.Tuple)) and _is_spawn(node)
    }
    env_offence = _module_env_offence(tree) if heads else None
    offenders = [f"{rel}: {env_offence}"] if env_offence is not None else []
    sites: list[tuple[str, str]] = []
    for node, scope in nodes:
        kind, offence = _classify(node, scope, heads)
        if kind is not None:
            sites.append((rel, kind))
        if offence is not None:
            offenders.append(f"{rel}:{getattr(node, 'lineno', 0)}: {offence}")
    return sites, offenders


def claude_sites(root: Path) -> tuple[dict[tuple[str, str], int], list[str]]:
    """``(census, offenders)`` for the ``.py`` files under ``root``."""
    census: dict[tuple[str, str], int] = {}
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        sites, found = _file_sites(rel, ast.parse(path.read_text(encoding="utf-8")))
        for site in sites:
            census[site] = census.get(site, 0) + 1
        offenders.extend(found)
    return census, offenders


def test_each_claude_spawn_in_kstrl_carries_the_session_flags() -> None:
    census, offenders = claude_sites(_KSTRL)
    assert census == EXPECTED_CLAUDE_SITES, (
        "the census of claude spawns in kstrl/ changed. Classify each new site: an "
        "argv that starts with 'claude' must hold *claude_session_argv(...), and a "
        "ClaudeAgentOptions must get extra_args=claude_session_flags(...) and "
        "env=claude_session_env(), and an SDK session call must get options.",
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
        "c = 'claude --print'\n"
        "async def d(sdk):\n"
        "    async for m in sdk.query(prompt='x'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "bad_env.py").write_text(
        "def a():\n"
        "    return ['claude', *claude_session_argv('probe', network=False, settings=None)]\n",
        encoding="utf-8",
    )
    (tmp_path / "bad_settings.py").write_text(
        "def b(sdk):\n"
        "    kw = {'extra_args': claude_session_flags('writer', network=True, settings=None)}\n"
        "    return sdk.ClaudeAgentOptions(env=claude_session_env(), settings='{}', **kw)\n",
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text(
        "def a():\n"
        "    cmd = ['claude', *claude_session_argv('probe', network=False, settings=None)]\n"
        "    return run(cmd, env=claude_session_env())\n"
        "def b(sdk):\n"
        "    kw = {'extra_args': claude_session_flags('writer', network=True, settings=None)}\n"
        "    return sdk.ClaudeAgentOptions(env=claude_session_env(), settings=None, **kw)\n"
        "async def d(sdk, options):\n"
        "    async for m in sdk.query(prompt='x', options=options):\n"
        "        pass\n",
        encoding="utf-8",
    )
    census, offenders = claude_sites(tmp_path)
    assert census == {
        ("bad.py", "spawn"): 1,
        ("bad.py", "sdk"): 1,
        ("bad.py", "other"): 1,
        ("bad.py", "sdk_call"): 1,
        ("bad_env.py", "spawn"): 1,
        ("bad_settings.py", "sdk"): 1,
        ("good.py", "spawn"): 1,
        ("good.py", "sdk"): 1,
        ("good.py", "sdk_call"): 1,
    }, census
    # bad.py: the argv, its module's env, the SDK options and the SDK call.
    # bad_env.py: its module's env. bad_settings.py: the settings of the SDK.
    assert [line.split(":")[0] for line in offenders] == [
        *(["bad.py"] * 4),
        "bad_env.py",
        "bad_settings.py",
    ], offenders
