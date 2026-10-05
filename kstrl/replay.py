"""The clean replay of a ``[stack]``'s recipe (#700 slice 3).

``ks doctor --measure`` replays the recipe the way a later run will need
it: in a throwaway worktree of the base commit, inside both proven zones
of the isolation rung, with the stack's own commands and nothing else.
The stages run in order and the first one that fails ends the replay:

1. ``setup`` in the setup zone;
2. ``up`` in the test zone, through :func:`kstrl.verify.start_scrubbed`:
   ready means it exited 0, and a timeout is never ready;
3. every check in the test zone, in the stack's order;
4. the ``up`` process group is stopped by the group id recorded when it
   started, then the worktree is removed
   (:func:`kstrl.contract._remove_temp_worktree`).

kstrl never reads what ``up`` does: only its exit status and whether its
group is gone afterwards. The stage a replay failed at is named in the
vocabulary :mod:`kstrl.stack` owns (``REPLAY_*``), because a failure
keeps the stack from being confirmed. Below a proven rung nothing runs and
the replay is ``boundary_refused``.

Replays on one machine are serialised by :func:`replay_lock`: two
applications started at once would contend for the same ports.
"""

from __future__ import annotations

import fcntl
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git
from kstrl.contract import ContractCleanupError, _create_temp_worktree, _remove_temp_worktree
from kstrl.isolation import SETUP_ZONE, TEST_ZONE, prove_zones
from kstrl.procdispose import reap_or_abandon
from kstrl.procgroup import read_group_liveness, signal_group
from kstrl.rung import ProvenRung, release
from kstrl.stack import (
    REPLAY_BASE_CONTRADICTION,
    REPLAY_BOUNDARY_REFUSED,
    REPLAY_CHECK_NOT_RUNNABLE,
    REPLAY_SETUP_FAILED,
    REPLAY_UP_FAILED,
    REPLAY_UP_TIMEOUT,
    Stack,
    stack_paths,
)
from kstrl.statedir import CONTROL_APP_NAME, xdg_state_home
from kstrl.verify import (
    SHELL_COULD_NOT_RUN,
    ChildOutputDecodeError,
    _readable,
    run_scrubbed,
    start_scrubbed,
)

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: The label of the throwaway worktree, under ``.kstrl/contract/``.
WORKTREE_LABEL = "replay"

#: The per-machine lock file, under the XDG state home beside every
#: repository's control directory.
REPLAY_LOCK_FILE = "replay.lock"

#: How many of a stage's last output lines its record keeps: the count
#: ``verify.check_stack_command`` prints for a failed check.
TAIL_LINES = 5

#: Seconds the ``up`` group gets after SIGTERM, then after SIGKILL: the
#: grace ``verify.run_scrubbed`` gives a timed-out group.
STOP_GRACE_SECONDS = 5.0

#: Seconds between two reads of whether the ``up`` group is gone.
POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Stage:
    """One command of the replay. ``exit`` is None when it gave none, and
    ``stopped`` says why ("timed out", "output not utf-8"). ``failed`` is
    the replay's failure this stage is, or ""."""

    name: str
    command: str
    exit: int | None
    stopped: str
    seconds: float
    tail: tuple[str, ...]
    failed: str = ""


@dataclass
class Replay:
    """The record of one replay, filled in as it runs.

    ``failed`` is the stage it failed at, "" when every stage passed.
    ``error`` is why kstrl could not set the replay up or clean it up
    (the base did not resolve, the checkout or its removal failed): that
    is kstrl's failure, not the recipe's, and it never reads as a pass.
    ``pgid`` is the ``up`` group's id, recorded when it started, and
    ``group_gone`` whether a read after the stop found it empty."""

    stack_digest: str
    base_branch: str
    base_sha: str = ""
    isolation: dict[str, str] = field(default_factory=dict)
    lock_wait_seconds: float = 0.0
    stages: list[Stage] = field(default_factory=list)
    pgid: int | None = None
    group_gone: bool | None = None
    failed: str = ""
    detail: str = ""
    error: str = ""
    seconds: float = 0.0


@contextmanager
def replay_lock(ui: UI) -> Iterator[float]:
    """Hold the per-machine replay lock; yield the seconds spent waiting."""
    path = xdg_state_home() / CONTROL_APP_NAME / REPLAY_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ui.info(f"Waiting for another replay on this machine to finish: {path}")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield round(time.monotonic() - started, 3)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def replay_stack(
    root: Path,
    stack: Stack,
    *,
    setup_limit: float | None,
    check_limit: float | None,
    ui: UI,
) -> Replay:
    """Replay ``stack``'s recipe on the base branch; never raises for a
    stage that failed. ``setup_limit`` bounds the setup, ``check_limit``
    bounds ``up`` and each check (None waits with no limit, #467)."""
    started = time.monotonic()
    record = Replay(stack.digest, git.detect_base_branch(root))
    try:
        with replay_lock(ui) as waited:
            record.lock_wait_seconds = waited
            _replay_in_worktree(root, stack, record, (setup_limit, check_limit), ui)
    finally:
        record.seconds = round(time.monotonic() - started, 3)
    return record


def _replay_in_worktree(
    root: Path,
    stack: Stack,
    record: Replay,
    limits: tuple[float | None, float | None],
    ui: UI,
) -> None:
    """Check the base out into a throwaway worktree, replay inside it, and
    remove it whatever happened."""
    try:
        record.base_sha = git.resolve_base_sha(record.base_branch, root)
    except git.GitDiffError as exc:
        record.error = f"the base branch {record.base_branch} did not resolve: {exc}"
        return
    worktree, checkout_error = _create_temp_worktree(record.base_sha, root, WORKTREE_LABEL)
    if worktree is None:
        record.error = f"the base {record.base_sha[:12]} was not checked out: {checkout_error}"
        return
    scratch = Path(tempfile.mkdtemp(prefix="kstrl-replay-"))
    try:
        _replay_in_rungs(root, stack, worktree, scratch, record, limits)
    finally:
        try:
            _remove_temp_worktree(worktree, root, ui, WORKTREE_LABEL)
        except ContractCleanupError as exc:
            record.error = record.error or f"the replay worktree survived: {exc}"
        shutil.rmtree(scratch, ignore_errors=True)


def _replay_in_rungs(
    root: Path,
    stack: Stack,
    worktree: Path,
    scratch: Path,
    record: Replay,
    limits: tuple[float | None, float | None],
) -> None:
    """Prove both zones with the worktree and the stack's own paths granted,
    and run the stages only when both hold."""
    rungs = prove_zones(
        root,
        [worktree, *stack_paths(root, stack.writable)],
        stack_paths(root, stack.readable),
        stack.browser,
    )
    try:
        record.isolation = {zone: rung.refusal or rung.label for zone, rung in rungs.items()}
        refused = [
            f"the {zone} zone is {rung.refusal}" for zone, rung in rungs.items() if rung.refusal
        ]
        if refused:
            record.failed, record.detail = REPLAY_BOUNDARY_REFUSED, "; ".join(refused)
            return
        _run_stages(stack, worktree, scratch, rungs, record, limits)
    finally:
        release(rungs.values())


def _run_stages(
    stack: Stack,
    worktree: Path,
    scratch: Path,
    rungs: Mapping[str, ProvenRung],
    record: Replay,
    limits: tuple[float | None, float | None],
) -> None:
    """setup, up, every check, in that order, until one fails; then stop
    the ``up`` group by the id recorded when it started."""
    setup_limit, check_limit = limits
    app: subprocess.Popen[bytes] | None = None
    try:
        if stack.setup and not _passed(
            record, _ran("setup", stack.setup, worktree, rungs[SETUP_ZONE], setup_limit, stack)
        ):
            return
        if stack.up:
            log = scratch / "up.log"
            began = time.monotonic()
            with log.open("wb") as handle:
                app = start_scrubbed(
                    stack.up,
                    cwd=worktree,
                    rung=rungs[TEST_ZONE],
                    log=handle,
                    declared_env=stack.env,
                )
            record.pgid = app.pid
            if not _passed(record, _ready(app, stack.up, check_limit, log, began)):
                return
        for name, command in stack.checks:
            check = _ran(f"check:{name}", command, worktree, rungs[TEST_ZONE], check_limit, stack)
            if not _passed(record, check):
                return
    finally:
        if app is not None and record.pgid is not None:
            record.group_gone = _stop(app, record.pgid)


def _passed(record: Replay, stage: Stage) -> bool:
    record.stages.append(stage)
    if not stage.failed:
        return True
    said = f"exited {stage.exit}" if stage.exit is not None else stage.stopped
    lines = " | ".join(stage.tail) or "(no output)"
    record.failed = stage.failed
    record.detail = f"{stage.name} `{stage.command}` {said}; last lines: {lines}"
    return False


def _tail(text: str) -> tuple[str, ...]:
    return tuple(text.strip().splitlines()[-TAIL_LINES:])


def _ran(
    name: str, command: str, cwd: Path, rung: ProvenRung, limit: float | None, stack: Stack
) -> Stage:
    """Run one setup or check command to its end, named by what it gave."""
    began = time.monotonic()
    try:
        done = run_scrubbed(command, cwd=cwd, timeout=limit, declared_env=stack.env, rung=rung)
    except subprocess.TimeoutExpired as expired:
        output = _readable(expired.stdout) + _readable(expired.stderr)
        code, stopped, given = None, f"timed out after {limit}s", "timeout"
    except ChildOutputDecodeError as exc:
        output, code, stopped, given = (
            exc.stdout + exc.stderr,
            None,
            "output not utf-8",
            "undecodable",
        )
    else:
        output, code, stopped, given = done.stdout + done.stderr, done.returncode, "", ""
    if name == "setup":
        failed = "" if code == 0 else f"{REPLAY_SETUP_FAILED}:{given or code}"
    elif code == 0:
        failed = ""
    elif code is None or code in SHELL_COULD_NOT_RUN:
        failed = REPLAY_CHECK_NOT_RUNNABLE
    else:
        failed = REPLAY_BASE_CONTRADICTION
    return Stage(
        name, command, code, stopped, round(time.monotonic() - began, 3), _tail(output), failed
    )


def _ready(
    app: subprocess.Popen[bytes], command: str, limit: float | None, log: Path, began: float
) -> Stage:
    """Wait for ``up`` to exit: 0 is ready, anything else failed, and a
    timeout is never ready."""
    try:
        code: int | None = app.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        code = None
    if code is None:
        failed, stopped = REPLAY_UP_TIMEOUT, f"was not ready after {limit}s"
    else:
        failed, stopped = ("" if code == 0 else f"{REPLAY_UP_FAILED}:{code}"), ""
    tail = _tail(_readable(log.read_bytes()))
    return Stage("up", command, code, stopped, round(time.monotonic() - began, 3), tail, failed)


def _stop(app: subprocess.Popen[bytes], pgid: int) -> bool:
    """SIGTERM the ``up`` group, then SIGKILL what is left after the grace;
    True when a read after it finds the group empty."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        signal_group(pgid, sig)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while read_group_liveness(pgid).live is not False and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        if read_group_liveness(pgid).live is False:
            break
    reap_or_abandon(app, STOP_GRACE_SECONDS)
    return read_group_liveness(pgid).live is False
