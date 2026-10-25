"""Where the agent processes started inside a block keep their temporary files.

The verification designer writes held-out checks and a reference
implementation of its own, and the claude CLI keeps a session scratchpad
under the system temporary directory: a user run's designer left both there,
outside the scratch checkout kstrl deletes, where the engineer's user could
read them. claude puts that scratchpad under ``TMPDIR`` when it is set
(measured, claude 2.1.310), as most tools put their temporary files, so a
block that names a directory kstrl deletes keeps them inside it. The
adapters read the directory here when they start a process, the way
:mod:`kstrl.agents.prompt_record` reads the call being recorded.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_TMPDIR: ContextVar[Path | None] = ContextVar("kstrl_agent_tmpdir", default=None)


@contextmanager
def agent_tmpdir(path: Path) -> Iterator[None]:
    """Give every agent process started inside this block ``path`` as its
    ``TMPDIR``, creating it. Removing it is the caller's."""
    path.mkdir(parents=True, exist_ok=True)
    token = _TMPDIR.set(path)
    try:
        yield
    finally:
        _TMPDIR.reset(token)


def agent_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` for an agent process, with ``TMPDIR`` when a block set one."""
    out = dict(env or {})
    path = _TMPDIR.get()
    if path is not None:
        out["TMPDIR"] = str(path)
    return out
