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

    ``argv`` is the worktree, then each writable path.
    """
    if not argv:
        print("the write guard has no worktree, so the tool call is blocked", file=sys.stderr)
        return 2
    reason = refusal(read_json(sys.stdin.read()), real_roots(argv))
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
