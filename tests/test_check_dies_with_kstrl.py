"""A verification command does not outlive the kstrl process that started it (#642 slice 5).

The defect: ``verify.run_scrubbed`` starts every check, setup and
``[stack]`` command in a session of its own, so killing kstrl's process
group does not reach it, and its only cleanup runs inside kstrl. A
SIGKILL of ``ks factory`` while a base-gate check ran left the check and
the process it started alive 3.0 s later, on the host and inside the nono
rung alike. nono's ``wrap`` replaces itself with the command, so under a
rung the command is the very process kstrl started, in the session kstrl
gave it.

The fix: ``run_scrubbed`` starts the command under
``kstrl/agents/leash.py``, as every agent is started, so the command's
group ends when kstrl's end of the lifeline closes. Under a rung the
leash starts nono, never the reverse.

Every process these tests signal is the ``ks`` process they started or a
pid the check wrote to a file (#292).
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

from kstrl.procgroup import pid_is_alive
from tests import test_stack_e2e as stack_e2e
from tests.helpers import procs
from tests.helpers.executables import write_executable
from tests.test_agent_dies_with_kstrl import START_FUSE_SECONDS, _factory_args, _ks
from tests.test_agent_processes_outlive_run import COMPLETE, _env, _repo
from tests.test_isolation_rung import needs_nono

#: Seconds from the kill to the death of the check and of the process it
#: started. Measured on macOS, n=20 per case at load average 21 to 25 on
#: 10 cores: at most 0.021 s on the host and 0.018 s inside the nono rung.
#: Before the fix both were alive 3.0 s after the kill. The margin is for
#: CI runners, which were not measured.
CHECK_BOUND_SECONDS = 2.0


def _check(check_pid: Path, started_pid: Path) -> str:
    """A check that starts a background process (in the check's own group:
    ``sh`` without job control), writes both pids, and waits without
    writing a byte. ``exec`` keeps the check's pid the
    one it wrote."""
    return (
        f"sh -c 'echo $$ > \"$1\"; exec sleep 600' sh '{started_pid}' & "
        f"echo $$ > '{check_pid}'; exec sleep 600"
    )


def _kill(proc: subprocess.Popen[str], sig: signal.Signals, check: int, started: int) -> float:
    """Send ``sig`` to the ks process and return when it was sent.

    Both pids must be alive at that moment: a leash that fired early would
    have ended the check already, and the assertion after the kill would
    pass without the kill having ended anything."""
    assert pid_is_alive(check) and pid_is_alive(started), (
        f"precondition: the check {check} and the process it started {started} "
        "run until ks is killed"
    )
    killed = time.monotonic()
    os.kill(proc.pid, sig)
    return killed


def _assert_both_die(check: int, started: int, killed: float, how: str) -> None:
    for what, pid in (("check", check), ("process the check started", started)):
        remaining = max(0.0, killed + CHECK_BOUND_SECONDS - time.monotonic())
        assert procs.wait_for_pid_to_die(pid, timeout=remaining), (
            f"the {what}, pid {pid}, alive {CHECK_BOUND_SECONDS}s after {how}"
        )


def _dispose(proc: subprocess.Popen[str], *pidfiles: Path) -> None:
    """SIGKILL every pid the check wrote, then the ks group."""
    for pidfile in pidfiles:
        text = pidfile.read_text(encoding="utf-8").strip() if pidfile.exists() else ""
        if text:
            try:
                os.kill(int(text), signal.SIGKILL)
            except ProcessLookupError:
                pass
    procs.kill_group(proc.pid)
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def test_a_check_and_the_process_it_started_die_with_kstrl(tmp_path: Path) -> None:
    """No ``[stack]``: the base gate's test command runs on the host. The
    ``ks factory`` process is SIGKILLed while the check is silent; the check
    and the process it started are gone within ``CHECK_BOUND_SECONDS``."""
    root = _repo(tmp_path)
    check_pid, started_pid = tmp_path / "check.pid", tmp_path / "started.pid"
    args = _factory_args(root, "1", "--no-worktrees")
    args[args.index("--test-command") + 1] = _check(check_pid, started_pid)
    proc = _ks(root, _env(COMPLETE), *args)
    try:
        check = procs.read_pid(check_pid, timeout=START_FUSE_SECONDS)
        started = procs.read_pid(started_pid, timeout=START_FUSE_SECONDS)
        killed = _kill(proc, signal.SIGKILL, check, started)
        _assert_both_die(check, started, killed, "a SIGKILL of ks factory")
    finally:
        _dispose(proc, check_pid, started_pid)


@needs_nono
def test_a_check_inside_the_rung_and_the_process_it_started_die_with_kstrl(
    tmp_path: Path,
) -> None:
    """A confirmed ``[stack]``: the check runs inside the test zone's nono
    rung, which the refused write to ``$HOME`` shows. The ``ks factory``
    process is SIGKILLed while the check is silent; the check and the
    process it started are gone within ``CHECK_BOUND_SECONDS``."""
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    check_pid, started_pid = pid_dir / "check.pid", pid_dir / "started.pid"
    escape = Path.home() / f"kstrl-escape-check-{secrets.token_hex(6)}"
    command = f'echo x > "$HOME/{escape.name}"; {_check(check_pid, started_pid)}'
    stack = stack_e2e._stack({"hang": command}, rung={"writable": [str(pid_dir)]})
    root = stack_e2e._repo(tmp_path, stack)
    engineer = write_executable(
        tmp_path / "engineer.sh", "#!/bin/sh\necho '<promise>COMPLETE</promise>'\n"
    )
    args = [
        "factory",
        *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
        *("--root", str(root), "--agent-cmd", str(engineer)),
        *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
        *("--max-retries", "0", "--max-parallel", "1"),
        *("--review-mode", "skip", "--contract-check", "skip"),
    ]
    proc = _ks(root, stack_e2e._child_env(), *args)
    try:
        check = procs.read_pid(check_pid, timeout=START_FUSE_SECONDS)
        started = procs.read_pid(started_pid, timeout=START_FUSE_SECONDS)
        killed = _kill(proc, signal.SIGKILL, check, started)
        _assert_both_die(check, started, killed, "a SIGKILL of ks factory")
        assert not escape.exists(), "the check wrote $HOME, so it did not run in the rung"
    finally:
        escape.unlink(missing_ok=True)
        _dispose(proc, check_pid, started_pid)


def test_an_interrupted_ks_check_takes_its_check_and_the_process_it_started(
    tmp_path: Path,
) -> None:
    """``ks check`` installs no stop handler, so a SIGINT is a
    ``KeyboardInterrupt`` inside ``run_scrubbed``'s read. Its broad clause
    SIGKILLs the command's group before it lets the leash go: the check and
    the process it started are gone within ``CHECK_BOUND_SECONDS``."""
    root = _repo(tmp_path)
    check_pid, started_pid = tmp_path / "check.pid", tmp_path / "started.pid"
    env = {
        **_env(COMPLETE),
        "KSTRL_VERIFY_TEST_CMD": _check(check_pid, started_pid),
        "KSTRL_VERIFY_TYPECHECK_CMD": "true",
        "KSTRL_VERIFY_LINT_CMD": "true",
    }
    proc = _ks(root, env, "check", "--root", str(root), "--ui", "plain", "--no-color")
    try:
        check = procs.read_pid(check_pid, timeout=START_FUSE_SECONDS)
        started = procs.read_pid(started_pid, timeout=START_FUSE_SECONDS)
        killed = _kill(proc, signal.SIGINT, check, started)
        _assert_both_die(check, started, killed, "a SIGINT of ks check")
    finally:
        _dispose(proc, check_pid, started_pid)
