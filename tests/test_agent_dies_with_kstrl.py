"""An agent does not outlive the kstrl process that started it (#642).

The defect: every agent runs in a session of its own, and every cleanup
kstrl has runs inside the kstrl process that owns it. A SIGKILL, an OOM
kill or a closed terminal runs none of them, so a silent agent and the
processes it started kept running (measured: alive 15 s after a SIGKILL
of ``ks factory``, 8 s after a SIGHUP). A pool worker whose parent alone
was killed kept running too, and kept starting agents.

The fix: each agent runs under ``kstrl/agents/leash.py``, which ends the
agent's process group when kstrl's end of a pipe closes, and a pool
worker ends when its parent does.

Every process these tests signal is the ``ks`` process group they
started or a pid the stand-in agent wrote to a file (#292).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kstrl.agents.leash import NOT_A_GROUP_LEADER
from kstrl.agents.proc import DEFAULT_TERM_GRACE_SECONDS, LEASH_PATH
from kstrl.factory import run_factory
from kstrl.procgroup import pid_is_alive
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from tests import spine_utils
from tests.helpers import gitrepo, procs
from tests.test_agent_processes_outlive_run import COMPLETE, FLAGS, _detach, _env, _kill_pid, _repo
from tests.test_worktree_removal_sweeps import _names, _run_warnings

#: Seconds from the kill to the agent's death. Measured on macOS, n=20 at
#: load average 10.9 to 26.0 on 10 cores: at most 0.033 s. Re-measured for
#: the four kill cases and the pool, n=20 each at load 20.5 to 46.8: at most
#: 0.130 s. The margin is for CI runners, which were not measured. It must
#: stay below the 5 s grace, or an agent that ignores SIGTERM would pass by
#: dying of the SIGKILL.
AGENT_BOUND_SECONDS = 2.0

#: Seconds from the kill to the death of a grandchild that ignores SIGTERM:
#: the leash's grace, then its SIGKILL. Measured in the same runs: at most
#: 5.038 s. The pool test uses it for every process: at most 0.019 s there,
#: n=20 at load 26.0 to 30.0, because the worker's own kill ends the group
#: as soon as the agent has gone.
GRANDCHILD_BOUND_SECONDS = DEFAULT_TERM_GRACE_SECONDS + 2.0

#: How long the stand-in agents may take to start. A fuse, not a measurement.
START_FUSE_SECONDS = 120.0


def _silent_agent(pairs: Path) -> str:
    """Starts a grandchild that ignores SIGTERM in the agent's own group,
    then waits without writing a byte. The grandchild appends
    ``<agent pid> <grandchild pid>`` to ``pairs`` once its SIGTERM is
    ignored, because the test kills kstrl the moment the line appears: a
    line the agent wrote straight after the fork could land before the
    subshell ran its ``trap``, and the leash's SIGTERM killed it before
    the grace (measured: 2 of 10 runs of this file at load 53 to 62)."""
    return (
        "(trap '' TERM; exec sh -c 'echo \"$1 $$\" >> \"$2\"; exec sleep 600' "
        f"sh $$ '{pairs}') & exec sleep 600"
    )


def _pairs(path: Path) -> list[tuple[int, int]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [(int(a), int(g)) for a, g in (line.split() for line in text.splitlines()) if g]


#: Starts the CLI with SIGHUP at its default disposition, by ``exec``, so
#: the pid is the CLI's. An ignored disposition survives ``exec``, and a
#: suite started under ``nohup`` hands SIG_IGN to everything it spawns: the
#: merge gate is started that way, ``ks`` outlived the SIGHUP, and the
#: sighup case failed there 2 of 2. Measured: 3 of 3 failed under
#: ``nohup`` and 20 of 20 passed without it, both at load 38 to 45. A
#: hangup is a stop since #642 slice 4, so that case now lives in
#: ``tests/test_stop_kills_whoever_asks.py``.
_DEFAULT_HUP_EXEC = (
    "import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_DFL); "
    "os.execv(sys.executable, [sys.executable, '-m', 'kstrl', *sys.argv[1:]])"
)


def _ks(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """The real CLI in its own session, so its pgid is its pid."""
    return subprocess.Popen(
        [sys.executable, "-c", _DEFAULT_HUP_EXEC, *args],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def _factory_args(root: Path, max_parallel: str, *extra: str) -> list[str]:
    return [
        "factory",
        "--manifest",
        str(root / "scripts" / "kstrl" / "manifest.json"),
        "--root",
        str(root),
        "--max-parallel",
        max_parallel,
        *FLAGS,
        *extra,
    ]


def _loop_files(root: Path) -> None:
    """The prompt and PRD ``ks run`` reads."""
    kdir = root / "scripts" / "kstrl"
    (kdir / "prompt.md").write_text("do the thing\n", encoding="utf-8")
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["a"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    (kdir / "prd.json").write_text(
        json.dumps({"branchName": "kstrl/test", "userStories": [story]}), encoding="utf-8"
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "loop files")


def _second_component(root: Path) -> None:
    """``_repo`` has one component; the pool test needs two running at once."""
    manifest_path = root / "scripts" / "kstrl" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first = manifest["components"][0]
    prd = json.loads((root / first["prdPath"]).read_text(encoding="utf-8"))
    prd["branchName"] = "kstrl/factory/comp-b"
    prd_path = root / "scripts" / "kstrl" / "feature" / "comp-b" / "prd.json"
    prd_path.parent.mkdir(parents=True)
    prd_path.write_text(json.dumps(prd), encoding="utf-8")
    manifest["components"].append(
        {
            **first,
            "id": "comp-b",
            "title": "comp-b",
            "prdPath": "scripts/kstrl/feature/comp-b/prd.json",
            "branchName": "kstrl/factory/comp-b",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "second component")


def _wait_for_agents(proc: subprocess.Popen[str], pairs: Path, count: int) -> None:
    deadline = time.monotonic() + START_FUSE_SECONDS
    while len(_pairs(pairs)) < count:
        assert time.monotonic() < deadline, f"{count} agent(s) never started"
        assert proc.poll() is None, proc.communicate()[0]
        time.sleep(0.05)


def _dispose(proc: subprocess.Popen[str], pairs: Path) -> None:
    """SIGKILL every pid the stand-ins wrote, then the ks group."""
    for pair in _pairs(pairs):
        for pid in pair:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    procs.kill_group(proc.pid)
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def _assert_alive_through_the_grace(pid: int, killed: float, what: str) -> None:
    """Every look at ``pid`` taken before the grace has run out from the
    kill finds it alive. The time is read after the look, so a look that
    finds ``pid`` dead while that time is still inside the grace proves it
    died early. A look that load delays past the grace proves nothing and
    ends the loop, so load can make this weaker but never red."""
    while True:
        alive = pid_is_alive(pid)
        since = time.monotonic() - killed
        if since >= DEFAULT_TERM_GRACE_SECONDS:
            return
        assert alive, (
            f"{what} pid {pid} died {since:.2f}s after the kill, inside the "
            f"{DEFAULT_TERM_GRACE_SECONDS}s grace"
        )
        time.sleep(0.05)


_KILLS = {
    "factory-sigkill-group": ("factory", lambda pid: os.killpg(pid, signal.SIGKILL)),
    "factory-sigkill-parent": ("factory", lambda pid: os.kill(pid, signal.SIGKILL)),
    "run-sigkill-group": ("run", lambda pid: os.killpg(pid, signal.SIGKILL)),
}


@pytest.mark.parametrize("case", sorted(_KILLS))
def test_a_silent_agent_and_its_grandchild_die_with_kstrl(tmp_path: Path, case: str) -> None:
    """The issue's acceptance, without worktrees: the owning kstrl process
    dies while its agent is silent. The agent is gone within
    ``AGENT_BOUND_SECONDS``. Its grandchild, which ignores SIGTERM, is
    alive at every look taken inside the grace, so the leash sent SIGTERM
    first and did not cut the grace short while a member lived (#708), and
    gone within ``GRANDCHILD_BOUND_SECONDS``, so the SIGKILL after the
    grace came."""
    command, kill = _KILLS[case]
    root = _repo(tmp_path)
    pairs = tmp_path / "pairs"
    env = _env(_silent_agent(pairs))
    if command == "run":
        _loop_files(root)
        args = ["run", "1", "--root", str(root), "--ui", "plain", "--no-verify", "--branch", ""]
    else:
        args = _factory_args(root, "1", "--no-worktrees")
    proc = _ks(root, env, *args)
    try:
        _wait_for_agents(proc, pairs, 1)
        [(agent, grandchild)] = _pairs(pairs)
        killed = time.monotonic()
        kill(proc.pid)
        assert procs.wait_for_pid_to_die(agent, timeout=AGENT_BOUND_SECONDS), (
            f"agent pid {agent} alive {AGENT_BOUND_SECONDS}s after the kill ({case}); "
            f"ks returncode {proc.poll()}"
        )
        _assert_alive_through_the_grace(grandchild, killed, f"{case}: the grandchild")
        remaining = killed + GRANDCHILD_BOUND_SECONDS - time.monotonic()
        assert procs.wait_for_pid_to_die(grandchild, timeout=remaining), (
            f"grandchild pid {grandchild}, which ignores SIGTERM, alive "
            f"{GRANDCHILD_BOUND_SECONDS}s after kstrl died ({case})"
        )
    finally:
        _dispose(proc, pairs)


def test_a_pool_worker_ends_with_its_parent_and_takes_its_agents(tmp_path: Path) -> None:
    """Worktrees on, two workers, and only the parent pid is SIGKILLed.
    Before the fix the workers kept running, kept starting agents and held
    the run's stdout. Both agents and both grandchildren are gone within
    ``GRANDCHILD_BOUND_SECONDS``, and the run's stdout reaches EOF."""
    root = _repo(tmp_path)
    _second_component(root)
    pairs = tmp_path / "pairs"
    proc = _ks(root, _env(_silent_agent(pairs)), *_factory_args(root, "2"))
    try:
        _wait_for_agents(proc, pairs, 2)
        killed = time.monotonic()
        os.kill(proc.pid, signal.SIGKILL)
        for pid in [pid for pair in _pairs(pairs) for pid in pair]:
            remaining = killed + GRANDCHILD_BOUND_SECONDS - time.monotonic()
            assert procs.wait_for_pid_to_die(pid, timeout=remaining), (
                f"pid {pid} alive {GRANDCHILD_BOUND_SECONDS}s after the parent was SIGKILLed"
            )
        remaining = max(0.1, killed + GRANDCHILD_BOUND_SECONDS - time.monotonic())
        try:
            proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            pytest.fail("a pool worker still holds the run's stdout after its parent died")
    finally:
        _dispose(proc, pairs)


@pytest.mark.parametrize("left", [True, False], ids=["tool-process-left", "nothing-left"])
def test_the_next_run_names_what_it_kills_in_a_killed_runs_worktree(
    tmp_path: Path, left: bool
) -> None:
    """A pool run is SIGKILLed while its agent is alive in its worktree. The
    leash ends the agent's group, but a process the agent's tool started in
    a session of its own is outside that group and keeps running in the
    worktree. The next run's prune kills it, and a warning in that run's
    events.jsonl names its pid. The control leaves nothing running: the
    next run prunes the worktree and names no process."""
    root = _repo(tmp_path)
    agent_pidfile = tmp_path / "agent.pid"
    left_pidfile = tmp_path / "left.pid"
    detach = f"{_detach(left_pidfile)} && " if left else ""
    agent = f"{detach}echo $$ > '{agent_pidfile}' && exec sleep 600"
    first = _ks(root, _env(agent), *_factory_args(root, "2"))
    second: subprocess.Popen[str] | None = None
    try:
        agent_group = os.getpgid(procs.read_pid(agent_pidfile, timeout=START_FUSE_SECONDS))
        os.killpg(first.pid, signal.SIGKILL)
        first.communicate(timeout=30)
        # The leash leads the agent's group, runs in the worktree and waits
        # out its grace after the kill, so a run started sooner kills it too.
        assert procs.wait_for_group_to_die(agent_group, timeout=GRANDCHILD_BOUND_SECONDS), (
            f"the agent's group {agent_group} outlived the killed run"
        )
        second = _ks(root, _env(COMPLETE), *_factory_args(root, "2"))
        out, _ = second.communicate(timeout=240)
        assert second.returncode == 0, out
        assert "Pruned 1 stale worktree(s) from previous runs" in out, out
        warnings = _run_warnings(root)
        if not left:
            assert "orphan_process (stale worktree)" not in warnings, warnings
            return
        pid = procs.read_pid(left_pidfile)
        assert procs.wait_for_pid_to_die(pid, timeout=10), f"pid {pid} outlived the next run"
        assert _names(warnings, pid, "stale worktree"), (
            f"no stale worktree warning in events.jsonl names pid {pid}:\n{warnings}"
        )
    finally:
        for pidfile in (agent_pidfile, left_pidfile):
            text = pidfile.read_text(encoding="utf-8").strip() if pidfile.exists() else ""
            if text:
                _kill_pid(int(text))
        procs.kill_group(first.pid)
        if second is not None:
            procs.kill_group(second.pid)


def test_the_leash_passes_the_agent_its_stdin_and_stdout(tmp_path: Path) -> None:
    """An agent that echoes its prompt: the run completes and the
    component transcript carries the prompt, so the leash in between
    hands the agent kstrl's pipes."""
    root = _repo(tmp_path)
    proc = _ks(root, _env(f"cat && {COMPLETE}"), *_factory_args(root, "1", "--no-worktrees"))
    try:
        out, _ = proc.communicate(timeout=240)
    finally:
        procs.kill_group(proc.pid)
    assert proc.returncode == 0, out
    [log] = root.glob(".kstrl/runs/*/components/comp-a/engineer.log")
    assert "scripts/kstrl/feature/comp-a/prd.json" in log.read_text(encoding="utf-8")


def _run_leash(tmp_path: Path, marker: Path, *, leader: bool) -> tuple[int, bytes]:
    """The leash, started the way ``DeadlineStreamer`` starts it, with an
    agent that creates ``marker``. ``leader=False`` starts it as the child
    of a shell that leads a new group, so the leash is in a group it does
    not lead and that is not this test's. Returns its exit status and what
    it wrote on the status pipe."""
    lifeline_read, lifeline_write = os.pipe()
    status_read, status_write = os.pipe()
    leash = [
        sys.executable,
        "-I",
        "-S",
        LEASH_PATH,
        str(lifeline_read),
        str(status_write),
        "0",
        "--",
        "/bin/sh",
        "-c",
        f"touch '{marker}'",
    ]
    argv = leash if leader else ["/bin/sh", "-c", '"$@" & wait $!', "sh", *leash]
    child = subprocess.Popen(
        argv, pass_fds=(lifeline_read, status_write), cwd=tmp_path, start_new_session=True
    )
    os.close(lifeline_read)
    os.close(status_write)
    try:
        rc = child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        procs.kill_group(child.pid)
        child.wait(timeout=10)
        pytest.fail("the leash neither refused nor let its agent finish within 30s")
    finally:
        os.close(lifeline_write)
    with os.fdopen(status_read, "rb") as status:
        return rc, status.read()


def test_a_leash_that_does_not_lead_its_group_refuses_to_start(tmp_path: Path) -> None:
    """``killpg(0, ...)`` reaches the caller's own group, so a leash that
    is not its group's leader would signal a group that is not the
    agent's. It must exit 70 without starting the agent. Invoked by path
    because no kstrl command can start it that way: ``DeadlineStreamer``
    always gives it a session of its own. The leader case is the control
    that the invocation itself works."""
    marker = tmp_path / "started"
    rc, status = _run_leash(tmp_path, marker, leader=True)
    assert (rc, status, marker.exists()) == (0, b"", True)
    marker.unlink()
    rc, status = _run_leash(tmp_path, marker, leader=False)
    assert (rc, status, marker.exists()) == (NOT_A_GROUP_LEADER, b"refused", False)


def test_an_agent_cli_that_cannot_start_is_reported_as_missing(tmp_path: Path) -> None:
    """``claude_code`` reports a ``FileNotFoundError`` from the spawn as a
    missing CLI. Under the leash the spawn happens in another process, so
    the leash reports it back and ``DeadlineStreamer`` raises it here.

    PATH is sealed to a directory with a ``claude`` whose interpreter does
    not exist and one holding the tools the run needs, so the exec fails
    with ENOENT and no real ``claude`` can be reached."""
    root = _repo(tmp_path)
    _loop_files(root)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "claude").write_text("#!/nonexistent/kstrl-642-interpreter\n", encoding="utf-8")
    (shim / "claude").chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("git", "sh", "bash"):
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (tools / name).symlink_to(found)
    sealed = f"{shim}{os.pathsep}{tools}"
    assert shutil.which("claude", path=sealed) == str(shim / "claude")
    env = {k: v for k, v in _env("").items() if k != "AGENT_CMD"}
    env.update(PATH=sealed, KSTRL_AGENT_TYPE="claude", KSTRL_AGENT_PROBE="0")
    args = ["run", "1", "--root", str(root), "--ui", "plain", "--no-verify", "--branch", ""]
    proc = _ks(root, env, *args, "--sleep", "0")
    try:
        out, _ = proc.communicate(timeout=240)
    finally:
        procs.kill_group(proc.pid)
    logs = list(root.glob(".kstrl/runs/*/components/*/engineer.log"))
    assert logs, out
    text = "".join(log.read_text(encoding="utf-8") for log in logs)
    assert "ERROR: claude CLI not found in PATH" in text, text


def _open_pipes() -> set[int]:
    """The descriptors of this process that are pipes. A set rather than a
    count, so a descriptor another test's leftover thread closes during
    the run cannot hide one this run leaked."""
    pipes: set[int] = set()
    for name in os.listdir("/dev/fd"):
        try:
            if stat.S_ISFIFO(os.fstat(int(name)).st_mode):
                pipes.add(int(name))
        except OSError:
            continue
    return pipes


def test_an_orderly_run_leaves_no_lifeline_open(tmp_path: Path) -> None:
    """Every agent call holds the write end of its leash's lifeline, and
    every disposal must close it. A run in this process with two
    components whose agents finish: the process has no pipe open afterwards
    that it did not have before. A disposal that kept the write end leaked
    one per call (measured: 4 more after this run)."""
    root = tmp_path / "repo"
    spine_utils.init_kstrl_repo(root, ("comp-a", "comp-b"))
    manifest = spine_utils.make_manifest(
        [spine_utils.component("comp-a"), spine_utils.component("comp-b")]
    )
    config = spine_utils.factory_config(use_worktrees=False)
    before = _open_pipes()
    result = run_factory(
        manifest,
        config,
        spine_utils.base_config(root),
        PlainUI(no_color=True, file=io.StringIO()),
        root,
    )
    assert result.exit_code == 0
    assert _open_pipes() - before == set()


def test_a_timed_out_run_leaves_no_lifeline_open(tmp_path: Path) -> None:
    """The deadline path is a disposal too: an agent that outlives its
    1 s iteration limit is killed by ``_breach``, which must close the
    lifeline's write end like ``finish`` does. A breach that kept it leaked
    one pipe per timed-out call (measured: 3 more after this run)."""
    root = tmp_path / "repo"
    spine_utils.init_kstrl_repo(root, ("comp-a",))
    manifest = spine_utils.make_manifest([spine_utils.component("comp-a")])
    config = spine_utils.factory_config(
        use_worktrees=False, timeout_config=TimeoutConfig(agent_iteration=1.0)
    )
    before = _open_pipes()
    result = run_factory(
        manifest,
        config,
        spine_utils.base_config(root, agent_cmd="exec sleep 30"),
        PlainUI(no_color=True, file=io.StringIO()),
        root,
    )
    assert result.exit_code != 0
    assert _open_pipes() - before == set()


def _quiet_agent(pids: Path) -> str:
    """Writes its pid to ``pids`` and waits without a byte. It dies of
    the SIGTERM, and it starts nothing, so once it is gone the leash is
    the only process left in the group."""
    return f"echo $$ > '{pids}' && exec sleep 600"


def test_the_leash_ends_as_soon_as_its_group_is_empty(tmp_path: Path) -> None:
    """#708: the owning ``ks`` dies while its agent is silent, and the
    agent dies of the leash's SIGTERM. Nothing is left in the group but
    the leash, so the group must be empty before the grace has run out
    from the kill. Before the fix the leash slept the whole grace first.

    A leash that sleeps the grace cannot pass at any load: it starts that
    sleep after ``killed``, and ``seen`` is read after the reading that
    found the group empty, so ``seen`` is never earlier than the group's
    end. The fixed leash fails this only if load makes it spend the whole
    grace before one reading of its group shows it empty (one reading
    measured at most 0.125 s at load 25)."""
    root = _repo(tmp_path)
    pids = tmp_path / "pids"
    proc = _ks(root, _env(_quiet_agent(pids)), *_factory_args(root, "1", "--no-worktrees"))
    try:
        agent = procs.read_pid(pids, timeout=START_FUSE_SECONDS)
        leash = os.getpgid(agent)
        assert leash != agent, f"agent pid {agent} leads its own group: no leash above it"
        killed = time.monotonic()
        os.killpg(proc.pid, signal.SIGKILL)
        assert procs.wait_for_pid_to_die(agent, timeout=AGENT_BOUND_SECONDS), (
            f"agent pid {agent} alive {AGENT_BOUND_SECONDS}s after the kill"
        )
        empty = procs.wait_for_group_to_die(
            leash, timeout=max(0.0, killed + DEFAULT_TERM_GRACE_SECONDS - time.monotonic())
        )
        seen = time.monotonic() - killed
        assert empty and seen < DEFAULT_TERM_GRACE_SECONDS, (
            f"the leash's group {leash} held only the leash after its agent died and "
            f"still lived {seen:.2f}s after the kill: it waited out its "
            f"{DEFAULT_TERM_GRACE_SECONDS}s grace with nothing left to signal"
        )
    finally:
        text = pids.read_text(encoding="utf-8").strip() if pids.exists() else ""
        if text:
            _kill_pid(int(text))
        procs.kill_group(proc.pid)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def test_a_leash_that_cannot_read_its_group_waits_out_the_grace(tmp_path: Path) -> None:
    """The fail-closed half of #708. PATH holds no ``ps``, so the leash
    cannot read who is left in its group. It must not end early on a
    reading it could not make: it is alive at every look inside the grace,
    though its agent died of the SIGTERM, and gone within
    ``GRANDCHILD_BOUND_SECONDS``, so the SIGKILL after the grace came."""
    root = _repo(tmp_path)
    pids = tmp_path / "pids"
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("git", "sh", "bash", "sleep"):
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (tools / name).symlink_to(found)
    env = _env(_quiet_agent(pids))
    env["PATH"] = str(tools)
    proc = _ks(root, env, *_factory_args(root, "1", "--no-worktrees"))
    try:
        agent = procs.read_pid(pids, timeout=START_FUSE_SECONDS)
        leash = os.getpgid(agent)
        assert leash != agent, f"agent pid {agent} leads its own group: no leash above it"
        killed = time.monotonic()
        os.killpg(proc.pid, signal.SIGKILL)
        _assert_alive_through_the_grace(leash, killed, "the leash (no ps on PATH)")
        remaining = killed + GRANDCHILD_BOUND_SECONDS - time.monotonic()
        assert procs.wait_for_pid_to_die(leash, timeout=remaining), (
            f"leash pid {leash} alive {GRANDCHILD_BOUND_SECONDS}s after kstrl died"
        )
    finally:
        text = pids.read_text(encoding="utf-8").strip() if pids.exists() else ""
        if text:
            _kill_pid(int(text))
        procs.kill_group(proc.pid)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def test_a_leash_that_cannot_import_its_reading_still_waits_and_kills(tmp_path: Path) -> None:
    """The leash run by path, as ``DeadlineStreamer`` runs it, but from a
    copy with no ``kstrl`` two directories above it, so its one kstrl
    import fails. A reading it cannot make must keep it waiting the whole
    grace, and the failure must not end it before the SIGKILL: the
    grandchild, which ignores SIGTERM, is alive at every look inside the
    grace and gone within ``GRANDCHILD_BOUND_SECONDS`` (#708)."""
    copy = tmp_path / "elsewhere" / "agents" / "leash.py"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(LEASH_PATH, copy)
    pairs = tmp_path / "pairs"
    lifeline_read, lifeline_write = os.pipe()
    status_read, status_write = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            str(copy),
            str(lifeline_read),
            str(status_write),
            str(DEFAULT_TERM_GRACE_SECONDS),
            "--",
            "/bin/sh",
            "-c",
            _silent_agent(pairs),
        ],
        pass_fds=(lifeline_read, status_write),
        cwd=tmp_path,
        start_new_session=True,
    )
    os.close(lifeline_read)
    os.close(status_write)
    try:
        deadline = time.monotonic() + START_FUSE_SECONDS
        while not _pairs(pairs):
            assert time.monotonic() < deadline, "the agent never started"
            assert child.poll() is None, f"the leash exited {child.returncode} before its owner"
            time.sleep(0.05)
        [(agent, grandchild)] = _pairs(pairs)
        killed = time.monotonic()
        os.close(lifeline_write)
        lifeline_write = -1
        assert procs.wait_for_pid_to_die(agent, timeout=AGENT_BOUND_SECONDS), (
            f"agent pid {agent} alive {AGENT_BOUND_SECONDS}s after its owner went"
        )
        _assert_alive_through_the_grace(grandchild, killed, "the grandchild (no kstrl to import)")
        remaining = killed + GRANDCHILD_BOUND_SECONDS - time.monotonic()
        assert procs.wait_for_pid_to_die(grandchild, timeout=remaining), (
            f"grandchild pid {grandchild}, which ignores SIGTERM, alive "
            f"{GRANDCHILD_BOUND_SECONDS}s after its owner went: the leash sent no SIGKILL"
        )
    finally:
        if lifeline_write != -1:
            os.close(lifeline_write)
        for pair in _pairs(pairs):
            for pid in pair:
                _kill_pid(pid)
        procs.kill_group(child.pid)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        os.close(status_read)
