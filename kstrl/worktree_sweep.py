"""Processes still running in a component worktree, found and killed (#461).

An engineer agent's shell tool starts each command in a process group of
its own. kstrl ends an agent by signalling the agent's group
(``kstrl.agents.proc``), so those commands are outside what it signals: a
test server the engineer started kept its port and ran code from a
deleted worktree for an hour after the run that spawned it ended.

The census keys on the one thing every such process carries whatever
group or session it is in: a working directory at or under the worktree.
``lsof`` lists the working directory of every process this user can see.
Each process found there has its whole group sent SIGKILL, because the
measured survivors ignored SIGTERM and a group leader's children share
its fate. Nothing here asks how the process was launched.

A census that could not be read is reported, never read as "nothing
found": ``error`` carries the reason and the callers record it.

:func:`leftover_agents` (#642) answers the same question for agent
process groups wherever they run, the project root included: a group
kstrl recorded at spawn whose kstrl process is gone and which still has
a running member. It reports and never signals (owner decision 1 (b)):
once the leash is dead the group has no leader to prove it is kstrl's,
and a kill would rest on the kernel never reusing the id of a leaderless
group, which has not been measured.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.agents.spawn_record import SpawnRecord, forget_spawn_record, read_spawn_records
from kstrl.findings import Finding
from kstrl.procgroup import pid_is_alive, read_group_members, signal_group
from kstrl.verify import ChildOutputDecodeError, run_scrubbed

if TYPE_CHECKING:
    from kstrl.ui.base import UI

logger = logging.getLogger(__name__)

#: Every visible process's working directory, one field per line:
#: ``p<pid>``, ``g<pgid>``, ``c<command>``, ``f<fd>``, ``n<path>``. ``-w``
#: drops the warnings for other users' processes, whose cwd we cannot read
#: and which cannot be ours.
LSOF_ARGV = ["lsof", "-w", "-n", "-P", "-a", "-d", "cwd", "-F", "pgcn"]

#: How long the census may take.
LSOF_TIMEOUT_SECONDS = 30.0

ORPHAN_CATEGORY = "orphan_process"


@dataclass(frozen=True)
class Survivor:
    """One process found running in the worktree, and what was done to it."""

    pid: int
    pgid: int
    command: str
    cwd: str
    #: Empty when SIGKILL reached its group or the group was already gone;
    #: otherwise why it was not signalled.
    not_killed: str = ""


@dataclass(frozen=True)
class WorktreeSweep:
    """What one sweep of one worktree found, or why it could not look."""

    survivors: tuple[Survivor, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class _Row:
    pid: int
    pgid: int
    command: str
    cwd: str


def sweep_worktree(worktree: Path) -> WorktreeSweep:
    """Kill the group of every process whose cwd is at or under ``worktree``."""
    if not worktree.exists():
        return WorktreeSweep()
    root = str(worktree.resolve())
    rows, error = _read_cwds(worktree.parent)
    if error:
        logger.warning("worktree sweep of %s could not run: %s", worktree, error)
        return WorktreeSweep(error=error)
    found = [row for row in rows if row.cwd == root or row.cwd.startswith(root + "/")]
    outcomes: dict[int, str] = {}
    survivors: list[Survivor] = []
    for row in found:
        if row.pgid not in outcomes:
            sent = signal_group(row.pgid, signal.SIGKILL)
            outcomes[row.pgid] = sent.refused or sent.denied
        survivor = Survivor(row.pid, row.pgid, row.command, row.cwd, outcomes[row.pgid])
        logger.warning("worktree sweep: %s", _describe(survivor))
        survivors.append(survivor)
    return WorktreeSweep(tuple(survivors))


def sweep_findings(sweep: WorktreeSweep, phase: str) -> list[Finding]:
    """One advisory finding per survivor, and one for a census that failed."""
    findings = [
        Finding(
            phase=phase,
            category=ORPHAN_CATEGORY,
            severity="advisory",
            location=s.cwd,
            explanation=_describe(s),
            tags=(ORPHAN_CATEGORY,),
        )
        for s in sweep.survivors
    ]
    if sweep.error:
        findings.append(
            Finding(
                phase=phase,
                category=ORPHAN_CATEGORY,
                severity="advisory",
                location="",
                explanation=f"the worktree process census could not run: {sweep.error}",
                tags=(ORPHAN_CATEGORY, "census_failed"),
            )
        )
    return findings


def warn_sweep(sweep: WorktreeSweep, ui: UI, phase: str) -> None:
    """One warning line per survivor, and one for a census that failed (#528).

    For the worktrees whose sweep has no component to hold a finding: the
    contract and integration temp worktrees, and the evidence worktree a
    retry removes. A factory run writes every warning to its events.jsonl.
    """
    for finding in sweep_findings(sweep, phase):
        ui.warn(f"  {ORPHAN_CATEGORY} ({phase}): {finding.explanation}")


def leftover_agents(root_dir: Path) -> WorktreeSweep:
    """Agent processes still running whose kstrl process is gone (#642).

    Reads every spawn record for ``root_dir``. A record whose owner is
    alive belongs to a run in progress and is skipped. Otherwise the
    record's group is read in the one ``ps`` listing, and every running
    member is a survivor, unless the group is empty or its id now leads
    a process whose command lacks the record's nonce: the recorded group
    is gone then, and the record is removed. Nothing is signalled.

    A record that cannot be read, or a group that cannot be listed, is
    ``error``, never an empty answer: the caller refuses on it.
    """
    try:
        records = read_spawn_records(root_dir)
    except (OSError, ValueError) as exc:
        return WorktreeSweep(error=f"an agent spawn record could not be read: {exc}")
    survivors: list[Survivor] = []
    for path, record in records:
        if pid_is_alive(record.owner_pid):
            continue
        members, error = _recorded_members(record)
        if error:
            return WorktreeSweep(error=f"the group in {path} could not be read: {error}")
        if not members:
            forget_spawn_record(path)
        survivors += [_survivor(pid, record) for pid in members]
    return WorktreeSweep(tuple(survivors))


def leftover_lines(sweep: WorktreeSweep) -> list[str]:
    """What an operator reads about :func:`leftover_agents`'s answer."""
    if sweep.error:
        return [f"agent processes could not be checked: {sweep.error}"]
    lines = [_describe(s) for s in sweep.survivors]
    groups = sorted({s.pgid for s in sweep.survivors})
    return lines + [f"stop group {pgid} with: kill -KILL -{pgid}" for pgid in groups]


def _recorded_members(record: SpawnRecord) -> tuple[tuple[int, ...], str]:
    """The running members of the record's group, or why they are unknown.

    Empty when the group is gone, and when its id leads a process whose
    command line lacks the nonce: the leash never changes its command, so
    that leader is another process that was given the id once the
    recorded group had ended.
    """
    members = read_group_members(record.pgid)
    if members.pids is None or len(members.commands) != len(members.pids):
        return (), members.reason or "the listing carried no command for each member"
    leader = dict(zip(members.pids, members.commands, strict=True)).get(record.pgid)
    if leader is not None and record.nonce not in leader:
        return (), ""
    return members.pids, ""


def _survivor(pid: int, record: SpawnRecord) -> Survivor:
    return Survivor(
        pid,
        record.pgid,
        shlex.join(record.command),
        record.cwd,
        f"the kstrl process that started it (pid {record.owner_pid}) is not running, "
        "and kstrl reports such a group without signalling it",
    )


def _describe(s: Survivor) -> str:
    done = f"not killed: {s.not_killed}" if s.not_killed else "killed its process group"
    return f"pid {s.pid} ({s.command}, group {s.pgid}) was still running in {s.cwd}; {done}"


def _read_cwds(cwd: Path) -> tuple[list[_Row], str]:
    try:
        out = run_scrubbed(LSOF_ARGV, cwd=cwd, timeout=LSOF_TIMEOUT_SECONDS)
    except (OSError, ValueError, subprocess.TimeoutExpired, ChildOutputDecodeError) as exc:
        return [], f"lsof failed to run ({exc!r})"
    rows = _parse(out.stdout)
    if not any(row.pid == os.getpid() for row in rows):
        return [], (
            f"lsof (rc={out.returncode}) did not list this process's own working "
            f"directory, so its listing cannot be trusted to list the worktree's: "
            f"{out.stderr.strip()[:200]!r}"
        )
    return rows, ""


def _parse(stdout: str) -> list[_Row]:
    rows: list[_Row] = []
    fields: dict[str, str] = {}
    for line in [*stdout.splitlines(), "p"]:
        key, value = line[:1], line[1:]
        if key == "p":
            if "p" in fields and "g" in fields and "n" in fields:
                try:
                    rows.append(
                        _Row(int(fields["p"]), int(fields["g"]), fields.get("c", ""), fields["n"])
                    )
                except ValueError:
                    pass
            fields = {}
        if key in ("p", "g", "c", "n"):
            fields[key] = value
    return rows
