"""The one-run-for-each-checkout lock: acquire, refuse, hand on and release it."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

from kstrl.worktree_sweep import leftover_agents, leftover_lines

if TYPE_CHECKING:
    from kstrl.ui.base import UI


class FactoryLockHeldError(RuntimeError):
    """Another factory invocation holds the run-level lock on this root."""


class LeftoverAgentsError(FactoryLockHeldError):
    """Agent processes a kstrl process that is gone started are still running (#642).

    A subclass so every caller's exit-2 path for a held lock refuses this
    too. Its message does not name ``--force-lock``, which `ks serve`
    reads as lock contention and retries: this needs an operator.
    """


def _refuse_leftover_agents(root_dir: Path, fp: IO[str]) -> None:
    """Release the run lock and raise when :func:`leftover_agents` finds
    anything, or cannot look (#642, owner decision 4 (b)).

    Taken after the flock, so no run of ours starts an agent between the
    reading and the refusal. Nothing is signalled: the message names each
    process and the command that stops its group.
    """
    sweep = leftover_agents(root_dir)
    if not (sweep.survivors or sweep.error):
        return
    fp.close()
    raise LeftoverAgentsError(
        "\n  ".join(
            [
                "Refusing to start: agent processes started by a kstrl process that "
                "is no longer running are still running in this project, or could "
                "not be checked, and a new run would share the tree with them. "
                "Nothing was changed.",
                *leftover_lines(sweep),
            ]
        )
    )


@dataclass
class _RunLock:
    """Handle for the run-level factory lock.

    ``held=True`` means we hold an exclusive flock for the whole run and
    may safely prune state left by previous runs. ``held=False`` means we
    are running WITHOUT exclusion (Windows/no-fcntl degrade, or
    ``--force-lock``): stale-state cleanup must be skipped because another
    live invocation may own it. ``released`` is separate from ``fp is
    None``: a forced or no-fcntl lock also has ``fp=None`` while still
    legitimate to hand to `ks factory`, so ``released`` is the only field
    that says whether ``release()`` has run on this handle (#597).
    """

    fp: IO[str] | None
    held: bool
    released: bool = False

    def release(self) -> None:
        if self.fp is None:
            self.held = False
            self.released = True
            return
        try:
            import fcntl

            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self.fp.close()
        self.fp = None
        self.held = False
        self.released = True


def _acquire_run_lock(root_dir: Path, ui: UI, force: bool) -> _RunLock:
    """Take the run-level flock on ``.kstrl/factory.lock`` (R0.5, H-7).

    Held for the entire run so a second ``ks factory`` / ``ks run``
    on the same root refuses to start instead of destroying the first
    invocation's in-flight worktrees and clobbering its manifest. flock
    releases automatically if the holder dies, so a crashed run never
    wedges the root.

    POSIX only, like the A4 per-component lock: without fcntl we degrade
    to no exclusion with a warning. ``force=True`` proceeds past a held
    lock with a warning instead of raising FactoryLockHeldError.
    """
    from kstrl.statedir import state_dir

    lock_path = state_dir(root_dir) / "factory.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        ui.warn(
            "Run-level factory lock unavailable on this platform (no "
            "fcntl); concurrent invocations on this root are not excluded"
        )
        return _RunLock(fp=None, held=False)

    # "a+" so a refused attempt can read the holder's pid without
    # truncating it.
    fp = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            fp.seek(0)
            holder = fp.read(64).strip()
        except (OSError, UnicodeDecodeError):
            # ONE clause: ``holder`` stays "" either way, the refusal
            # message below just omits the pid, and nothing reports the
            # cause - so #320's separate-remedy rule has no message to
            # separate. What matters is that the decode is caught at
            # all: the read is on a TEXT handle, so a lock file holding
            # anything but utf-8 raised a UnicodeDecodeError - a
            # ValueError - straight past this OSError clause and turned
            # a "another run holds the lock" refusal into a traceback.
            # A census of read CALLS cannot see this site; the handle
            # walk in tests/helpers/encodingwalk.py found it, and it is
            # the only one of its shape in the package.
            pass
        fp.close()
        holder_note = f" (pid {holder})" if holder else ""
        if force:
            ui.warn(
                f"--force-lock: proceeding while {lock_path} is held"
                f"{holder_note}; concurrent runs can corrupt each "
                f"other's worktrees and manifest"
            )
            return _RunLock(fp=None, held=False)
        raise FactoryLockHeldError(
            f"Another kstrl invocation{holder_note} holds {lock_path}; "
            f"refusing to start a second factory run on this root. "
            f"Wait for it to finish, or re-run with --force-lock to "
            f"override."
        ) from None

    _refuse_leftover_agents(root_dir, fp)
    # Holder pid is diagnostic only (shown in the refusal message of a
    # contending invocation); the flock itself is the exclusion.
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
    except OSError:
        pass
    return _RunLock(fp=fp, held=True)


@contextmanager
def held_or_acquired_run_lock(
    run_lock: _RunLock | None, root_dir: Path, ui: UI, force: bool
) -> Iterator[_RunLock]:
    """Use ``run_lock`` if the caller already holds one; acquire and release our own otherwise.

    `ks factory --spec` takes the lock before it calls ``decompose_spec``
    (#597) and keeps holding it after this returns, so a caller passing
    its own lock keeps owning it: this never releases one it did not
    acquire. `ks decompose`'s own case passes None and gets a lock this
    acquires and releases around exactly the block it wraps.
    """
    owns = run_lock is None
    lock = run_lock if run_lock is not None else _acquire_run_lock(root_dir, ui, force=force)
    try:
        yield lock
    finally:
        if owns:
            lock.release()
