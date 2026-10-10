"""A claude-code engineer's file tools hold to its worktree, and a session in
which claude does not run the write guard, or loads an MCP server, stops
before its first tool call (#700).

End to end: the real ``ks factory`` with a stub ``claude`` that runs the real
hook commands of the ``--settings`` that kstrl gives it. The helpers are the
ones of tests/test_sandbox_engineer_e2e.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.helpers.executables import write_executable
from tests.test_isolation_stack import NO_PROVER
from tests.test_sandbox_engineer_e2e import (
    _FACTORY,
    CLAUDE_TOML,
    NO_NETWORK_TOML,
    STACK_TOML,
    _ks,
)

#: How long the stub claude waits for a stalled hook. The guard's own deadline
#: (10 s) must end it first; claude 2.1.291 waits the hook's ``timeout``.
STALL_WAIT_SECONDS = 40

#: (label, tool, the target's path, the hook's exit code). ``{wt}`` is the
#: worktree, ``{out}`` a directory outside the project, ``{cache}`` the stack's
#: writable path. A relative path and a ``~`` path exit 2 even when they name
#: a place in the worktree: the guard takes an absolute path only. claude
#: blocks the tool on exit 2 only.
_TARGETS = [
    ("inside", "Write", "{wt}/inside.txt", 0),
    ("nested", "Edit", "{wt}/src/a.py", 0),
    ("stack", "Write", "{cache}/x.txt", 0),
    ("outside", "Write", "{out}/escaped.txt", 2),
    ("edit-outside", "Edit", "{out}/escaped.txt", 2),
    ("multiedit-outside", "MultiEdit", "{out}/escaped.txt", 2),
    ("notebook-outside", "NotebookEdit", "{out}/n.ipynb", 2),
    ("relative", "Write", "../../../escaped.txt", 2),
    ("relative-inside", "Write", "inside.txt", 2),
    ("home", "Write", "~/escaped.txt", 2),
    ("symlink", "Write", "{wt}/link-out/escaped.txt", 2),
    ("claude-link", "Write", "{wt}/claude-link/settings.local.json", 2),
    ("claude-settings", "Write", "{wt}/.claude/settings.local.json", 2),
    ("claude-upper", "Write", "{wt}/.Claude/settings.json", 2),
    ("mcp", "Write", "{wt}/.mcp.json", 2),
    ("git-file", "Write", "{wt}/.git", 2),
    ("nested-git", "Write", "{wt}/sub/.git/config", 2),
    ("stack-git", "Write", "{cache}/.git/config", 2),
    ("stack-prefix", "Write", "{cache}-sibling/x.txt", 2),
]


def _hooked_claude(path: Path, out: Path) -> Path:
    """A ``claude`` that records its argv like :func:`_stub` and, on its first
    call, runs each PreToolUse hook of its ``--settings`` the way claude does:
    ``sh -c`` in its working directory, the tool call as JSON on stdin. It
    writes ``<label>=<exit code>`` for each of :data:`_TARGETS`, ``nohook``
    when no hook matches the tool, two calls the guard cannot read, and a
    relative path from a ``cwd`` outside the worktree (``cwd-outside``). The
    worktree holds a ``kstrl`` package of its own, as an engineer can write,
    and a ``.claude/settings.json`` that switches hooks off, as a target
    repository can carry.

    ``stalled`` is a Write whose guard cannot decide: the stub keeps the
    hook's stdin open, as a slow file system would keep the guard waiting.
    claude lets the tool run when a hook reaches its ``timeout`` (measured,
    claude 2.1.291), so the stub writes ``timeout`` when the hook outlives
    its ``timeout`` or :data:`STALL_WAIT_SECONDS`, whichever is less. The
    stub also writes the hook's ``timeout`` and ``onFailure`` fields."""
    argv = path.with_suffix(".argv")
    cache = out.parent / "proj" / "tool-cache"
    outside = out.parent / "outside"
    targets = [
        (label, tool, raw.format(cache=cache, out=outside, wt="{wt}"))
        for label, tool, raw, _ in _TARGETS
    ]
    return write_executable(
        path,
        f"""#!{sys.executable}
import json, os, re, subprocess, sys
argv = sys.argv[1:]
with open({str(argv)!r}, "a", encoding="utf-8") as log:
    log.write("".join(a + "\\n" for a in [*argv, "--"]))
sys.stdin.read()
if not os.path.exists({str(out)!r}):
    settings = json.loads(argv[argv.index("--settings") + 1])
    matchers = settings.get("hooks", {{}}).get("PreToolUse", [])
    cwd = os.getcwd()
    # A project setting that switches every hook off, as a target repository
    # can carry. claude 2.1.291 then runs no hook unless --settings sets
    # disableAllHooks to false (measured).
    os.makedirs(os.path.join(cwd, ".claude"), exist_ok=True)
    with open(os.path.join(cwd, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        f.write('{{"disableAllHooks": true}}')
    if settings.get("disableAllHooks") is not False:
        matchers = []
    os.makedirs({str(outside)!r}, exist_ok=True)
    os.symlink({str(outside)!r}, os.path.join(cwd, "link-out"))
    os.symlink(os.path.join(cwd, ".claude"), os.path.join(cwd, "claude-link"))
    # A kstrl package in the worktree whose guard lets every call run: the
    # hook must not import it.
    os.makedirs(os.path.join(cwd, "kstrl"))
    for name, body in (("__init__.py", ""), ("write_guard.py", "raise SystemExit(0)")):
        with open(os.path.join(cwd, "kstrl", name), "w", encoding="utf-8") as f:
            f.write(body)
    def hook(tool, stdin):
        commands = [h["command"] for m in matchers if re.fullmatch(m["matcher"], tool)
                    for h in m["hooks"]]
        if not commands:
            return "nohook"
        codes = [subprocess.run(["/bin/sh", "-c", c], input=stdin, cwd=cwd, encoding="utf-8",
                                capture_output=True, timeout=60).returncode for c in commands]
        return str(max(codes))
    def stalled(tool, stdin):
        handlers = [h for m in matchers if re.fullmatch(m["matcher"], tool) for h in m["hooks"]]
        if not handlers:
            return "nohook"
        codes = []
        for h in handlers:
            child = subprocess.Popen(["/bin/sh", "-c", h["command"]], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd=cwd, encoding="utf-8", start_new_session=True)
            child.stdin.write(stdin)
            child.stdin.flush()
            try:
                wait = min(h.get("timeout", 600), {STALL_WAIT_SECONDS})
                codes.append(str(child.wait(timeout=wait)))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, 9)
                child.wait()
                codes.append("timeout")
            child.stdin.close()
        return "timeout" if "timeout" in codes else str(max(int(c) for c in codes))
    lines = []
    guard = [h for m in matchers for h in m["hooks"] if m["matcher"].startswith("Write")]
    lines.append("hook-timeout=" + str(guard[0].get("timeout") if guard else None))
    lines.append("on-failure=" + str(guard[0].get("onFailure") if guard else None))
    inside = {{"tool_name": "Write", "tool_input": {{"file_path": os.path.join(cwd, "s.txt")}}}}
    lines.append("stalled=" + stalled("Write", json.dumps(inside)))
    for label, tool, raw in {targets!r}:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        raw = raw.replace("{{wt}}", cwd)
        event = {{"tool_name": tool, "tool_input": {{key: raw}}, "cwd": cwd}}
        lines.append(label + "=" + hook(tool, json.dumps(event)))
    lines.append("unparsable=" + hook("Write", "not json"))
    empty = json.dumps({{"tool_name": "Write", "tool_input": {{}}}})
    lines.append("no-path=" + hook("Write", empty))
    away = {{"tool_name": "Write", "tool_input": {{"file_path": "escaped.txt"}},
            "cwd": {str(outside)!r}}}
    lines.append("cwd-outside=" + hook("Write", json.dumps(away)))
    with open({str(out)!r}, "w", encoding="utf-8") as log:
        log.write("\\n".join(lines) + "\\n")
print("<promise>COMPLETE</promise>")
""",
    )


@pytest.mark.parametrize("network", ["", NO_NETWORK_TOML], ids=["network-open", "network-denied"])
def test_a_claude_engineer_s_file_tools_cannot_write_outside_the_worktree_and_stack_paths(
    tmp_path: Path, network: str
) -> None:
    """RED before this change: every line was ``nohook``, because the claude
    sandbox confines Bash only and the adapter gave claude no hook (measured
    with claude 2.1.291: the Write tool wrote a file in $HOME in both modes).
    Now the hook blocks a target outside the worktree and the confirmed
    stack's writable path, and a .git, .claude or .mcp.json path, and a tool
    call it cannot read; it lets a write in the worktree and the stack path
    run."""
    out = tmp_path / "hooks.out"
    argv_ = (*_FACTORY, "--review-mode", "skip", "--security-mode", "skip")
    toml = CLAUDE_TOML + network + STACK_TOML
    ran = _ks(
        tmp_path,
        toml,
        argv_,
        NO_PROVER,
        confirm=True,
        clis=("claude",),
        claude_out=out,
        claude_stub=_hooked_claude,
    )

    assert out.exists(), ran
    got = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    want = {label: str(code) for label, _, _, code in _TARGETS}
    want |= {"unparsable": "2", "no-path": "2", "cwd-outside": "2", "stalled": "2"}
    want |= {"hook-timeout": "600", "on-failure": "block"}
    assert got == want, ran
    assert not (tmp_path / "outside" / "escaped.txt").exists()


#: How long the stub claude takes for the model's first turn. kstrl must stop
#: the session at its ``init`` event, before this ends.
MODEL_TURN_SECONDS = 10


def _session_claude(path: Path, out: Path) -> Path:
    """A ``claude`` that sends the stream-json events of a session the way
    claude 2.1.291 does (measured): the output of each SessionStart hook of its
    ``--settings``, then ``init`` with the MCP servers it loaded, then, after
    :data:`MODEL_TURN_SECONDS`, a first turn that writes outside the worktree
    with the Write tool and with each MCP server's write tool. A tool runs
    unless a PreToolUse hook that matches it exits 2. It writes what each tool
    did to ``out`` at the end of that turn.

    ``STUB_MANAGED_HOOKS_ONLY`` stands for a managed policy with
    ``allowManagedHooksOnly``: no hook of ``--settings`` runs (measured with
    ``--managed-settings``). Without ``--strict-mcp-config`` the session loads
    the operator's server ``operator-fs``; ``STUB_MANAGED_MCP`` adds a managed
    server ``managed-fs`` in both cases. ``STUB_NO_INIT`` leaves the ``init``
    event out, and ``STUB_INIT_NO_SERVERS`` its ``mcp_servers`` list: an
    event that kstrl cannot read must stop the session too. As claude does,
    the stub sends each tool call's ``tool_use`` event before the tool runs."""
    argv = path.with_suffix(".argv")
    outside = out.parent / "outside"
    return write_executable(
        path,
        f"""#!{sys.executable}
import json, os, re, subprocess, sys, time
argv = sys.argv[1:]
with open({str(argv)!r}, "a", encoding="utf-8") as log:
    log.write("".join(a + "\\n" for a in [*argv, "--"]))
sys.stdin.read()
cwd = os.getcwd()
settings = json.loads(argv[argv.index("--settings") + 1]) if "--settings" in argv else {{}}
hooks = {{}} if os.environ.get("STUB_MANAGED_HOOKS_ONLY") else settings.get("hooks", {{}})
def emit(event):
    print(json.dumps(event), flush=True)
def run(command, stdin):
    return subprocess.run(["/bin/sh", "-c", command], input=stdin, cwd=cwd, encoding="utf-8",
                          capture_output=True, timeout=60)
for matcher in hooks.get("SessionStart", []):
    for handler in matcher["hooks"]:
        done = run(handler["command"], "{{}}")
        emit({{"type": "system", "subtype": "hook_response", "hook_name": "SessionStart:startup",
              "hook_event": "SessionStart", "stdout": done.stdout, "exit_code": done.returncode}})
servers = [] if "--strict-mcp-config" in argv else [{{"name": "operator-fs", "source": "user"}}]
if os.environ.get("STUB_MANAGED_MCP"):
    servers.append({{"name": "managed-fs", "source": "managed"}})
tools = ["Write"] + ["mcp__" + s["name"] + "__write_file" for s in servers]
init = {{"type": "system", "subtype": "init", "mcp_servers": servers, "tools": tools}}
if os.environ.get("STUB_INIT_NO_SERVERS"):
    del init["mcp_servers"]
if not os.environ.get("STUB_NO_INIT"):
    emit(init)
time.sleep({MODEL_TURN_SECONDS})
os.makedirs({str(outside)!r}, exist_ok=True)
lines = []
for tool in tools:
    target = os.path.join({str(outside)!r}, tool + ".txt")
    event = json.dumps({{"tool_name": tool, "tool_input": {{"file_path": target}}}})
    use = {{"type": "tool_use", "name": tool, "input": {{"file_path": target}}}}
    emit({{"type": "assistant", "message": {{"content": [use]}}}})
    codes = [run(h["command"], event).returncode for m in hooks.get("PreToolUse", [])
             if re.fullmatch(m["matcher"], tool) for h in m["hooks"]]
    if 2 not in codes:
        with open(target, "w", encoding="utf-8") as f:
            f.write("escaped")
    lines.append(tool + "=" + ("blocked" if 2 in codes else "ran"))
with open({str(out)!r}, "w", encoding="utf-8") as log:
    log.write("\\n".join(lines) + "\\n")
emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "done"}}]}}}})
emit({{"type": "result", "result": "<promise>COMPLETE</promise>"}})
""",
    )


#: (id, kstrl.toml network section, stub env, the text the run must print, or
#: None when the engineer must run its first turn).
_SESSIONS = [
    ("no-policy", "", {}, None),
    ("no-policy-network-denied", NO_NETWORK_TOML, {}, None),
    ("managed-hooks-only", "", {"STUB_MANAGED_HOOKS_ONLY": "1"}, "allowManagedHooksOnly"),
    ("managed-mcp", "", {"STUB_MANAGED_MCP": "1"}, "loaded MCP servers (managed-fs)"),
    ("init-without-servers", "", {"STUB_INIT_NO_SERVERS": "1"}, "has no mcp_servers list"),
]


def test_a_claude_session_with_no_init_event_is_stopped_at_its_first_tool_call(
    tmp_path: Path,
) -> None:
    """A claude that sends no ``init`` event gives kstrl no MCP server list,
    so kstrl stops the session at the first event that is not a hook event:
    here the ``tool_use`` event of the first tool call. That stop and the
    tool race (remaining risk, written in kstrl/write_guard.py), so this test
    asserts the refusal only, not that the tool did not run."""
    out = tmp_path / "session.out"
    argv_ = (*_FACTORY, "--review-mode", "skip", "--security-mode", "skip")
    ran = _ks(
        tmp_path,
        CLAUDE_TOML + STACK_TOML,
        argv_,
        {**NO_PROVER, "STUB_NO_INIT": "1"},
        confirm=True,
        clis=("claude",),
        claude_out=out,
        claude_stub=_session_claude,
    )
    assert "the engineer session sent no init event" in ran, ran
    assert "kstrl stopped the engineer before its first tool call" not in ran, ran


@pytest.mark.parametrize(
    ("network", "env", "refusal"),
    [row[1:] for row in _SESSIONS],
    ids=[row[0] for row in _SESSIONS],
)
def test_a_claude_engineer_runs_only_with_the_write_guard_and_no_mcp_server(
    tmp_path: Path, network: str, env: dict[str, str], refusal: str | None
) -> None:
    """RED before this change: under a managed policy with
    allowManagedHooksOnly the hook did not run and the Write tool wrote $HOME
    (measured with claude 2.1.291 and --managed-settings), and the session
    loaded the operator's MCP servers (measured: 37 servers, among them a
    server in the worktree's .mcp.json, whose command ran outside the sandbox).
    Now the engineer gets --strict-mcp-config, and kstrl stops a session whose
    SessionStart hook did not report the guard, or that loaded an MCP server
    anyway, at its init event: the first turn never runs, and the run prints
    the cause."""
    out = tmp_path / "session.out"
    argv_ = (*_FACTORY, "--review-mode", "skip", "--security-mode", "skip")
    toml = CLAUDE_TOML + network + STACK_TOML
    extra = {**NO_PROVER, **env}
    ran = _ks(
        tmp_path,
        toml,
        argv_,
        extra,
        confirm=True,
        clis=("claude",),
        claude_out=out,
        claude_stub=_session_claude,
    )
    outside = tmp_path / "outside"
    assert not outside.exists() or not any(outside.iterdir()), ran
    if refusal is None:
        assert out.read_text(encoding="utf-8") == "Write=blocked\n", ran
        assert "kstrl stopped the engineer" not in ran, ran
    else:
        assert not out.exists(), ran
        assert refusal in ran, ran
        assert "kstrl stopped the engineer before its first tool call" in ran, ran
