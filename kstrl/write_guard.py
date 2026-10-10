"""The write guard of a claude-code engineer's file tools (#700).

The claude sandbox confines Bash only: with the network open and with it
closed, the Write tool wrote a file in ``$HOME`` (measured with claude
2.1.291). So the claude-code adapter gives claude a PreToolUse command hook,
``<kstrl's interpreter> -I -m kstrl.write_guard <worktree> <writable>...``, which claude
runs before each file tool. The hook exits 2 to block the tool, and claude
obeys that under ``--dangerously-skip-permissions`` too (measured). It
blocks a target outside the worktree and the confirmed ``[stack]``'s
``writable`` paths, and a target with a ``.git``, ``.claude`` or
``.mcp.json`` component under them: claude and git run commands from those
paths outside every sandbox. Measured for ``.claude``: a hook that the Write
tool put in the worktree's ``.claude/settings.local.json`` wrote ``$HOME`` in
the next session. claude lets a tool run when a hook exits with a code other than 2,
so the command ends in ``|| exit 2``: a hook that cannot start, or that
fails, blocks the tool (measured).

``-I`` keeps the worktree, which is the hook's working directory, off
``sys.path``, so a ``kstrl`` package that the engineer writes there is not
the one that runs. The import is stdlib plus :mod:`kstrl.jsonread`, because
the hook starts once for each file tool call.

claude lets a tool run when its hook times out (measured with claude
2.1.291: a hook that slept past its 3 s ``timeout`` let the Write tool write
``$HOME``; ``"onFailure": "block"`` did not change that on 2.1.291, and the
claude documentation names it from 2.1.295). So the guard cannot be slow:
:func:`main` first sets an alarm of :data:`DECISION_DEADLINE_SECONDS` with
the default action, so a guard that has not decided by then ends on
SIGALRM, and ``|| exit 2`` blocks the tool. The hook's own ``timeout``
(:data:`HOOK_TIMEOUT_SECONDS`) is set in the settings so that the claude
default cannot become shorter than the deadline.

A hook that claude does not run at all (a managed policy with
``allowManagedHooksOnly``, or ``disableAllHooks`` in managed settings,
removes the hooks of ``--settings``; measured for ``allowManagedHooksOnly``)
blocks nothing. So the same settings carry a SessionStart hook that prints
:data:`READY_MARKER`, and :class:`SessionGate` stops the session at its
first event that is not a hook event when the marker did not come first.
claude runs the SessionStart hooks before its ``init`` event, and the model
gets the prompt after it (measured), so the stop comes before the first
tool call.

The guard refuses a target path that is not absolute, and it does not read
the ``cwd`` field of the event: the claude file tools take an absolute path,
and a relative path or a ``~`` path could name one file for the guard and
another for claude. Remaining risk, accepted for #700: the check and the
write are two steps. A background Bash process of the engineer can replace
a worktree directory with a symlink between them, and a hook that runs
before the tool cannot close that race. Only an OS sandbox around the whole
claude process can (the #700 design names a known sandbox tool for it).
"""

from __future__ import annotations

import os
import shlex
import signal
import sys
from collections.abc import Iterable
from pathlib import Path

from kstrl.jsonread import read_json

#: The file tools whose target path the guard checks. Bash is absent: the
#: claude sandbox confines it at the OS level (measured).
GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
#: The tool input keys that name the target path.
PATH_KEYS = ("file_path", "notebook_path")
#: Path components the file tools may not write under a root, compared in
#: lower case because the macOS file system ignores case.
PROTECTED_NAMES = frozenset({".git", ".claude", ".mcp.json"})
#: Seconds after which the guard ends on SIGALRM, and so blocks the tool,
#: when it has not decided. Measured: 100 runs of the hook took 0.027 s
#: (median) and 0.038 s (maximum) at a load average of 13 to 15.
DECISION_DEADLINE_SECONDS = 10
#: The ``timeout`` of the hook in the claude settings: longer than the
#: deadline, so the guard ends first.
HOOK_TIMEOUT_SECONDS = 600
#: What the SessionStart hook prints. :class:`SessionGate` keys on it.
READY_MARKER = "kstrl-write-guard-ready"
#: The argument that makes :func:`main` print :data:`READY_MARKER`.
READY_FLAG = "--ready"


def path_refusal(raw_path: str, roots: tuple[Path, ...]) -> str | None:
    """The reason to refuse ``raw_path``, or None when a write to it may run.

    A path that is not absolute is refused before any expansion: the claude
    file tools take an absolute path, and a relative path or a ``~`` path
    that the guard and claude resolve from different places would let the
    guard examine one file while claude writes another. Otherwise the path
    must be under one of ``roots`` with no protected component. ``realpath``
    (not ``resolve(strict=False)`` alone) so that a symlink under a root that
    points out of it cannot carry the write out. ``roots`` are real paths.
    """
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return f"{raw_path} is not an absolute path. Use an absolute path within the worktree."
    resolved = Path(os.path.realpath(candidate))
    if any(
        resolved.is_relative_to(root)
        and not any(part.lower() in PROTECTED_NAMES for part in resolved.relative_to(root).parts)
        for root in roots
    ):
        return None
    return (
        f"{raw_path} is outside the worktree {roots[0]} and the [stack] writable paths, "
        "or it is a .git, .claude or .mcp.json path. Write within the worktree."
    )


def real_roots(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    """The real paths of the worktree, then of each writable path."""
    return tuple(Path(os.path.realpath(path)) for path in paths)


def hook_command(workspace: Path, writable: tuple[str, ...]) -> str:
    """The shell command of the PreToolUse hook for ``workspace``."""
    argv = [sys.executable, "-I", "-m", "kstrl.write_guard", str(workspace), *writable]
    return shlex.join(argv) + " || exit 2"


def ready_command() -> str:
    """The shell command of the SessionStart hook: prints :data:`READY_MARKER`.

    The same interpreter and module as :func:`hook_command`, so the marker
    also shows that the guard can start.
    """
    return shlex.join([sys.executable, "-I", "-m", "kstrl.write_guard", READY_FLAG])


def refusal(event: object, roots: tuple[Path, ...]) -> str | None:
    """The reason to block the tool call in ``event``, or None to let it run.

    This is the one definition of a permitted write: the claude-code hook
    (:func:`main`) and the claude-sdk runner both call it with the same
    roots. A tool call that the guard cannot read, or that names no target
    path, is blocked.
    """
    if not isinstance(event, dict) or not isinstance(event.get("tool_input"), dict):
        return "the write guard cannot read the tool call, so it is blocked"
    tool_input = event["tool_input"]
    paths = [tool_input[key] for key in PATH_KEYS if isinstance(tool_input.get(key), str)]
    if not paths:
        return "the write guard found no target path in the tool call, so it is blocked"
    for raw in paths:
        reason = path_refusal(raw, roots)
        if reason is not None:
            return reason
    return None


def main(argv: list[str]) -> int:
    """Read one PreToolUse event on stdin. Exit 2 to block it, 0 to let it run.

    ``argv`` is the worktree, then each writable path, or :data:`READY_FLAG`
    alone. The alarm comes first, so that a guard that cannot decide in
    :data:`DECISION_DEADLINE_SECONDS` ends on SIGALRM: the wrapper of
    :func:`hook_command` maps that to exit 2.
    """
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(DECISION_DEADLINE_SECONDS)
    if argv == [READY_FLAG]:
        print(READY_MARKER)
        return 0
    if not argv:
        print("the write guard has no worktree, so the tool call is blocked", file=sys.stderr)
        return 2
    reason = refusal(read_json(sys.stdin.read()), real_roots(argv))
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


#: The end of each refusal that :class:`SessionGate` makes before ``init``.
STOPPED = "kstrl stopped the engineer before its first tool call."
#: The refusal when the SessionStart hook did not run before the session.
HOOKS_OFF = (
    "claude started the engineer session without the kstrl write guard: the "
    "SessionStart hook of the kstrl settings did not run. A managed policy with "
    "allowManagedHooksOnly, or disableAllHooks in managed settings, removes the "
    "hooks of --settings, and then the file tools can write outside the worktree. "
    f"{STOPPED} Remove that policy for this machine or user, or set [sandbox] "
    "enabled = false to run the engineer with no sandbox."
)


class SessionGate:
    """Stops a claude session that runs without the write guard, or with MCP.

    Give it each stream-json line of the session in order. Before the first
    event that is not a hook event, the SessionStart hook of
    :func:`ready_command` must report :data:`READY_MARKER`, and the first
    such event must be the ``init`` event with an empty ``mcp_servers``
    list: an MCP tool can write a file, and the guard does not see it. The
    adapter passes ``--strict-mcp-config`` so claude loads no MCP server
    (measured), and the gate refuses a session that has one anyway (a
    managed policy can add servers). An event that the gate cannot read
    before ``init`` is a refusal. A line that is not JSON is not an event,
    and the gate lets it pass. Remaining risk: a claude that sends no
    ``init`` event is stopped at its first other event, which can be the
    ``tool_use`` event of a tool call, and then the stop races that tool.
    """

    def __init__(self) -> None:
        self._ready = False
        self._started = False

    def refusal(self, raw_line: str) -> str | None:
        """The reason to stop the session at ``raw_line``, or None."""
        if self._started:
            return None
        try:
            event = read_json(raw_line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return f"the engineer session sent an event that kstrl cannot read. {STOPPED}"
        if event.get("type") == "system" and event.get("subtype") != "init":
            if event.get("hook_event") == "SessionStart" and READY_MARKER in str(
                event.get("stdout", "")
            ):
                self._ready = True
            return None
        if not self._ready:
            return HOOKS_OFF
        if event.get("type") != "system":
            return (
                "the engineer session sent no init event, so kstrl cannot see its MCP "
                "servers. kstrl stopped the engineer at its first event after the hooks."
            )
        self._started = True
        return _mcp_refusal(event.get("mcp_servers"))


def _mcp_refusal(servers: object) -> str | None:
    """The refusal for the ``mcp_servers`` of an ``init`` event, or None."""
    if not isinstance(servers, list):
        return f"the init event of the engineer session has no mcp_servers list. {STOPPED}"
    if not servers:
        return None
    names = ", ".join(
        str(server.get("name")) if isinstance(server, dict) else repr(server) for server in servers
    )
    return (
        f"the engineer session loaded MCP servers ({names}) although kstrl passed "
        "--strict-mcp-config. An MCP tool can write outside the worktree, and the "
        "write guard does not see it. A managed policy can add servers "
        f"(managed-mcp.json or managedMcpServers). {STOPPED}"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
