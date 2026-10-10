"""Run a child command with a scrubbed environment, in a rung when one is given."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from kstrl.agents.proc import leash_command, leash_start_error
from kstrl.agents.spawn_record import new_nonce
from kstrl.procdispose import drain_or_abandon, reap_or_abandon
from kstrl.procgroup import signal_process_tree
from kstrl.rung import Rung
from kstrl.stack import SECRET_NAME_FRAGMENTS

# R2.6 env scrub: verification subprocesses execute agent-authored code
# (the project's tests, linters run over agent files, CLI fixtures), so
# they must never inherit the harness's secrets. Allowlist, not denylist:
# only names below (or matching a prefix below) pass through, everything
# else - ANTHROPIC_API_KEY, OPENAI_API_KEY, cloud credentials, gh tokens -
# is dropped. Since the #696 flag day the list names no toolchain: a
# ``[stack]`` adds the variables its commands need in ``[stack] env``.
SCRUB_ENV_ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "TERM",
        "CI",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    }
)
SCRUB_ENV_ALLOWED_PREFIXES: tuple[str, ...] = ("LC_",)

# Belt over the allowlist's braces: no name containing one of these
# fragments passes, whoever admitted it. The same tuple ``[stack] env`` is
# refused against at load (#696 decision 11).
_SCRUB_ENV_SENSITIVE_FRAGMENTS: tuple[str, ...] = SECRET_NAME_FRAGMENTS


def scrubbed_subprocess_env(declared: tuple[str, ...] = ()) -> dict[str, str]:
    """Allowlist-filtered copy of ``os.environ`` for verification subprocesses.

    ``declared`` is a ``[stack]``'s ``env`` (#696): names added to
    :data:`SCRUB_ENV_ALLOWED_NAMES`. The sensitive-fragment filter runs last.
    """
    names = SCRUB_ENV_ALLOWED_NAMES | frozenset(declared)
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name not in names and not name.startswith(SCRUB_ENV_ALLOWED_PREFIXES):
            continue
        if any(frag in name for frag in _SCRUB_ENV_SENSITIVE_FRAGMENTS):
            continue
        env[name] = value
    return env


_SCRUB_TERM_GRACE_SECONDS = 5.0


def _signal_process_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal the child's whole process group, direct-child fallback.

    A one-line forward to :func:`kstrl.procgroup.signal_process_tree`,
    kept as a name because ``tests/test_hitl_env_scrub.py`` pins this
    module's timeout behaviour through it and because the name says what
    the verification path is doing at the point it does it.

    #308 lifted the pid/pgid GUARD out of here and left the routine
    around it, so ``os.killpg`` itself stayed spelled in this module and
    in ``agents.proc`` as well as in ``procgroup``. #329 is what that
    costs: three spellings is how a fourth arrives unguarded. The whole
    routine now has one home.
    """
    signal_process_tree(proc, sig)


class ChildOutputDecodeError(RuntimeError):
    """A verification child produced bytes that are not valid utf-8.

    :func:`run_scrubbed` chose ``encoding="utf-8"``, so it is the one place
    that can name this fault; every caller would otherwise meet a bare
    ``UnicodeDecodeError`` (a ``ValueError``) that no handler here was written
    for, and the mechanical verifier would die with a traceback instead of
    returning a verdict (#416). Every caller answers for it in its own result
    type, as it answers for a timeout: the check ran, measured nothing, and
    fails closed. The two exceptions, measured rather than asserted away
    (#416's simplify review): ``contract._abort_merge`` and the prune call in
    ``contract._remove_temp_worktree``, whose results were never read and
    which swallow it so cleanup is not blocked. kstrl does not weaken the
    decode with ``errors=`` to make it go away (#409).

    ``stdout`` and ``stderr`` carry what the child printed, rendered by
    :func:`_readable`, so a gate that fails here still leaves its output
    for the operator (#527). They are evidence only: no verdict reads them.
    """

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def _translated(text: str) -> str:
    """``text`` with ``\\r\\n`` and ``\\r`` turned into ``\\n``.

    Exactly what CPython's ``Popen._translate_newlines`` does in text
    mode (3.12: decode, then these two replaces), so reading the child's
    bytes here gives every caller the string text mode used to give it.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _readable(data: bytes | str | None) -> str:
    """A child's output as text the operator can read, whatever it holds (#527).

    A byte that is not valid utf-8 is written as a ``\\xNN`` escape rather
    than replaced, so the log shows the byte the strict decode refused.
    This is the EVIDENCE rendering and nothing decides on it: the strict
    decode in :func:`run_scrubbed` still fails the call (#409, #416).
    ``str`` is passed through and ``None`` is empty, because a timeout
    raised by a test double carries no output at all.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return _translated(data.decode("utf-8", errors="backslashreplace"))


def run_scrubbed(
    cmd: str | list[str],
    *,
    cwd: Path,
    timeout: float | None,
    term_grace: float = _SCRUB_TERM_GRACE_SECONDS,
    extra_env: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    declared_env: tuple[str, ...] = (),
    rung: Rung | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a verification subprocess: scrubbed env, own process group, leashed.

    The command runs under ``kstrl/agents/leash.py``, as every agent does
    (#642 slice 5): the leash leads the command's process group and ends
    it, SIGTERM, the grace, SIGKILL, once this process has gone however
    it went, SIGKILL included, because the one write end of its lifeline
    is held here and closed in this function's ``finally``. Under a rung
    the leash starts nono, never the reverse: nono replaces itself with
    the command (``nono wrap``), so the command and everything it starts
    stay in the leash's group, and nono refuses to run inside nono. The
    returncode is the command's, through the leash; a death by signal N
    reads as 128 + N.

    ``declared_env`` is a ``[stack]``'s ``env``, handed to
    :func:`scrubbed_subprocess_env` (#696).

    ``rung`` runs the command inside that proven isolation rung (#700
    slice 2), through its ``command``: a string ``cmd`` as
    ``/bin/sh -c cmd``, the shell ``shell=True`` would have used, and every
    declared name and ``extra_env`` value set again inside the rung,
    because nono strips some variables from the environment it passes on.
    A :class:`~kstrl.rung.HostFallback` runs that same ``/bin/sh -c cmd``
    on the host (a platform with no prover). None, the default, runs on
    the host exactly as before.

    Drop-in for the ``subprocess.run(..., capture_output=True, text=True,
    timeout=...)`` calls verification used to make, with two differences
    (R2.6): the child gets :func:`scrubbed_subprocess_env` instead of the
    harness environment, and on timeout the ENTIRE process group is
    signalled (SIGTERM, grace, SIGKILL) so a test that backgrounds a
    server cannot leak it past the deadline. A string ``cmd`` runs through
    the shell exactly as before; a list does not.

    ``extra_env`` carries values KSTRL ITSELF CHOSE for one command,
    layered on top of the scrub, never values inherited from the
    operator's environment - so the scrub's guarantee (no secret reaches
    a verification subprocess) is unchanged. Its one caller is an
    acceptance check (#700 slice 4, ``replay._ran``), given ``KSTRL_TREE``.

    ``stdin_text`` given as a string is the child's whole stdin, sent as
    utf-8 through a pipe that is then closed, so the child reads it and
    then EOF. ``None`` leaves stdin inherited from this process, which is
    what every gate caller does today (#632 decision 7). The fixture
    runners always pass a string, because an inherited stdin made a
    fixture's verdict depend on how kstrl itself was launched: an open
    pipe nobody writes timed the fixture out, ``/dev/null`` passed it.

    ``timeout=None`` waits with no deadline: that is what a work limit the
    operator did not set means (#467). Callers turn a configured value into
    one with :func:`kstrl.timeout.limit_seconds`, because ``timeout=0``
    would mean "already expired" here.

    Raises :class:`subprocess.TimeoutExpired` after the group is dead so
    existing callers' timeout handling keeps working unchanged, and
    :class:`ChildOutputDecodeError` when the child's bytes are not valid
    utf-8 - every one of this function's call sites answers for it
    exactly as it answers for a timeout (#416).

    THE TIMEOUT PATH LETS GO THROUGH ``procdispose`` (#326). It used to
    drain the pipes itself and, when that drain expired, set
    ``stdout, stderr = "", ""`` and drop the child on the floor: no
    close of the two pipe ends, and no register, so the only thing left
    holding the pid was ``Popen.__del__``. That is not a fallback under
    ``PYTHONWARNINGS=error``, which is a setting this codebase already
    records crashing a daemon: ``__del__`` calls ``_warn`` BEFORE
    ``_active.append`` (CPython 3.12.8 ``subprocess.py`` lines 1139 and
    1145), the warn raises, ``__del__`` aborts, and the child stays a
    zombie for the life of the process. This is the widest window of the
    three sites that had it - it needs no D-state child, only something
    outside the group holding a pipe write end, which a forked
    grandchild does routinely, and it runs once per verification command
    per iteration rather than once per timed-out run.
    """
    env = scrubbed_subprocess_env(declared_env)
    if extra_env:
        env.update(extra_env)
    spawned = _in_rung(cmd, rung, env, [*(declared_env or ()), *(extra_env or {})])
    # `/bin/sh -c`, the shell `shell=True` used, so a string runs as before.
    argv = ["/bin/sh", "-c", spawned] if isinstance(spawned, str) else list(spawned)
    lifeline_read, lifeline = os.pipe()
    status_read, status_write = os.pipe()
    # BYTES mode, decoded below (#527). In text mode CPython decodes inside
    # `communicate`, and a byte that is not utf-8 raised there with both
    # streams discarded, so a gate that failed that way could leave no log.
    try:
        proc = subprocess.Popen(
            leash_command(
                argv,
                lifeline=lifeline_read,
                status=status_write,
                term_grace=term_grace,
                nonce=new_nonce(),
            ),
            cwd=cwd,
            stdin=None if stdin_text is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
            pass_fds=(lifeline_read, status_write),
        )
    except BaseException:
        os.close(lifeline)
        os.close(status_read)
        raise
    finally:
        os.close(lifeline_read)
        os.close(status_write)
    try:
        # A command the leash could not start raises what a direct Popen
        # would have, after the broad clause below lets the leash go.
        start_error = leash_start_error(status_read, argv[0])
        if start_error is not None:
            raise start_error
        raw_stdout, raw_stderr = proc.communicate(
            input=None if stdin_text is None else stdin_text.encode("utf-8"),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            pass
        # SIGKILL the group even when the direct child honored SIGTERM: a
        # grandchild that ignored it can hold the pipes open and would
        # otherwise block the drain below indefinitely.
        _signal_process_group(proc, signal.SIGKILL)
        drained_stdout, drained_stderr = drain_or_abandon(proc, term_grace)
        # What the child printed before the kill, as readable text: the
        # gate that timed out writes it to its log (#527).
        raise subprocess.TimeoutExpired(
            cmd,
            expired.timeout,
            output=_readable(drained_stdout),
            stderr=_readable(drained_stderr),
        ) from None
    except BaseException:
        # The rule `procgroup._read_ps` already states and this module
        # did not: every exit that is not a completed read leaves a
        # child behind, so every one of them goes through the same
        # disposal. Catching only `TimeoutExpired` made this the widest
        # remaining hole of the #326 class rather than a fixed site - a
        # KeyboardInterrupt out of `ks verify`, or a MemoryError on a
        # capture big enough to matter, left the child unsignalled,
        # unreaped, unregistered and holding both pipe ends, on the
        # highest-frequency spawn in the factory. The group first: the
        # direct child is the leash, and a SIGKILL of the leash alone
        # would leave the command running with nothing to end it (#642).
        _signal_process_group(proc, signal.SIGKILL)
        drain_or_abandon(proc, term_grace)
        raise
    finally:
        os.close(lifeline)
    # The read completed, so the child is reaped and both pipes are closed:
    # a decode failure here leaves nothing to dispose of (#326). Strict
    # utf-8, stdout first, as text mode decoded them (#409, #416).
    try:
        stdout = _translated(raw_stdout.decode("utf-8"))
        stderr = _translated(raw_stderr.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ChildOutputDecodeError(
            f"the command produced bytes that are not valid utf-8, so its "
            f"output could not be read: {exc}",
            stdout=_readable(raw_stdout),
            stderr=_readable(raw_stderr),
        ) from exc
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def start_scrubbed(
    cmd: str,
    *,
    cwd: Path,
    rung: Rung,
    log: IO[bytes],
    declared_env: tuple[str, ...] = (),
) -> tuple[subprocess.Popen[bytes], int]:
    """Start ``cmd`` inside ``rung`` and return at once, leaving it running (#700 slice 3).

    The sibling of :func:`run_scrubbed` for a command that starts servers
    and exits while they keep running: a ``[stack]``'s ``up``.
    ``run_scrubbed`` cannot run one, because ``communicate`` reads both
    pipes to their end and a server the command started holds them open,
    so the read would wait on the servers rather than on the command.
    Here stdout and stderr go to ``log``, a file the caller opened, so
    nothing waits on a pipe.

    Shared with :func:`run_scrubbed`: :func:`scrubbed_subprocess_env`, a
    process group of the child's own, and :func:`_in_rung`. ``rung`` has
    no default and is never None: a command that outlives this call runs
    on the host only under the explicit host fallback of a platform with
    no prover. The child leads a new session, so its pid is the
    group id; the caller waits on it, records that id, and stops the group
    through :mod:`kstrl.procgroup` (``kstrl.replay``).

    The child is the leash in its ``--hold`` mode (#642 slice 6): it exits
    with ``cmd``'s status when ``cmd`` exits, as ``cmd`` would have, and a
    fork of it stays in the group and ends the group once the lifeline
    closes. The lifeline's write end is returned with the child, and the
    caller closes it when it stops the group; if this process dies first,
    the kernel closes it, so the servers ``cmd`` started end with it.
    """
    env = scrubbed_subprocess_env(declared_env)
    spawned = _in_rung(cmd, rung, env, declared_env or ())
    argv = ["/bin/sh", "-c", spawned] if isinstance(spawned, str) else list(spawned)
    lifeline_read, lifeline = os.pipe()
    status_read, status_write = os.pipe()
    try:
        proc = subprocess.Popen(
            leash_command(
                argv,
                lifeline=lifeline_read,
                status=status_write,
                term_grace=_SCRUB_TERM_GRACE_SECONDS,
                nonce=new_nonce(),
                hold=True,
            ),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            pass_fds=(lifeline_read, status_write),
        )
    except BaseException:
        os.close(lifeline)
        os.close(status_read)
        raise
    finally:
        os.close(lifeline_read)
        os.close(status_write)
    start_error = leash_start_error(status_read, argv[0])
    if start_error is not None:
        os.close(lifeline)
        reap_or_abandon(proc, _SCRUB_TERM_GRACE_SECONDS)
        raise start_error
    return proc, lifeline


def _in_rung(
    cmd: str | list[str], rung: Rung | None, env: Mapping[str, str], names: Sequence[str]
) -> str | list[str]:
    """``cmd`` as :func:`run_scrubbed` spawns it: unchanged with no rung,
    else inside ``rung`` with each of ``names`` that ``env`` holds set
    again after nono (#700 slice 2)."""
    if rung is None:
        return cmd
    argv = ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else cmd
    assignments = [f"{name}={env[name]}" for name in dict.fromkeys(names) if name in env]
    return rung.command(argv, assignments)
