"""Phase 1: Mechanical verification - independent checks after agent execution."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any, Protocol

from kstrl import git
from kstrl.agents.proc import leash_command, leash_start_error
from kstrl.agents.spawn_record import new_nonce
from kstrl.config import component_progress_path
from kstrl.config_numbers import check_numbers
from kstrl.failure_excerpt import failure_excerpt
from kstrl.findings import Finding, finding_waiver
from kstrl.guards import path_is_allowed
from kstrl.policy import (
    DEFAULT_SECRET_PATTERNS,
    PolicyConfig,
    PolicyConfigError,
    _scan_secrets,
    evaluate_policy,
    parse_added_lines,
)
from kstrl.prd import PRD
from kstrl.procdispose import drain_or_abandon, reap_or_abandon
from kstrl.procgroup import signal_process_tree
from kstrl.rung import Rung
from kstrl.stack import NO_STACK, SECRET_NAME_FRAGMENTS, Stack, stack_in_force
from kstrl.timeout import limit_seconds
from kstrl.waivers import Waivers, apply_waivers, waiver_note

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


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    # R8.1: typed findings this mechanical check produced, lifted into the
    # component's finding stream by the pipeline so a machine-made gate
    # decision lands in the audit trail (PR body, journal) and not only in
    # the retry context. Empty for checks that emit prose only.
    findings: list[Finding] = field(default_factory=list)
    # #227: whether this row is a MEASUREMENT. False when the check ran and
    # measured nothing anyway - it timed out, or its detector is not
    # installed. Those still produce a row, so :class:`NotMeasured` cannot
    # carry them: the sidecar is for checks that produce NO row, and turning
    # one of these into a gap would make `result.passed` true on a timeout.
    #
    # Read by :mod:`kstrl.baseline` and nothing else today. It changes no
    # existing behaviour and no published surface: `passed` still decides the
    # verdict, the report table and the `ks check --json` check objects are
    # untouched. What it buys is that a signature's disappearance can be told
    # apart from the check's, which a fallback signature cannot say for
    # itself: `signature_slug` strips digits, so "timed out after 300.0s" and
    # "timed out after 1800.0s" are the same string.
    measured: bool = True
    # #462: the gate's own output, stdout then stderr, when a test,
    # typecheck or lint gate ran and FAILED; None for every other row.
    # #527: a gate that timed out, or printed bytes that are not utf-8,
    # FAILED too, and this holds what it printed before it was stopped.
    # Bounded by :func:`bounded_gate_output`. The pipeline writes it to
    # disk as the operator's evidence for the failure. It is never put in
    # the retry prompt, the report table or ``ks check --json``: those
    # read ``details``, which is the excerpt of this text.
    output: str | None = None


#: Why a check that was ASKED FOR produced no measurement. A stable
#: token: it reaches `ks check --json` and `events.jsonl`, so a reader
#: keys on it and not on the prose beside it.
#:
#: ``retired``: kstrl no longer has a mechanism for the check, and the
#: thing that asked for it (the autonomy ladder, for Layer 0) still does
#: (#696 slice 8).
NOT_MEASURED_RETIRED = "retired"


@dataclass(frozen=True)
class NotMeasured:
    """A check that was asked for and produced no measurement (#306).

    The SIDECAR. Deliberately not a :class:`CheckResult`: it never
    reaches ``checks``, so ``all(c.passed ...)``, ``report_lines``'
    verdict column, ``ks check --json``'s ``checks`` array and
    :func:`kstrl.review.build_review_prompt` cannot read it as a pass -
    which is the whole of #306. Equally it never reaches
    :meth:`VerificationResult.as_context`, so it is not retry context:
    no engineer iteration is spent on a gap it cannot close.

    A check nobody asked for records nothing at all, on purpose: a
    question nobody asked needs no answer. ``reason`` is one of the
    ``NOT_MEASURED_*`` constants above. ``detail`` is prose for a human
    and is never parsed.
    """

    check: str
    reason: str
    detail: str

    def as_line(self) -> str:
        """The report-table rendering, for :meth:`VerificationResult.report_lines`.

        Not every terminal surface: the factory's Phase 1 warning
        prefixes the component id and names the reason, because it is
        one line inside a multi-component run rather than a row under a
        table that already says which component it is.
        """
        return f"  {self.check}  not measured  {self.detail}"

    def as_token(self) -> str:
        """The ``events.jsonl`` rendering: ``"<check>:<reason>"``.

        Here rather than in the emitters because there are two of them,
        in :mod:`kstrl.pipeline` and :mod:`kstrl.feature_verify`, and a
        format spelled twice is one an edit can change in one place
        only. Same ``<check>:<code>`` shape
        :func:`kstrl.evolution.split_signature` already reads, so a
        consumer of that file meets one convention rather than two.
        """
        return f"{self.check}:{self.reason}"

    def to_dict(self) -> dict[str, str]:
        """The ``ks check --json`` rendering."""
        return {"check": self.check, "reason": self.reason, "detail": self.detail}


#: The gap Phase 1 records where Layer 0 ran (#696 decision 7): from
#: autonomy level 1, where the R8.5 roadmap made Layer 0 blocking. The
#: mechanical layer read one language's test files and is gone; the code
#: reviewer's test-weakening criterion (``review.REVIEWER_PROMPT``) is the
#: only check left, and it has no measured detection rate yet. The
#: pipeline puts this gap on the pull request.
LAYER0_NOT_MEASURED = NotMeasured(
    "test_adequacy",
    NOT_MEASURED_RETIRED,
    "Layer 0 not measured: kstrl reads no test file mechanically (#696 decision 7). "
    "The code reviewer's test-weakening criterion is the only check that this change "
    "did not weaken the tests, and its detection rate is not yet measured.",
)


def _capped_detail_lines(check: CheckResult, limit: int | None) -> list[str]:
    """``check``'s details, indented, truncated to ``limit`` if given.

    The truncation is never silent: what is dropped is counted on a final
    line, because a report that quietly shows you 12 of 400 failures is a
    report you would act on wrongly.
    """
    lines = [f"      {line}" for detail in check.details for line in detail.splitlines()]
    if limit is None or len(lines) <= limit:
        return lines
    return [*lines[:limit], f"      ... {len(lines) - limit} more line(s) not shown"]


@dataclass
class VerificationResult:
    """Aggregated result of all mechanical checks."""

    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    #: Checks that were asked for and measured nothing (#306). The
    #: sidecar: see :class:`NotMeasured` for why it is beside ``checks``
    #: rather than in it.
    not_measured: list[NotMeasured] = field(default_factory=list)

    def as_context(self) -> str:
        """Format failures for injection into retry prompt."""
        lines: list[str] = []
        for check in self.checks:
            if not check.passed:
                lines.append(f"- {check.name}: FAIL - {check.message}")
                for detail in check.details[:10]:
                    lines.append(f"  {detail}")
        return "\n".join(lines)

    @property
    def failure_count(self) -> int:
        """How many checks failed (#233).

        One per failing check: kstrl parses no check's output, so a check
        that reported forty failures counts the same as one that reported
        one (#696 decision 5). This is the number ``[factory]
        convergence_attempts`` watches across attempts; the retry context
        cannot supply it, because Phase 1 files one entry per attempt
        however many checks failed.
        """
        return sum(1 for check in self.checks if not check.passed)

    def report_lines(
        self,
        *,
        durations: bool = True,
        max_detail_lines: int | None = None,
    ) -> list[str]:
        """One line per check, then the indented details of each failure.

        The TERMINAL rendering of this object, in one place: ``ks check``
        and ``ks feature``'s #288 report print the same table, and before
        this existed they printed it from two copies of the same
        f-string.

        ``durations=False`` drops the wall-clock column. `ks feature`
        needs that: its narration sits inside a longer flow that
        ``tests/test_feature_run.py`` compares BYTE FOR BYTE between a
        recorded and an unrecorded run, and a timing makes two runs of
        the same work disagree. The figure is not lost there - it goes on
        the ``VerificationResultEvent``.

        ``max_detail_lines`` caps the details PER CHECK and appends a
        line saying how many were dropped, so a truncation is never
        silent. `ks feature` needs that too, and for a different reason:
        under the embedded TUI every one of these lines becomes a
        ``Log`` event on the run bus, and a failing check's details are
        the excerpt :func:`kstrl.failure_excerpt.failure_excerpt` keeps,
        up to 80 lines. A run of failing checks is hundreds of events
        per report, up to ``2 + repair_max_runs`` times a run, which is
        the event-stream flood ``commandrun._StreamFilterSink`` exists to
        prevent. ``as_context`` already truncates at 10 for the same
        reason. None (the default, and ``ks check``) prints everything:
        there the measurement IS the whole output.
        """
        width = max((len(check.name) for check in self.checks), default=0)
        lines: list[str] = []
        for check in self.checks:
            verdict = "pass" if check.passed else "FAIL"
            timing = f"  ({check.duration_seconds:.2f}s)" if durations else ""
            lines.append(f"  {check.name.ljust(width)}  {verdict}  {check.message}{timing}")
            if check.passed:
                continue
            lines.extend(_capped_detail_lines(check, max_detail_lines))
        # The sidecar, below the table and outside it (#306). Rendered
        # here rather than by each caller for the reason the table is:
        # `ks check` and `ks feature` must not be able to disagree about
        # whether they mention what was not measured. No verdict column
        # and no duration - there is no verdict, and nothing was timed.
        lines.extend(gap.as_line() for gap in self.not_measured)
        return lines


def _optional_str(value: object) -> str | None:
    """A toml scalar as a string, with the empty string meaning unset."""
    return str(value) or None


def gate_names(config: VerifyConfig) -> tuple[str, ...]:
    """The names Phase 1's command gates go by: the ``[stack]``'s check
    names (#696), or none when there is no stack."""
    return config.project_stack.check_names if config.project_stack is not None else ()


def _gate_name_list(value: object, source: str) -> list[str]:
    """``value`` as a list of strings, or ValueError naming ``source``."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source} must be a list of gate names, got {value!r}")
    return list(value)


def validate_fast_iteration_checks(value: object, source: str, names: Sequence[str]) -> list[str]:
    """``value`` as a list of gate names, or ValueError naming ``source``.

    A list of strings, each one of ``names``: :func:`gate_names`, the
    ``[stack]``'s check names. Empty is valid and turns the
    between-iteration checks off.
    """
    gates = _gate_name_list(value, source)
    unknown = [item for item in gates if item not in names]
    if unknown:
        raise ValueError(f"{source} names unknown gate(s) {unknown}; expected any of {list(names)}")
    return gates


def _fast_iteration_checks_from_toml(value: object) -> list[str]:
    """The shape only: the names are checked at the end of
    :meth:`VerifyConfig.load`, once it knows whether a ``[stack]`` names them."""
    return _gate_name_list(value, "[verify] fast_iteration_checks")


def _fast_iteration_checks_from_env(raw: str) -> list[str]:
    """Comma-separated. Blank parts are dropped, so "" is the empty list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


#: Every live ``[verify]`` toml key and how its value is coerced onto the
#: dataclass. A table rather than a per-key ``if``: the chain it replaced
#: was fourteen near-identical branches, and its cyclomatic complexity
#: was already twice the repo's ratchet limit before #258 added three
#: more keys to it. Order is the dataclass's, so a reader can diff the
#: two lists by eye. Anything absent from this table is not a toml key.
_VERIFY_TOML_FIELDS: tuple[tuple[str, Callable[[Any], object]], ...] = (
    ("check_diff_scope", bool),
    ("check_bad_patterns", bool),
    ("subprocess_timeout", float),
    ("require_self_critique", bool),
    ("self_critique_min_bullets", int),
    ("progress_file_path", _optional_str),
    ("fast_iteration_checks", _fast_iteration_checks_from_toml),
)


@dataclass
class VerifyConfig:
    """Configuration for mechanical verification."""

    check_diff_scope: bool = True
    check_bad_patterns: bool = True
    subprocess_timeout: float = 0.0
    # Mechanical enforcement of the engineer prompt's "## Self-Critique"
    # mandate. Off by default to keep this opt-in; set to True (or
    # KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE=1) to fail Phase 1 when an
    # iteration's progress.txt entry omits the block.
    require_self_critique: bool = False
    self_critique_min_bullets: int = 3
    # Where check_self_critique looks for the engineer's progress log.
    # None (the default) derives it from the component's PRD
    # (config.component_progress_path), which is where the engineer was
    # actually told to write and the only location inside the
    # component's allowedPaths. An explicit value wins for every
    # component. It is None-defaulted rather than carrying a separate
    # "was it set?" flag because every scalar field of this dataclass is
    # a documented kstrl.toml key (scripts/gen_docs.py probes for that).
    progress_file_path: str | None = None
    # #233: the gates run between engineer iterations, whose failures are
    # handed to the next iteration's prompt. Empty (the default) is off.
    # A list, never a tuple: scripts/gen_docs.py probes list defaults.
    fast_iteration_checks: list[str] = field(default_factory=list)
    # #696: the project's [stack], read by ``load`` from its own table. Its
    # checks are Phase 1's command gates; None is no stack, and Phase 1 then
    # fails closed (:data:`NO_STACK_CHECK`). Provenance: no [verify] key.
    project_stack: Stack | None = field(default=None, metadata={"provenance": True})
    # #700 slice 2: the TEST-zone rung a ``ks factory`` run under a [stack]
    # proved before its base gates; every [stack] check runs inside it.
    # None runs on the host. Set only through ``FactoryConfig``, never from
    # kstrl.toml. Provenance: no [verify] key.
    rung: Rung | None = field(default=None, metadata={"provenance": True})

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            subprocess_timeout=float(os.environ.get("KSTRL_TIMEOUT_VERIFY", "0")),
            require_self_critique=os.environ.get("KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE", "") == "1",
            self_critique_min_bullets=int(
                os.environ.get("KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS", "3"),
            ),
            progress_file_path=os.environ.get("KSTRL_VERIFY_PROGRESS_FILE"),
            fast_iteration_checks=_fast_iteration_checks_from_env(
                os.environ.get("KSTRL_VERIFY_FAST_ITERATION_CHECKS", ""),
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> VerifyConfig:
        """Load verify config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "verify")
        for key, coerce in _VERIFY_TOML_FIELDS:
            if key in section:
                setattr(config, key, coerce(section[key]))
        # Env overrides. Each var is applied only when it is explicitly
        # set in the environment: the previous compare-against-default
        # heuristic silently dropped an env value that happened to equal
        # the dataclass default (e.g. KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS=3
        # could not override a toml self_critique_min_bullets), breaking the
        # env-beats-toml precedence contract (R2.1).
        env = cls.from_env()
        env_var_to_field = {
            "KSTRL_TIMEOUT_VERIFY": "subprocess_timeout",
            "KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE": "require_self_critique",
            "KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS": "self_critique_min_bullets",
            "KSTRL_VERIFY_PROGRESS_FILE": "progress_file_path",
            "KSTRL_VERIFY_FAST_ITERATION_CHECKS": "fast_iteration_checks",
        }
        for env_var, field_name in env_var_to_field.items():
            if env_var in os.environ:
                setattr(config, field_name, getattr(env, field_name))
        # Last, so every [verify] key above has been read when a bad
        # [stack] raises (the entry check's unread-name report).
        config.project_stack = stack_in_force(root_dir)
        if "fast_iteration_checks" in section:
            # The toml value is refused on its own names even when the
            # environment overrides it, as before #696.
            validate_fast_iteration_checks(
                section["fast_iteration_checks"],
                "[verify] fast_iteration_checks",
                gate_names(config),
            )
        source = (
            "KSTRL_VERIFY_FAST_ITERATION_CHECKS"
            if "KSTRL_VERIFY_FAST_ITERATION_CHECKS" in os.environ
            else "[verify] fast_iteration_checks"
        )
        config.fast_iteration_checks = validate_fast_iteration_checks(
            config.fast_iteration_checks, source, gate_names(config)
        )
        return check_numbers(config)


# Engineer prompt mandates the EXACT heading `## Self-Critique`.
# Accept also `- **Self-Critique:**` (common bullet-in-list form) and
# `## Self Critique` (loose hyphen-space variant). Reject prose like
# "the self-critique above" so we don't false-positive on body text.
# Both forms must START the line after at most a list marker + whitespace.
_SELF_CRITIQUE_HEADING_RE = re.compile(
    r"""^
    (?:
        \#{2,3}\s+                  # H2 / H3: '## ' or '### '
      | [\-*]\s+\*{2}\s*            # '- **' or '* **'
    )
    Self[-\s]Critique
    (?:
        \s*\*{2}                    # '**' (close bold)
      | \s*:                        # ':'
      | \s*\*{2}\s*:                # '**:'
      | \s*$                        # end-of-line
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An iteration entry boundary in progress.txt. The engineer prompt's
# documented format starts each appended entry with
# `## [YYYY-MM-DD] - [Story ID]`; agents also commonly write
# `## Iteration N`. Exactly two hashes: H3 sub-headings inside an
# entry must not be mistaken for a new entry.
_ITERATION_HEADING_RE = re.compile(
    r"""^\#\#\s+
    (?:
        \[?\d{4}-\d{2}-\d{2}        # '## [YYYY-MM-DD] - ...' (documented form)
      | Iteration\b                 # '## Iteration N' (loose variant)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An UNINDENTED bullet opening with a closed bold label, e.g.
# `- **Learnings:**` or `- **Interpretations** (only if ...): ...`.
# In the engineer prompt's entry format these are sibling sections of
# `- **Self-Critique:**`, so one of them terminates the bullet count.
# Applied to the raw line: the Self-Critique block's own nested bullets
# are indented and therefore never match.
_SECTION_BULLET_RE = re.compile(r"^[\-*]\s+\*{2}[^*]+\*{2}")

# Thematic break: the engineer prompt's entry format ends each entry
# with `---`.
_ENTRY_SEPARATOR_RE = re.compile(r"^-{3,}$")


def latest_entry(lines: Sequence[str]) -> int | None:
    """The index of the line that starts the latest iteration entry of a
    progress log, or None when no line is an iteration heading.

    Entries are appended, so the LAST iteration heading starts the
    current iteration's entry. The one definition of "this iteration's
    entry": the self-critique check reads it, and so does the acceptance
    dispute (``kstrl.acceptance_record.read_dispute``, #700 slice 10a).
    """
    for index in range(len(lines) - 1, -1, -1):
        if _ITERATION_HEADING_RE.match(lines[index]):
            return index
    return None


def _self_critique_text(progress_path: Path, start: float) -> str | CheckResult:
    """The progress file's text, or the failing check explaining why not.

    Two handlers because the remedies differ: "could not read" sends the
    operator to the file's permissions, and this file opened fine - the
    agent wrote bytes that are not UTF-8, which is a fact about the
    agent's output rather than about the disk. Before #320 the decode was
    not caught at all and a Phase 1 gate died with a traceback instead of
    reporting red.

    Split out of :func:`check_self_critique` so the second handler does
    not push that function past the complexity ratchets.
    """
    try:
        return progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Could not read progress file: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except UnicodeDecodeError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Progress file is not valid UTF-8: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )


def check_self_critique(
    progress_path: Path,
    min_bullets: int = 3,
) -> CheckResult:
    """Confirm the CURRENT (latest) progress.txt entry contains a
    Self-Critique block with at least ``min_bullets`` bullet points.

    Shape check only (H4): this verifies that a Self-Critique block of
    the right shape exists in the right place. It does NOT verify the
    substance of the bullets - vacuous-but-plausible failure modes
    pass. Substance is the reviewer's job.

    Format assumption (from the engineer prompt's Progress Format):
    each iteration appends an entry starting with an H2 heading of the
    form `## [YYYY-MM-DD] - [Story ID]` (the loose `## Iteration N`
    variant is also recognized), containing `- **Self-Critique:**` (or
    `## Self-Critique`) followed by bullets, sibling bold-label
    sections such as `- **Interpretations:**`, and a closing `---`.

    The check first locates the latest iteration boundary (the LAST
    line matching ``_ITERATION_HEADING_RE``), then requires a
    Self-Critique heading within that entry - a block written by an
    EARLIER iteration does not satisfy the check for the current one.
    If no iteration heading exists anywhere, the whole file is treated
    as a single entry (fallback for free-form progress files; per-
    iteration association is not possible there).

    Bullet counting stops at the next `##` heading, a `---` entry
    separator, or an unindented bold-label bullet (a sibling section
    like `- **Interpretations:**`), so bullets belonging to later
    sections do not inflate the count. Consequence of the format
    assumption: critique bullets themselves must either be indented
    under the `- **Self-Critique:**` bullet (the documented format) or
    not open with a bold label, otherwise they read as a sibling
    section and the check fails loudly rather than over-counting.

    Without this mechanical check, the engineer prompt's mandate to
    list >=3 failure modes can silently rot - the only enforcement
    path otherwise is the reviewer noticing, which is unreliable.
    """
    start = time.monotonic()
    text = _self_critique_text(progress_path, start)
    if isinstance(text, CheckResult):
        return text

    lines = text.splitlines()
    found = latest_entry(lines)
    entry_start, entry_found = (0, False) if found is None else (found, True)

    # Find the LAST self-critique heading WITHIN the latest entry, so
    # an earlier iteration's block cannot satisfy the current one and
    # repeated blocks inside one entry resolve to the newest.
    heading_idx: int | None = None
    for i in range(len(lines) - 1, entry_start - 1, -1):
        if _SELF_CRITIQUE_HEADING_RE.match(lines[i]):
            heading_idx = i
            break

    if heading_idx is None:
        where = (
            f"in the latest iteration entry (line {entry_start + 1}: "
            f"{lines[entry_start].strip()[:60]!r})"
            if entry_found
            else "in progress file"
        )
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"No '## Self-Critique' block found {where}. "
                "Engineer prompt mandates >=3 failure-mode bullets "
                "before declaring done."
            ),
            duration_seconds=time.monotonic() - start,
        )

    # Count bullets after the heading until the entry's content ends:
    # next `##` heading, `---` separator, or a sibling bold-label
    # bullet section (e.g. `- **Interpretations:**`).
    bullet_count = 0
    bullet_lines: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        # Stop at next major heading
        if stripped.startswith("##"):
            break
        # Stop at the entry separator
        if _ENTRY_SEPARATOR_RE.match(stripped):
            break
        # Stop at the next sibling section: an UNINDENTED bold-label
        # bullet (matched on the raw line so the block's own indented
        # bullets never terminate the count).
        if _SECTION_BULLET_RE.match(line):
            break
        # Count substantive bullets (require non-trivial content after the marker)
        if stripped.startswith("- ") or stripped.startswith("* "):
            body = stripped[2:].strip()
            if body and not body.lower().startswith(("tbd", "todo", "n/a")):
                bullet_count += 1
                bullet_lines.append(body[:80])

    if bullet_count < min_bullets:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"Self-Critique block has {bullet_count} bullets; minimum required is {min_bullets}"
            ),
            details=bullet_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="self_critique",
        passed=True,
        message=f"{bullet_count} failure modes listed",
        duration_seconds=time.monotonic() - start,
    )


def _tamper_changes(prd: PRD, pre_run_prd_path: Path | None) -> list[str]:
    """How ``prd`` differs from the pre-run copy in ways no engineer may.

    Defence in depth for #264's carve-out, kept deliberately after #269
    made the SCOPE half of this comparison unnecessary. The plan-time
    snapshot (``kstrl.scope``) settles what a component may write, so an
    ``allowedPaths`` the agent edits is inert and is no longer compared:
    see that module for why comparing a value the agent can rewrite is
    the weaker answer.

    What the snapshot does NOT cover is everything else that reads this
    file, and a lot does: ``check_prd_stories`` below, the approved
    fixtures oracle, the acceptance criteria handed to the reviewer, the
    R10.3 claim check. None can be served from a snapshot, because
    the agent setting ``passes`` is the whole job, so the live file has
    to be trusted and a comparison is the only answer available for it.
    Drop this and an agent can delete an acceptance criterion or neuter
    an executable oracle and pass a gate it authored.

    WHICH fields are the engineer's to write is the PRD's own business
    and lives in ``PRD.tamper_changes``, beside the fields it pins.

    Empty when there is nothing to compare, which is not the same as
    waving something through:

    - ``pre_run_prd_path`` is None. The caller has no trustworthy copy
      to offer: ``ks check`` judges an operator's own working tree.
    - It is the SAME file, which is ``use_worktrees=False``: both reads
      return the same document and the comparison is empty by
      arithmetic rather than by a special case. That mode has no
      isolation boundary, so this check cannot be what gives it one.
      Scope is the part of the answer that does survive there, because
      #269 reads it before the agent starts.

    A pre-run copy that will not load raises what ``PRD.load`` raises,
    ``OSError`` or ``ValueError``, and ``check_prd_stories`` fails closed
    on it (#568). It was read once already, at plan time, so it went
    missing or changed during the run, and an empty comparison would be
    the mechanism removed without a word.
    """
    if pre_run_prd_path is None:
        return []
    return prd.tamper_changes(PRD.load(pre_run_prd_path))


#: H3 (#303): fragments check_prd_stories' tamper branch assembles;
#: versioned as one body (docs/adversarial-roadmap.md, H3a sweep row).
PRD_TAMPER_PROMPT_VERSION = "1.0.0"

PRD_TAMPER_FIELDS_PROMPT = (
    "It {changes}. A component may set `passes` "
    "and `notes` on its own stories and nothing else: it may "
    "not rewrite the criteria or the fixtures it is judged "
    "against."
)
PRD_TAMPER_GATES_PROMPT = (
    "Every gate that reads this file - these stories, the "
    "approved fixtures, the criteria the reviewer is given - "
    "is judging a document the component rewrote. Restore it "
    "to what the run started with; do not treat this as "
    "permission to change what the component is measured "
    "against."
)


def check_prd_stories(prd_path: Path, pre_run_prd_path: Path | None = None) -> CheckResult:
    """Re-read PRD from disk and verify all stories have passes=true.

    ``pre_run_prd_path`` (#269) is the copy of the same PRD the run
    started with, which lives outside every worktree and so is not
    agent-writable. Given one, this check also refuses a PRD the
    component rewrote in its own favour (``_tamper_changes``).

    This is the check that carries that refusal, rather than
    ``diff_scope``, for two reasons. It is a statement about the
    STORIES, which is what this check reads and what a rewrite attacks;
    scope stopped being the question when #269 made the plan-time
    snapshot the only scope source. And ``diff_scope`` is switchable off
    (``[verify] check_diff_scope``), while this one runs whenever there
    is a PRD at all: defence in depth an unrelated toggle can disable is
    not defence in depth.
    """
    start = time.monotonic()
    try:
        prd = PRD.load(prd_path)
    except Exception as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"Failed to load PRD: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    try:
        tampered = _tamper_changes(prd, pre_run_prd_path)
    except (OSError, ValueError) as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=(
                f"The PRD this run started from, {pre_run_prd_path}, could not be "
                f"read; failing closed: {exc}"
            ),
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    if tampered:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message="The PRD is not the one this run started with; failing closed",
            details=[
                PRD_TAMPER_FIELDS_PROMPT.format(changes="; ".join(tampered)),
                PRD_TAMPER_GATES_PROMPT,
            ],
            duration_seconds=time.monotonic() - start,
        )

    failing = [s for s in prd.user_stories if not s.passes]
    if failing:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"{len(failing)} stories not marked as passing",
            details=[f"{s.id}: {s.title}" for s in failing],
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="prd_stories",
        passed=True,
        message=f"All {len(prd.user_stories)} stories passing",
        duration_seconds=time.monotonic() - start,
    )


#: The most characters of one gate's output kept for its log (#462).
#: Measured on kstrl's own tree: one failing test printed 699 characters,
#: 179 collection errors 273,184, mypy with 3,981 error lines 352,260, and
#: a shared helper broken under the full suite (1,265 failures) 4,523,001,
#: of which the closing short summary was the last 141,403. At this bound
#: the first three are kept whole and the fourth keeps its full summary.
GATE_OUTPUT_MAX_CHARS = 1_048_576


def bounded_gate_output(output: str, limit: int = GATE_OUTPUT_MAX_CHARS) -> str:
    """``output`` whole when it fits in ``limit`` characters; else its
    first and last ``limit // 2`` characters with a line between them
    saying how many were dropped (#462).

    Both ends, because they answer different questions: the head holds
    the first error, which is usually the cause (a collection error, an
    import failure), and the tail holds the tool's summary (pytest's
    short summary, mypy's count, and stderr, which comes after stdout).
    """
    if len(output) <= limit:
        return output
    half = limit // 2
    dropped = len(output) - 2 * half
    marker = (
        f"\n[kstrl: output truncated, {dropped} of {len(output)} characters "
        f"dropped here; kept the first {half} and the last {half}]\n"
    )
    return output[:half] + marker + output[-half:]


def _output_before_stop(stdout: bytes | str | None, stderr: bytes | str | None) -> str:
    """What a gate printed before it timed out or printed bytes that are
    not utf-8, joined and bounded as any failed gate's output is (#527).

    A hung test suite is the case where the operator most needs the last
    lines it printed, so these two exits keep the output the exception
    carries rather than returning a message alone.
    """
    return bounded_gate_output((_readable(stdout) + _readable(stderr)).strip())


def run_fast_checks(worktree_path: Path, config: VerifyConfig) -> VerificationResult:
    """The gates ``config.fast_iteration_checks`` names, run between
    engineer iterations (#233), in Phase 1's order.

    Each gate is the same per-gate function
    :func:`run_mechanical_verification` calls, with the same command
    and timeout, so the reading handed to the next iteration is the
    reading Phase 1 would take of the same tree. Direct calls rather than
    a lookup table, because every static guard that resolves a spawn's
    callee has to be able to read these three.
    """
    selected = validate_fast_iteration_checks(
        config.fast_iteration_checks, "[verify] fast_iteration_checks", gate_names(config)
    )
    checks, gaps = _command_gates(worktree_path, config, selected)
    return VerificationResult(
        passed=all(check.passed for check in checks), checks=checks, not_measured=gaps
    )


#: The statuses a shell returns when it could not run the command at all:
#: 126 found but not executable, 127 not found. A ``[stack]`` check that
#: ends with one failed and measured nothing (#696 decision 4).
SHELL_COULD_NOT_RUN: frozenset[int] = frozenset({126, 127})


def check_stack_command(
    cwd: Path,
    stack: Stack,
    name: str,
    command: str,
    timeout: float | None,
    rung: Rung | None = None,
) -> CheckResult:
    """Run one ``[stack]`` check in ``cwd``: the row ``stack:<name>`` (#696).

    ``rung`` is the run's TEST-zone rung, or None on the host (#700 slice 2).

    The verdict is the exit status (decision 4): 0 passes, and any other
    completed exit is a MEASURED failure. 126, 127, a timeout and output that
    is not utf-8 fail UNMEASURED. kstrl parses none of the output to decide;
    the details are the lines around each location inside ``cwd``, or the
    last five lines when the output names none.

    A stack no person confirmed runs nothing (slice 3): the row fails
    UNMEASURED with the reason, so every phase that reads it refuses.
    """
    row = f"stack:{name}"
    if stack.unconfirmed:
        refused = f"`{command}` not run: the [stack] in kstrl.toml {stack.unconfirmed}"
        return CheckResult(name=row, passed=False, message=refused, measured=False, output=refused)
    start = time.monotonic()
    try:
        result = run_scrubbed(command, cwd=cwd, timeout=timeout, declared_env=stack.env, rung=rung)
    except subprocess.TimeoutExpired as expired:
        return CheckResult(
            name=row,
            passed=False,
            message=f"`{command}` timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
            measured=False,
            output=_output_before_stop(expired.stdout, expired.stderr),
        )
    except ChildOutputDecodeError as exc:
        return CheckResult(
            name=row,
            passed=False,
            message=f"`{command}` output could not be decoded: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
            output=_output_before_stop(exc.stdout, exc.stderr),
        )
    if result.returncode == 0:
        return CheckResult(
            name=row,
            passed=True,
            message=f"`{command}` exited 0",
            duration_seconds=time.monotonic() - start,
        )
    output = (result.stdout + result.stderr).strip()
    excerpt = failure_excerpt(output, cwd) or "\n".join(output.splitlines()[-5:])
    return CheckResult(
        name=row,
        passed=False,
        message=f"`{command}` exited {result.returncode}",
        details=excerpt.splitlines(),
        duration_seconds=time.monotonic() - start,
        measured=result.returncode not in SHELL_COULD_NOT_RUN,
        output=bounded_gate_output(output),
    )


def _stack_gates(
    worktree_path: Path, config: VerifyConfig, stack: Stack, selected: Sequence[str]
) -> list[CheckResult]:
    """The ``[stack]`` checks named in ``selected``, in the stack's order (#696)."""
    timeout = limit_seconds(config.subprocess_timeout)
    return [
        check_stack_command(worktree_path, stack, name, command, timeout, config.rung)
        for name, command in stack.checks
        if name in selected
    ]


#: Phase 1's one row when no ``[stack]`` names its checks (#696 slice 4).
#: Every entry point refuses before this is reached (``stack.NO_STACK``); a
#: caller that builds a :class:`VerifyConfig` with no stack gets a failed,
#: unmeasured row here, never a pass and never a command kstrl chose.
NO_STACK_CHECK = "stack"


def _command_gates(
    worktree_path: Path, config: VerifyConfig, selected: Sequence[str]
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """The ``[stack]`` checks named in ``selected``, in the stack's order (#696).

    Shared by :func:`run_mechanical_verification` and :func:`run_fast_checks`.
    With no stack there is nothing to run, and the one row says so: no
    command is chosen in its place.
    """
    if config.project_stack is None:
        return [
            CheckResult(
                name=NO_STACK_CHECK,
                passed=False,
                message=NO_STACK,
                measured=False,
                output=NO_STACK,
            )
        ], []
    return _stack_gates(worktree_path, config, config.project_stack, selected), []


#: What the two diff-driven checks report when the diff handed them nothing.
#:
#: One constant because the baseline turns a row's message into the REASON a
#: check is unmeasured, and round 2 of review on #357 found the two checks
#: disagreeing about the same empty diff - one measured, one did not. They sit
#: on the same `git diff`, so they answer this question together or the
#: mechanism is a coin toss over which check the operator configured.
NO_FILES_IN_THE_DIFF = "no files in the diff"

#: H3 (#303): fragments _diff_scope_details assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
DIFF_SCOPE_DETAILS_PROMPT_VERSION = "1.0.0"

DIFF_SCOPE_BASE_BRANCH_PROMPT = (
    "Base branch: {base_branch} "
    "(scope is judged on `git diff {base_branch}...HEAD`; "
    "do NOT `git checkout {base_branch} -- <path>`, revert only "
    "your own out-of-scope commits/edits)"
)
DIFF_SCOPE_ALLOWED_PATHS_PROMPT = "Allowed paths (complete list): {allowed_paths}"
DIFF_SCOPE_HARNESS_PATHS_PROMPT = (
    "Plus harness artifacts (kstrl's own files, already in "
    "scope, no need to widen allowedPaths): {harness_paths}"
)
DIFF_SCOPE_VIOLATIONS_PROMPT = "Files outside allowed scope:\n{violations}"
DIFF_SCOPE_TRUNCATION_PROMPT = "  ... and {count} more"


def _diff_scope_details(
    base_branch: str,
    allowed_paths: list[str],
    harness_paths: list[str] | None,
    violations: list[str],
) -> list[str]:
    """Failure details for a diff that left its scope.

    R0.4: name the base branch and the FULL allowed-paths list. Without
    them the retry agent has to guess both; the recorded e2e run guessed
    `main` as base and reverted base-branch content with `git checkout
    main -- ...`, failing again. Base branch and allowed paths are single
    detail entries at the head of the list so
    ``VerificationResult.as_context()``'s ``details[:10]`` slice carries
    them into the retry prompt verbatim.

    #264: the harness carve-out is its own entry, never folded into the
    authored list. The operator has to be able to read what THEY
    authorised, and the retry agent has to know its own PRD and progress
    log are already in scope - telling it to stop writing those is the
    one instruction it cannot obey and still pass ``prd_stories``.
    """
    shown = violations[:15]
    violation_lines = [f"  - {v}" for v in shown]
    if len(violations) > len(shown):
        violation_lines.append(
            DIFF_SCOPE_TRUNCATION_PROMPT.format(count=len(violations) - len(shown))
        )
    harness_note = (
        [DIFF_SCOPE_HARNESS_PATHS_PROMPT.format(harness_paths=", ".join(harness_paths))]
        if harness_paths
        else []
    )
    return [
        DIFF_SCOPE_BASE_BRANCH_PROMPT.format(base_branch=base_branch),
        DIFF_SCOPE_ALLOWED_PATHS_PROMPT.format(allowed_paths=", ".join(allowed_paths)),
        *harness_note,
        # One multi-line entry so as_context()'s details[:10] slice
        # cannot drop violations or the truncation marker.
        DIFF_SCOPE_VIOLATIONS_PROMPT.format(violations="\n".join(violation_lines)),
    ]


#: The Phase 1 check name for "no trustworthy scope could be read".
#:
#: Deliberately NOT ``scope_source``, which is already taken in the same
#: substrate: ``events.ComponentScopeResolved.scope_source`` is a
#: payload FIELD naming which authority supplied a component's
#: allowlist (component_prd / run_flag / unconstrained / unresolved).
#: A check of that name reaches the same ``events.jsonl`` as a VALUE in
#: ``VerificationResultEvent.checks``, so one token would carry two
#: unrelated meanings for the dashboards that read that file.
SCOPE_UNREADABLE_CHECK = "scope_unreadable"


#: Opening words of the failure recorded when a component is refused for
#: an unreadable scope. Load bearing twice over, so it is a constant
#: rather than a literal: ``evolution._classify_check`` matches on it to
#: recover the check name from a manifest written by an earlier process,
#: and it is what an operator sees first in the inbox, the notification
#: and ``comp.error``.
SCOPE_UNREADABLE_ERROR_PREFIX = "Component scope could not be read; retrying cannot change it"


def scope_unreadable_error(cause: str) -> str:
    """The recorded error for an unreadable scope, carrying its cause.

    ``pipeline.fail`` writes this to ``comp.error``, the
    ``ComponentFailed`` event, ``notify.fire_first_failure`` and the
    HALTED_RUN inbox item's detail. A fixed string left all four saying
    only THAT the scope was unreadable, while the file to restore sat in
    the check's details, where none of them look.
    ``factory._preflight_component_scope`` names the file in its own
    refusal; every refusal for this cause should read alike.
    """
    return f"{SCOPE_UNREADABLE_ERROR_PREFIX}. {cause}"


#: Rendered in place of an empty ``allowed_paths_error``. A fail-closed
#: check must not pass on an ambiguous sentinel (round 2), and it must
#: not refuse while naming no cause either (round 1). It refuses, and
#: says the cause is missing.
NO_CAUSE_RECORDED = "(no cause recorded; the scope resolver supplied an empty error)"

#: H3 (#303): fragments check_scope_unreadable assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
SCOPE_UNREADABLE_PROMPT_VERSION = "1.0.0"

SCOPE_UNREADABLE_EXPLANATION_PROMPT = (
    "The allowedPaths this component must be judged against "
    "could not be established before the run started, so no "
    "diff can be proven in-scope. This is NOT a diff violation, "
    "and NOT something an engineer can fix from inside the "
    "worktree: the scope is read from the pre-run checkout, "
    "outside this worktree, and is fixed for the life of the "
    "run, so neither narrowing nor widening the diff changes "
    "this verdict."
)
SCOPE_UNREADABLE_REMEDY_PROMPT = (
    "The Error line above names which of two faults this is. A "
    "pre-run PRD that would not read or parse: restore that "
    "file in the main checkout and start a new run. No "
    "plan-time scope resolved for this component at all: the "
    "PRD is not the problem, the manifest and the run's "
    "resolved scope disagree about which components exist, and "
    "that is a harness fault to report rather than a file to "
    "repair. A run-wide --allowed-paths fixes neither: scope "
    "resolution refuses before it reaches the flag, so a re-run "
    "with it set fails identically."
)


def check_scope_unreadable(allowed_paths_error: str) -> CheckResult:
    """Report that no trustworthy scope could be established (R1.5, #294).

    Fails CLOSED: no allowlist could be read, so no diff can be proven
    in-scope, and silently skipping the guard is the hole R1.5 exists to
    close. Distinct from ``allowed_paths=None`` reaching
    ``check_diff_scope``, which means no scope was CONFIGURED -- a
    legitimate pass.

    Its own check, and not a branch of ``diff_scope``, because the two
    name different faults and the name is what a reader acts on (#294).
    ``diff_scope`` means "the diff touched files outside the allowlist",
    so its retry context is read as "narrow the diff". Here there was no
    allowlist to be outside of: it is resolved once at plan time from
    the pre-run checkout (``scope.ComponentScope``), which is OUTSIDE
    every worktree and fixed for the life of the run, so nothing the
    engineer writes can move this verdict.

    TWO producers, with different remedies, which is why the text points
    at the ``Error:`` line rather than asserting a cause:

    - ``ComponentScope.resolve`` could not read or parse the component's
      pre-run PRD. Restore that file.
    - ``RunScope.for_component`` had no snapshot for the component at
      all and returned its fail-closed stand-in. The PRD is fine; the
      manifest and the resolved run scope disagree about which
      components exist, which is a harness fault.

    An earlier version asserted the first cause unconditionally, so on
    the second it sent an operator to inspect a file that reads
    perfectly. That is round-1 finding 1 again: a remediation naming an
    action that cannot fix the failure.

    Neither remedy is ``--allowed-paths``. ``resolve`` returns
    ``unresolved`` BEFORE it consults the run-wide flag, on the argument
    that a scope nobody could read is not a scope that does not exist,
    so a run restarted with the flag hits the identical refusal.

    Carries an infrastructure ``Finding`` because this is the harness
    failing to establish its own input, not a judgement about the
    change. Without it a run that dies here leaves an empty finding
    stream, and every consumer using ``len(findings) == 0`` as "ran
    cleanly" reads a hard stop as clean.

    Whether it runs at all is ``_scope_checks``'s decision, and it is
    ungated there.
    """
    start = time.monotonic()
    cause = allowed_paths_error or NO_CAUSE_RECORDED
    return CheckResult(
        name=SCOPE_UNREADABLE_CHECK,
        # #227: this row is a refusal about an INPUT nobody could read, so
        # it measured nothing about the diff. `passed` is untouched and the
        # gate still fails closed; what `measured=False` buys is that the
        # signature never enters a baseline, so repairing the harness is not
        # reported as a fix and the check leaving `measured_checks` is not
        # reported as a check that stopped.
        passed=False,
        measured=False,
        message="Scope could not be read at plan time; failing closed",
        details=[
            f"Error: {cause}",
            SCOPE_UNREADABLE_EXPLANATION_PROMPT,
            SCOPE_UNREADABLE_REMEDY_PROMPT,
        ],
        findings=[
            Finding.infrastructure_error(
                "verify",
                f"component scope could not be established at plan time: {cause}",
            )
        ],
        duration_seconds=time.monotonic() - start,
    )


def check_diff_scope(
    cwd: Path,
    base_branch: str,
    allowed_paths: list[str] | None = None,
    *,
    harness_paths: list[str] | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope.

    One question only: did the diff touch a file outside the allowlist?
    The allowlist not being READABLE is a different fault with a
    different audience, and it is ``check_scope_unreadable`` (#294).

    It no longer carries PRD TAMPERING either. That refusal moved to
    ``check_prd_stories`` when the plan-time snapshot took the scope
    question away from the worktree PRD: the file can still be rewritten
    and the stories still have to be defended, but the scope this check
    enforces is not something the rewrite can reach any more, so saying
    "scope could not be established" about it was untrue.

    ``harness_paths`` (#264) is kstrl's OWN per-component carve-out from
    ``config.component_harness_paths``: exact files kstrl's other checks
    require the agent to write (its PRD, its progress log, the codebase
    map). They widen the effective scope but are reported SEPARATELY, so
    the failure message still shows the operator what they authorised.
    They never create a scope where none was configured: with
    ``allowed_paths`` unset the check still passes unconditionally.

    Keyword-only, because #294 deleted an ``allowed_paths_error``
    parameter that sat in the 4th positional slot and this argument
    would otherwise have inherited it. Measured on the intermediate
    version: an unported caller passing the error string positionally
    got ``passed=True`` / "No scope constraints" where it intended a
    hard refusal, and with a non-empty ``allowed_paths`` the string
    splatted character by character into the effective allowlist. A
    silent fail-open is the one failure mode this check exists to
    prevent.
    """
    start = time.monotonic()

    if not allowed_paths:
        # #227: a VACUOUS pass. It reads no diff and applies no rule, so it
        # proves nothing about scope. `ks check` with no --allowed-path takes
        # this branch every time, and with measured=True it cleared: measured
        # on the head of #357, a baseline carrying
        # `diff_scope:files-outside-allowed-scope-diff-vs-base-branch` was
        # reported FIXED by a run that never looked.
        return CheckResult(
            name="diff_scope",
            passed=True,
            message="No scope constraints (allowed_paths not set)",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    # #264: the authored scope plus kstrl's own per-component files. The
    # two lists stay separate all the way into the failure details: an
    # operator reading "outside allowed scope" must be able to tell what
    # they authorised from what the harness added on their behalf.
    #
    # Deliberately NOT guards.check_violations, which is the same
    # decision on the same inputs: it takes a set and returns sorted, and
    # the violation list is truncated to 15 for the retry prompt, so
    # sorting silently changes WHICH violations the retry agent is shown.
    # Git's order is the order the operator sees elsewhere; a cosmetic
    # de-duplication is not worth moving it.
    effective = [*allowed_paths, *(harness_paths or ())]
    try:
        changed = git.get_diff_names(base_branch, cwd)
        # #435: name the ref the diff was actually judged against.
        # get_diff_names resolved it; saying "main" while measuring
        # origin/main sends the engineer to revert against the wrong tree.
        base_label = git.resolve_base_ref(base_branch, cwd)
        violations = [f for f in changed if not path_is_allowed(f, effective)]
    except git.GitDiffError as exc:
        # The lenient reader raises for exactly one family: a diff git
        # produced and this process cannot decode (#416). Everything else it
        # still answers with [], which the vacuous-pass branch below handles.
        # Failing closed here rather than falling into that branch is the
        # point: an undecodable diff is not an empty one.
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                "diff scope could not read the diff; failing closed "
                "(infrastructure error, not a scope pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "verify",
                    f"diff scope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    if not changed:
        # The other vacuous pass, and the one round 1 of #357 missed: the rule
        # exists but there is nothing to apply it to. Round 2 of review
        # measured the two diff-driven checks side by side on one empty diff
        # and found them disagreeing - `diff_scope` measured, `bad_patterns`
        # did not - so an adopter who sets --allowed-path had every
        # `diff_scope` baseline signature CLEARED by a pull request whose diff
        # touched none of the allowed globs.
        return CheckResult(
            name="diff_scope",
            passed=True,
            message=NO_FILES_IN_THE_DIFF,
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    if violations:
        details = _diff_scope_details(
            base_label,
            allowed_paths,
            harness_paths,
            violations,
        )
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                f"{len(violations)} files outside allowed scope "
                f"(diff vs base branch '{base_label}')"
            ),
            details=details,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="diff_scope",
        passed=True,
        message=f"{len(changed)} files, all within scope",
        duration_seconds=time.monotonic() - start,
    )


#: The ``bad_patterns`` row's words when the ``[policy]`` envelope runs and
#: owns the secret rule (#646 slice 4).
SECRETS_CHECKED_BY_ENVELOPE = "secrets: checked by policy_envelope"


def _bad_patterns_message(
    issues: Sequence[str],
    changed: Sequence[str],
    *,
    added_any: bool,
    secrets_owned_by_envelope: bool,
) -> str:
    """The ``bad_patterns`` row's message: what the secret rule read, and what it found."""
    if issues:
        return f"{len(issues)} issues found in changed files"
    if not changed:
        # The same sentence as check_diff_scope when the cause is the same,
        # so an operator reading two unmeasured rows in one report does not
        # have to work out whether two spellings mean one fact.
        return NO_FILES_IN_THE_DIFF
    if secrets_owned_by_envelope:
        return SECRETS_CHECKED_BY_ENVELOPE
    if not added_any:
        return f"secrets: the {len(changed)} changed files add no lines, nothing scanned"
    return f"secrets: scanned the lines added to {len(changed)} changed files, no issues"


def check_bad_patterns(
    cwd: Path,
    base_branch: str,
    secret_patterns: Sequence[str] = DEFAULT_SECRET_PATTERNS,
    *,
    secrets_owned_by_envelope: bool = False,
) -> CheckResult:
    """Scan the lines this branch added for a secret.

    One rule, the same for every file whatever its language (#696 slice 8
    removed the empty-file and syntax-error rules, which read one source
    language). Which files add a secret is a property of the DIFF, not of
    a file (#399 simplify pass): it is computed once, by intersecting the
    lines this branch ADDED with ``secret_patterns`` via
    ``policy._scan_secrets`` - the same function ``check_policy_envelope``
    evaluates its own ``[policy] secret_patterns`` through - one rule, not
    two copies of it. The result is keyed by PATH, which is safe because
    ``policy.parse_added_lines`` (#399 addendum) unquotes a git-quoted path
    before comparing it to ``git diff --name-status``'s own, unquoted
    spelling of the same file. The caller passes the envelope's own
    ``PolicyConfig.secret_patterns`` when it has one; ``PolicyConfig.load``
    reads that field unconditionally, whether or not ``[policy] enabled``
    is true, so a stock install (no config at all) keeps this default,
    which is that same list.

    ``secrets_owned_by_envelope`` (#646 slice 4) is true when the
    ``[policy]`` envelope runs in the same verification. This check then
    neither reads the diff's added lines nor scans them for secrets:
    ``check_policy_envelope`` reports a secret once, as a finding an inbox
    approval can waive, and this row says
    ``secrets: checked by policy_envelope`` and measured nothing. False,
    the default, keeps the secret rule here, with no approval path.
    """
    start = time.monotonic()

    # #399: which changed files add a secret. The rule reads the added
    # lines of EVERY changed file (#619). One name-status call: the
    # comprehension below is `git.get_diff_names`' dedupe written out.
    # `get_diff_name_status` is inside the try as of #414: it is lenient
    # about a diff git could not produce but raises on one it could not
    # DECODE, and outside the try that left this blocking gate as a
    # traceback (PR #419 handoff 1).
    try:
        records = git.get_diff_name_status(base_branch, cwd)
        changed = list(dict.fromkeys(path for _, path in records if path))
        # get_diff_name_status is LENIENT (returns [] on a git failure, not
        # only on a genuinely empty diff), but get_diff_content is not: it
        # raises. Reading content only when there is a changed-file list
        # keeps that lenient behaviour for "no diff at all" (#619): a
        # worktree with no git repository reaches the vacuous pass below
        # rather than the exception clause. `as_stored` (#695): every
        # changed byte, so a file git treats as binary is read too.
        added = (
            []
            if secrets_owned_by_envelope or not changed
            else parse_added_lines(git.get_diff_content(base_branch, cwd, as_stored=True))
        )
        secret_hit_paths = frozenset(_scan_secrets(added, secret_patterns))
    except Exception as exc:
        # Exception exactly, broad clause last (#318). get_diff_name_status
        # is LENIENT, so the file list can arrive when the diff does not; at
        # least two unrelated families are measured reaching here: a
        # GitDiffError, and a UnicodeDecodeError (a ValueError) from a
        # diff this process could not decode. A misconfigured secret
        # pattern (PolicyConfigError, also a ValueError, raised inside
        # `_scan_secrets`) reaches the same clause for the same reason:
        # this check runs by default, so a bad regex must fail this row
        # closed rather than crash the whole verification run. The row
        # fails CLOSED, so a swallow costs a visible red gate, never a
        # silent pass.
        return CheckResult(
            name="bad_patterns",
            passed=False,
            message=(
                "bad patterns could not read the diff; failing closed "
                "(infrastructure error, not a scan pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "verify",
                    f"bad patterns could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    issues = [
        f"{path}: possible secret/credential detected"
        for path in changed
        if path in secret_hit_paths
    ]
    message = _bad_patterns_message(
        issues,
        changed,
        added_any=bool(added),
        secrets_owned_by_envelope=secrets_owned_by_envelope,
    )
    return CheckResult(
        name="bad_patterns",
        passed=not issues,
        message=message,
        details=issues,
        duration_seconds=time.monotonic() - start,
        # #227: a scan that read nothing is a vacuous pass. It cannot prove
        # a secret went away. The rule reads the diff's added lines, so an
        # added line in any file counts.
        measured=bool(added),
    )


#: H3 (#303): the fragment check_policy_envelope assembles into its
#: diff-unreadable refusal text.
POLICY_ENVELOPE_PROMPT_VERSION = "1.0.0"

POLICY_DIFF_UNREADABLE_PROMPT = (
    "The change cannot be proven within policy; do not treat this as permission to merge."
)


def check_policy_envelope(
    cwd: Path,
    base_branch: str,
    config: PolicyConfig,
    *,
    waivers: Waivers | None = None,
) -> CheckResult:
    """R8.1: enforce the declarative ``[policy]`` envelope from artifacts.

    Reads the git diff, never agent self-report.
    Fails CLOSED on any infrastructure error (diff unreadable, malformed
    policy) and on any envelope violation. Enforcement-machinery edits
    are a non-overridable halt. Violation details are packed as
    individual entries so ``VerificationResult.as_context()``'s
    ``details[:10]`` slice carries them into the retry prompt.
    """
    start = time.monotonic()
    # All three reads are strict: each is a SEPARATE git subprocess, so a
    # successful content read proves nothing about the two that follow.
    # A lenient read returns [] on timeout/nonzero exit, which the
    # evaluator cannot distinguish from "nothing changed" - the change
    # would then satisfy every path and size rule vacuously. `as_stored`
    # (#695): every changed byte, so a file git treats as binary is read too.
    try:
        diff_text = git.get_diff_content(base_branch, cwd, as_stored=True)
        changed = git.get_diff_names(base_branch, cwd, strict=True)
        numstat = git.get_diff_numstat(base_branch, cwd, strict=True)
    except git.GitDiffError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not read the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[
                f"Error: {exc}",
                POLICY_DIFF_UNREADABLE_PROMPT,
            ],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    try:
        evaluation = evaluate_policy(changed, numstat, diff_text, config)
    except PolicyConfigError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message="policy envelope is misconfigured; failing closed",
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope is misconfigured: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except Exception as exc:
        # Exception exactly, broad clause last (#318). evaluate_policy calls
        # policy.parse_added_lines on the diff text this function already
        # read, and a diff header path holding bytes that are not valid
        # utf-8 makes that raise UnicodeDecodeError - a ValueError, and
        # neither a GitDiffError (the diff itself DID read) nor a
        # PolicyConfigError (the config is fine). The row fails CLOSED
        # rather than let the exception escape the check (#399 blocker 1b).
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not evaluate the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not evaluate the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    findings = [
        Finding.policy_violation(
            category=v.category,
            explanation=v.explanation,
            location=v.location,
            severity=v.severity,
            suggestion=v.suggestion,
        )
        for v in evaluation.violations
    ]
    # #595: an approved inbox item waives the one finding it covers, and
    # it does so HERE, before `blocking` is computed, so this check stays
    # the only place a policy finding becomes blocking.
    findings, waived, refusals = apply_waivers(findings, waivers)
    blocking, advisories, details = _after_waivers(findings, refusals)
    note = waiver_note(waived, refusals)

    if not blocking:
        message = evaluation.summary if evaluation.ok else "policy envelope satisfied after waivers"
        if advisories:
            message += f"; {len(advisories)} advisory(ies)"
        message += note
        return CheckResult(
            name="policy_envelope",
            passed=True,
            message=message,
            details=details,
            findings=findings,
            duration_seconds=time.monotonic() - start,
        )
    message = f"{len(blocking)} policy violation(s)"
    if evaluation.machinery_hit:
        message += " including enforcement-machinery halt"
    message += note
    return CheckResult(
        name="policy_envelope",
        passed=False,
        message=message,
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _after_waivers(
    findings: list[Finding], refusals: list[str]
) -> tuple[list[Finding], list[Finding], list[str]]:
    """``(blocking, advisories, details)`` once waivers are applied (#595).

    ``advisories`` leaves out waived findings, so a waiver is not counted
    as an advisory. ``details`` puts blocking findings first, because
    ``as_context()`` slices ``details[:10]`` into the retry prompt and
    nothing may crowd out a real failure, then the refusal reasons, the
    waived findings and the advisories.
    """
    blocking = [f for f in findings if f.severity != "advisory"]
    waived = [f for f in findings if f.severity == "advisory" and finding_waiver(f) is not None]
    advisories = [f for f in findings if f.severity == "advisory" and finding_waiver(f) is None]
    details = [f.explanation for f in blocking] + refusals
    details += [f.explanation for f in waived] + [f.explanation for f in advisories]
    return blocking, advisories, details


def _scope_checks(
    cwd: Path,
    base_branch: str,
    *,
    allowed_paths: list[str] | None,
    allowed_paths_error: str | None,
    harness_paths: list[str] | None,
    compare: bool,
) -> list[CheckResult]:
    """The scope checks Phase 1 appends, at most one of two.

    An unreadable scope source and an out-of-scope diff are alternatives
    rather than a check with a mode (#294), so the choice is made once,
    here, instead of inside a check that would then be named for the
    wrong one of them:

    - ``allowed_paths_error`` non-empty: ``scope_unreadable`` alone,
      UNGATED. The comparison is not merely turned off, it is
      unavailable - there is no trustworthy allowlist to compare
      against - so running ``check_diff_scope`` too would report a PASS
      ("no scope constraints") beside the refusal, which is the
      fail-open reading of the same state. The error wins even when a
      caller also supplies a list: a half-loaded state must not be
      judged on paths that may be stale.

      ``is not None``, not truthiness. Both review rounds hit this from
      opposite sides and both were right about the defect: truthiness
      lets an empty-string sentinel PASS a ``diff_scope`` that had no
      allowlist to compare, which is a fail-open in the one check whose
      job is to fail closed; ``is not None`` alone refused while naming
      no cause, rendering the bare "Error: ". Neither problem requires
      the other. This refuses on any non-None value and
      ``check_scope_unreadable`` substitutes
      :data:`NO_CAUSE_RECORDED` for the empty one, so an ambiguous
      sentinel is never read as permission and the refusal always says
      something. ``ComponentScope.resolve`` never produces "", but
      ``run_mechanical_verification`` is a public entry point.
    - otherwise ``diff_scope``, gated on ``compare``, which is
      ``[verify] check_diff_scope`` and nothing else. The one flag
      rather than the whole ``VerifyConfig``: this is the only field
      the decision reads, and the two ``list[str] | None`` arguments
      beside it are keyword-only so a transposition of the authored
      allowlist and the harness carve-out cannot type-check clean.

    Returns a list rather than taking the branch in
    ``run_mechanical_verification``: that function is already over the
    cyclomatic ratchet and is judged against its own previous value, so
    an ``if``/``elif`` there is a refusal at commit time.
    """
    if allowed_paths_error is not None:
        return [check_scope_unreadable(allowed_paths_error)]
    if compare:
        return [
            check_diff_scope(
                cwd,
                base_branch,
                allowed_paths,
                harness_paths=harness_paths,
            )
        ]
    return []


#: Every check :func:`run_mechanical_verification` appends that answers
#: its question by reading ``git diff <base>...HEAD``.
#:
#: Beside the function that appends them, because a caller that has no
#: measurable base has to know which checks that rules out, and deriving
#: the list by reading this module's source is how two callers end up
#: disagreeing about it.
DIFF_DEPENDENT_CHECKS: tuple[str, ...] = (
    "diff_scope",
    "bad_patterns",
    "policy_envelope",
)


def self_critique_progress_path(
    config: VerifyConfig,
    worktree_path: Path,
    prd_path: Path | None,
) -> Path | None:
    """The log ``check_self_critique`` would read, or None if it will not run.

    Read the log the engineer was actually pointed at: a factory
    component writes NEXT TO its PRD (the only location inside its
    allowedPaths), so resolving a repo-root default here would check a
    file that was never written and fail the component for the harness's
    own path confusion. An explicit config wins. ``prd_path`` is
    worktree-absolute at the factory call site, so the derived sibling is
    too; the join is a no-op for an absolute path and still anchors a
    relative one. With neither a PRD nor an explicit path there is no log
    to read, so the check is skipped rather than run against a path that
    cannot exist.

    Extracted (#288 review) because a caller has to be able to ask
    whether this check will run BEFORE the run, to say so: `ks feature`
    announces its report up front, and the announcement was silently
    wrong for an operator who had set ``require_self_critique``. Two
    copies of the rule is how the announcement and the run disagree, so
    there is one, and :func:`run_mechanical_verification` calls it too.
    """
    if not config.require_self_critique:
        return None
    if config.progress_file_path is not None:
        return worktree_path / Path(config.progress_file_path)
    if prd_path is not None:
        return worktree_path / component_progress_path(prd_path, None)
    return None


def run_undiffed_verification(
    worktree_path: Path,
    config: VerifyConfig,
) -> VerificationResult:
    """Mechanical verification over a tree with no base to diff against.

    The ONLY safe entry point for that case, and it is a function rather
    than a documented convention because :func:`narrow_to_undiffed`
    cannot deliver the guarantee its name promises (#288 review round
    2). Its ``replace`` reaches two of the three
    :data:`DIFF_DEPENDENT_CHECKS`; the third, ``policy_envelope``, is
    gated by ``policy_config``, a separate ARGUMENT to
    :func:`run_mechanical_verification`, and ``allowed_paths_error``
    outranks the ``check_diff_scope`` toggle entirely because
    :func:`_scope_checks` reads it first and appends the ungated
    ``scope_unreadable`` on any non-None value. So a second caller
    writing ``config=narrow_to_undiffed(cfg), policy_config=pc`` gets
    ``policy_envelope`` reporting a PASS over an empty diff: the exact
    defect the narrowing is named for, reintroduced by an argument the
    narrowing cannot see.

    This owns all of them. There is no parameter here for anything that
    consumes a diff, so the checks suppressed by config and the one
    suppressed by argument are suppressed the same way: by not being
    reachable.

    ``base_branch=""`` is the honest value for "there is no base here"
    and is never read, because nothing left running consumes one.
    ``prd_path=None`` skips the PRD-derived checks: ``prd_stories``
    re-reads a flag the agent itself set, which is a self-report rather
    than an independent measurement.

    The structural version of this - one object owning every argument
    that decides whether a check can honestly run - is tracked on #305.
    """
    return run_mechanical_verification(
        worktree_path=worktree_path,
        prd_path=None,
        base_branch="",
        allowed_paths=None,
        allowed_paths_error=None,
        config=narrow_to_undiffed(config),
    )


def narrow_to_undiffed(config: VerifyConfig) -> VerifyConfig:
    """``config`` with every :data:`DIFF_DEPENDENT_CHECKS` toggle off.

    Prefer :func:`run_undiffed_verification`, which owns the arguments
    this cannot reach. Exported on its own only because the announcement
    side of a report needs the narrowed config to say what will run.

    For a caller whose tree has no base it can honestly diff against -
    `ks feature` (#288), where nothing commits for the agent and the
    branch the loop checks out may BE the base branch, so
    ``base...HEAD`` is routinely empty and a diff-based check would
    report ``0 files, all within scope`` over work it never saw.

    An empty diff is indistinguishable from nothing changed: the lenient
    git helpers return an empty file list either way, and even
    ``get_diff_names(..., strict=True)`` returns ``[]`` without raising.
    So the only honest answer is not to run those checks, which is what
    this does.

    Note what it does NOT cover, because the toggles cannot. ``policy``
    is a separate config object and is suppressed by not being passed at
    all. And ``allowed_paths_error`` outranks ``check_diff_scope``
    entirely: :func:`_scope_checks` reads it first and, on ANY non-None
    value, appends :func:`check_scope_unreadable` instead, which is
    ungated by this config and fails closed by design (#294). So a caller
    relying on this narrowing must still leave that argument None, but
    for the opposite reason to the one that held before #294: the risk is
    no longer a ``diff_scope`` PASS over a diff it never saw, it is a
    hard scope_unreadable FAIL over a scope the caller never had.
    """
    return replace(config, check_diff_scope=False, check_bad_patterns=False)


class MechanicalVerification(Protocol):
    """The call shape of :func:`run_mechanical_verification` (#316).

    ``PipelineHooks.run_mechanical_verification`` was typed
    ``Callable[..., VerificationResult]``, and ``...`` means mypy checks
    NOTHING about the arguments - which matters because that hook is how
    the only call site carrying a real component's scope reaches the
    function. Measured on this branch: with the hook typed ``...``,
    swapping ``harness_paths=scope.harness_paths`` for
    ``harness_paths=scope.error`` - a ``str | None`` into a
    ``list[str] | None`` slot, an authored carve-out replaced by the
    snapshot's failure to read one - left ``mypy --strict`` reporting
    SUCCESS. With this Protocol the same swap is
    ``error: Argument "harness_paths" to "__call__" of
    "MechanicalVerification" has incompatible type "str | None";
    expected "list[str] | None"``.

    Making the arguments keyword-only stops a SLOT from being inherited
    silently; it cannot stop a wrong value being handed to the right
    name. Only a type can, and only if there is one.

    The defaults below are spelled as real values rather than the
    conventional ``= ...`` so that ``inspect.Signature`` equality can
    compare this to the function in one assertion; a Protocol that has
    drifted is worse than none, because it would type-check calls the
    function rejects. See
    ``test_the_protocol_says_exactly_what_the_function_says``.
    """

    def __call__(
        self,
        worktree_path: Path,
        prd_path: Path | None,
        base_branch: str,
        allowed_paths: list[str] | None,
        config: VerifyConfig,
        *,
        allowed_paths_error: str | None = None,
        harness_paths: list[str] | None = None,
        pre_run_prd_path: Path | None = None,
        policy_config: PolicyConfig | None = None,
        autonomy_level: int = 0,
        waivers: Waivers | None = None,
    ) -> VerificationResult: ...


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path | None,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
    *,
    allowed_paths_error: str | None = None,
    harness_paths: list[str] | None = None,
    pre_run_prd_path: Path | None = None,
    policy_config: PolicyConfig | None = None,
    autonomy_level: int = 0,
    waivers: Waivers | None = None,
) -> VerificationResult:
    """Run all mechanical checks. All checks run even if earlier ones fail.

    Everything after ``config`` is keyword-only (#316), so an inserted
    parameter cannot shift a later argument into a slot that means
    something else - and three of the arguments here mean opposite
    things in near-identical types (see :class:`MechanicalVerification`,
    which covers the half that keyword-only does not). Cost: none. No
    caller passed any of them positionally.

    ``prd_path=None`` (R10.1, ``ks check``) skips the PRD-dependent
    checks: ``prd_stories`` and ``self_critique`` unless
    ``config.progress_file_path`` names the log explicitly (with no PRD
    there is no sibling to derive it from). Every other check runs
    exactly as it does with a real path.

    ``harness_paths`` (#264) is the per-component carve-out for kstrl's
    OWN files, forwarded to ``check_diff_scope``. It reaches the factory
    from the run's plan-time scope snapshot (``scope.RunScope``), which
    is also where ``allowed_paths`` comes from; ``ks check`` leaves both
    None because it judges an operator's diff, not a factory
    component's.

    ``allowed_paths_error`` (#269) is that snapshot reporting that it
    could not read the component's scope at all. It replaces the
    ``diff_scope`` comparison with ``scope_unreadable``, an ungated
    fail-closed refusal named for its own cause (#294) - see
    ``_scope_checks``. Any non-None value refuses, empty included. ``ks check`` never sets it: it
    has no plan-time snapshot, so its scope is whatever
    ``--allowed-paths`` gave it.

    ``pre_run_prd_path`` (#269) is the copy of ``prd_path`` the run
    started with, forwarded to ``check_prd_stories``, which fails closed
    on a PRD the component rewrote. Also None for ``ks check``: there is
    no pre-run copy to compare an operator's working tree against.

    ``autonomy_level`` 1 or above records :data:`LAYER0_NOT_MEASURED` in
    :attr:`VerificationResult.not_measured` (#696 decision 7). The R8.5
    roadmap made Layer 0 blocking from L1, and kstrl no longer has a
    mechanical Layer 0, so the gap says so where Layer 0 ran. At level 0
    the ladder is off and nothing asked for Layer 0, so nothing is
    recorded. See :class:`NotMeasured` for why a gap is beside
    ``checks`` rather than in it.
    """
    checks: list[CheckResult] = []
    not_measured: list[NotMeasured] = []
    # #399 addendum: the envelope's own secret_patterns, read whether or not
    # [policy] enabled is true (PolicyConfig.load populates the field
    # unconditionally), so check_bad_patterns and check_policy_envelope
    # enforce one rule.
    bad_patterns_secret_patterns = (
        policy_config.secret_patterns if policy_config is not None else DEFAULT_SECRET_PATTERNS
    )
    # #646 slice 4: one owner for the secret rule. When the envelope runs it
    # reports a secret, waivably; check_bad_patterns then does not scan for
    # one, so a secret is one finding and an approval of it can pass.
    envelope = policy_config if policy_config is not None and policy_config.enabled else None

    if prd_path is not None:
        checks.append(check_prd_stories(prd_path, pre_run_prd_path))

    gate_rows, gate_gaps = _command_gates(worktree_path, config, gate_names(config))
    checks.extend(gate_rows)
    not_measured.extend(gate_gaps)

    checks.extend(
        _scope_checks(
            worktree_path,
            base_branch,
            allowed_paths=allowed_paths,
            allowed_paths_error=allowed_paths_error,
            harness_paths=harness_paths,
            compare=config.check_diff_scope,
        )
    )

    if config.check_bad_patterns:
        checks.append(
            check_bad_patterns(
                worktree_path,
                base_branch,
                bad_patterns_secret_patterns,
                secrets_owned_by_envelope=envelope is not None,
            )
        )

    # R8.1 policy envelope: opt-in ([policy] enabled). When disabled the
    # check is not appended, so existing runs are unchanged.
    if envelope is not None:
        checks.append(
            check_policy_envelope(
                worktree_path,
                base_branch,
                envelope,
                waivers=waivers,
            )
        )

    # #696 decision 7: where Layer 0 ran, say that nothing measured it.
    if autonomy_level >= 1:
        not_measured.append(LAYER0_NOT_MEASURED)

    progress_path = self_critique_progress_path(config, worktree_path, prd_path)
    if progress_path is not None:
        checks.append(
            check_self_critique(
                progress_path,
                config.self_critique_min_bullets,
            )
        )

    # ``checks`` only: a check that measured nothing neither passes nor
    # fails the run (#306).
    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks, not_measured=not_measured)
