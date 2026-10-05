"""The next command names an agent a killed kstrl left running (#642).

The leash ends an agent's process group when the kstrl process that owns
it dies. If the leash dies in the same moment (the double fault), nothing
ends the group. Every spawn is recorded under the project's control
directory, with a nonce the leash carries in its argv, until it is
disposed of. ``ks status`` names what a dead kstrl left running, and the
lock-taking commands refuse with exit 2 and the command that stops each
group. Nothing is signalled (owner decision 1 (b)).

Every process these tests signal is one they started or a pid the
stand-in agent wrote to a file (#292).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.agents.spawn_record import (
    SPAWN_RECORD_DIRNAME,
    SpawnRecord,
    new_nonce,
    spawn_record_path,
    write_spawn_record,
)
from kstrl.procgroup import pid_is_alive
from kstrl.statedir import control_dir
from tests.helpers import procs
from tests.test_agent_dies_with_kstrl import START_FUSE_SECONDS, _factory_args, _ks
from tests.test_agent_processes_outlive_run import COMPLETE, _env, _repo

#: How long one ``ks`` command may take. A fuse, not a measurement.
COMMAND_FUSE_SECONDS = 240.0


def _records(root: Path) -> list[Path]:
    return sorted((control_dir(root) / SPAWN_RECORD_DIRNAME).glob("*.json"))


def _status(root: Path) -> subprocess.CompletedProcess[str]:
    """``ks status``, its report and its errors in ``stdout`` (the plain UI
    writes the report to stderr)."""
    return subprocess.run(
        [sys.executable, "-m", "kstrl", "status", "--root", str(root), "--ui", "plain"],
        cwd=root,
        env=_env(COMPLETE),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=COMMAND_FUSE_SECONDS,
    )


def _next_run(
    root: Path, marker: Path, env: dict[str, str] | None = None, *extra: str
) -> tuple[int, str]:
    """``ks factory`` with an agent that creates ``marker`` and completes."""
    run_env = env if env is not None else _env(f"touch '{marker}' && {COMPLETE}")
    proc = _ks(root, run_env, *_factory_args(root, "1", "--no-worktrees", *extra))
    try:
        out, _ = proc.communicate(timeout=COMMAND_FUSE_SECONDS)
    finally:
        procs.kill_group(proc.pid)
    return proc.returncode, out


def _dead_pid() -> int:
    """A pid that is not running: a child this test started and reaped."""
    child = subprocess.Popen(["true"])
    child.wait(timeout=30)
    return child.pid


def _plant(root: Path, *, pgid: int, nonce: str, owner_pid: int) -> Path:
    path = spawn_record_path(root, nonce)
    assert path is not None
    write_spawn_record(
        path,
        SpawnRecord(pgid=pgid, nonce=nonce, owner_pid=owner_pid, cwd=str(root), command=("x",)),
    )
    return path


def _start_agent_and_kill_kstrl(
    root: Path, pidfile: Path, *, leash_sig: signal.Signals
) -> tuple[subprocess.Popen[str], int, int]:
    """A silent agent under ``ks factory``; ``leash_sig`` to its leash, then
    SIGKILL to the ``ks`` group, which is then reaped. Returns the ``ks``
    process, the agent pid and the agent's group, which the leash leads.

    The control comes first: while ``ks`` is alive, ``ks status`` calls the
    run in flight and names no leftover agent, because the record's owner
    is running."""
    first = _ks(
        root,
        _env(f"echo $$ > '{pidfile}' && exec sleep 600"),
        *_factory_args(root, "1", "--no-worktrees"),
    )
    agent = procs.read_pid(pidfile, timeout=START_FUSE_SECONDS)
    group = os.getpgid(agent)
    assert group != agent, f"agent pid {agent} leads its own group: no leash above it"
    live = _status(root)
    assert "Run state:    in flight" in live.stdout, live.stdout
    assert "leftover agents" not in live.stdout, live.stdout
    os.kill(group, leash_sig)
    os.killpg(first.pid, signal.SIGKILL)
    first.communicate(timeout=30)
    return first, agent, group


def test_a_double_fault_is_named_by_status_and_refused_by_the_next_run(tmp_path: Path) -> None:
    """The leash and then ``ks`` are SIGKILLed while the agent is silent.
    The agent keeps running, with no leader in its group. ``ks status``
    names it, the group's kill command and the run as stopped; the next
    ``ks factory`` exits 2 naming the same, starts no agent, and signals
    nothing: the agent is alive after both commands."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "agent.pid"
    first: subprocess.Popen[str] | None = None
    group = 0
    try:
        first, agent, group = _start_agent_and_kill_kstrl(root, pidfile, leash_sig=signal.SIGKILL)
        status = _status(root)
        assert status.returncode == 0, status.stdout
        assert f"pid {agent} " in status.stdout, status.stdout
        assert f"kill -KILL -{group}" in status.stdout, status.stdout
        assert (
            f"stopped without a finish record (kstrl process {first.pid} is not running)"
            in status.stdout
        ), status.stdout
        marker = tmp_path / "second-started"
        code, out = _next_run(root, marker)
        assert code == 2, out
        assert f"pid {agent} " in out and f"kill -KILL -{group}" in out, out
        assert not marker.exists(), "the refused run started an agent"
        assert pid_is_alive(agent), f"agent pid {agent} was signalled: report only"
    finally:
        if group > 1:
            procs.kill_group(group)
        if first is not None:
            procs.kill_group(first.pid)


def test_a_leash_that_cannot_act_is_named_with_its_group(tmp_path: Path) -> None:
    """The leash is stopped (SIGSTOP) and ``ks`` is SIGKILLed, so the group
    keeps its leader and that leader carries the nonce. ``ks status`` names
    both the leash and the agent: a live leader that shows the recorded
    nonce is kstrl's."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "agent.pid"
    first: subprocess.Popen[str] | None = None
    group = 0
    try:
        first, agent, group = _start_agent_and_kill_kstrl(root, pidfile, leash_sig=signal.SIGSTOP)
        status = _status(root)
        assert status.returncode == 0, status.stdout
        for pid in (group, agent):
            assert f"pid {pid} " in status.stdout, status.stdout
        assert f"kill -KILL -{group}" in status.stdout, status.stdout
    finally:
        if group > 1:
            procs.kill_group(group)
        if first is not None:
            procs.kill_group(first.pid)


def test_a_record_names_only_a_group_kstrl_can_still_prove_is_its_own(tmp_path: Path) -> None:
    """Two planted records and one run. The first names a test-owned
    ``sleep`` that leads its own group, with a nonce its command does not
    carry and an owner that is gone: the id now leads another process, so
    nothing is named, the sleep is left alive and the record is removed.
    The second names a live owner, this test, so its group belongs to a run
    in progress: it is skipped and kept. The run itself completes and
    removes the records of its own spawns."""
    root = _repo(tmp_path)
    nonce = new_nonce()
    sleeper = subprocess.Popen(["sleep", "600"], start_new_session=True)
    owned = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)", nonce], start_new_session=True
    )
    try:
        reused = _plant(root, pgid=sleeper.pid, nonce=new_nonce(), owner_pid=_dead_pid())
        live = _plant(root, pgid=owned.pid, nonce=nonce, owner_pid=os.getpid())
        code, out = _next_run(root, tmp_path / "started")
        assert code == 0, out
        assert pid_is_alive(sleeper.pid), "the sleep was signalled: report only"
        assert not reused.exists(), "a record whose group is gone was kept"
        assert _records(root) == [live], "the run left records, or removed a live one"
    finally:
        for child in (sleeper, owned):
            procs.kill_group(child.pid)
            child.wait(timeout=30)


@pytest.mark.parametrize(
    ("kind", "extra"),
    [("text", ()), ("directory", ("--force-lock",))],
    ids=["not-json", "not-readable-under-force-lock"],
)
def test_a_record_that_does_not_parse_refuses_the_next_run(
    tmp_path: Path, kind: str, extra: tuple[str, ...]
) -> None:
    """A record that is not JSON, or a record name that cannot be read as a
    file (a directory: an ``OSError`` rather than a ``ValueError``): the next
    run exits 2 naming it and starts no agent, ``--force-lock`` included,
    ``ks status`` reports the agents as unknown, and the entry is left for
    the operator."""
    root = _repo(tmp_path)
    garbage = (control_dir(root) / SPAWN_RECORD_DIRNAME) / f"{new_nonce()}.json"
    garbage.parent.mkdir(parents=True)
    if kind == "directory":
        garbage.mkdir()
    else:
        garbage.write_text("not a record\n", encoding="utf-8")
    marker = tmp_path / "started"
    code, out = _next_run(root, marker, None, *extra)
    assert code == 2, out
    assert str(garbage) in out, out
    assert not marker.exists(), "the refused run started an agent"
    status = _status(root)
    assert "leftover agents: unknown" in status.stdout, status.stdout
    assert garbage.exists()


def test_a_group_that_cannot_be_listed_refuses_the_next_run(tmp_path: Path) -> None:
    """A record whose owner is gone, and a PATH with no ``ps``, so its group
    cannot be listed. That is a refusal, never an empty group: exit 2, no
    agent started, the record kept."""
    root = _repo(tmp_path)
    sleeper = subprocess.Popen(["sleep", "600"], start_new_session=True)
    try:
        record = _plant(root, pgid=sleeper.pid, nonce=new_nonce(), owner_pid=_dead_pid())
        tools = tmp_path / "tools"
        tools.mkdir()
        for name in ("git", "sh", "bash", "touch"):
            found = shutil.which(name)
            assert found is not None, f"{name} is not on PATH"
            (tools / name).symlink_to(found)
        marker = tmp_path / "started"
        env = {**_env(f"touch '{marker}' && {COMPLETE}"), "PATH": str(tools)}
        code, out = _next_run(root, marker, env)
        assert code == 2, out
        assert str(record) in out, out
        assert not marker.exists(), "the refused run started an agent"
        assert record.exists()
    finally:
        procs.kill_group(sleeper.pid)
        sleeper.wait(timeout=30)
