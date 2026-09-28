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
from kstrl.ui.plain import PlainUI
from tests import spine_utils
from tests.helpers import gitrepo, procs
from tests.test_agent_processes_outlive_run import COMPLETE, FLAGS, _env, _repo

#: Seconds from the kill to the agent's death. Measured on macOS, n=20 at
#: load average 10.9 to 26.0 on 10 cores: at most 0.033 s. The margin is for
#: CI runners, which were not measured. It must stay below the 5 s grace, or
#: an agent that ignores SIGTERM would pass by dying of the SIGKILL.
AGENT_BOUND_SECONDS = 2.0

#: Seconds from the kill to the death of a grandchild that ignores SIGTERM:
#: the leash's grace, then its SIGKILL. Measured in the same runs: at most
#: 5.038 s. The pool test uses it for every process: at most 0.019 s there,
#: n=20 at load 26.0 to 30.0, because the worker's own kill ends the group
#: as soon as the agent has gone.
GRANDCHILD_BOUND_SECONDS = DEFAULT_TERM_GRACE_SECONDS + 2.0

#: When the grandchild must still be alive: after the agent has died of
#: the SIGTERM and well inside the grace, so a SIGKILL sent first fails it.
STILL_ALIVE_AT_SECONDS = 1.0

#: How long the stand-in agents may take to start. A fuse, not a measurement.
START_FUSE_SECONDS = 120.0


def _silent_agent(pairs: Path) -> str:
    """Starts a grandchild that ignores SIGTERM in the agent's own group,
    appends ``<agent pid> <grandchild pid>`` to ``pairs``, then waits
    without writing a byte."""
    return f"(trap '' TERM; exec sleep 600) & echo $$ $! >> '{pairs}'; exec sleep 600"


def _pairs(path: Path) -> list[tuple[int, int]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [(int(a), int(g)) for a, g in (line.split() for line in text.splitlines()) if g]


def _ks(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """The real CLI in its own session, so its pgid is its pid."""
    return subprocess.Popen(
        [sys.executable, "-m", "kstrl", *args],
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


_KILLS = {
    "factory-sigkill-group": ("factory", lambda pid: os.killpg(pid, signal.SIGKILL)),
    "factory-sigkill-parent": ("factory", lambda pid: os.kill(pid, signal.SIGKILL)),
    "factory-sighup-group": ("factory", lambda pid: os.killpg(pid, signal.SIGHUP)),
    "run-sigkill-group": ("run", lambda pid: os.killpg(pid, signal.SIGKILL)),
}


@pytest.mark.parametrize("case", sorted(_KILLS))
def test_a_silent_agent_and_its_grandchild_die_with_kstrl(tmp_path: Path, case: str) -> None:
    """The issue's acceptance, without worktrees: the owning kstrl process
    dies while its agent is silent. The agent is gone within
    ``AGENT_BOUND_SECONDS``. Its grandchild, which ignores SIGTERM, is
    still alive at ``STILL_ALIVE_AT_SECONDS``, so the leash sent SIGTERM
    first, and gone within ``GRANDCHILD_BOUND_SECONDS``, so the SIGKILL
    after the grace came."""
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
            f"agent pid {agent} alive {AGENT_BOUND_SECONDS}s after kstrl died ({case})"
        )
        time.sleep(max(0.0, killed + STILL_ALIVE_AT_SECONDS - time.monotonic()))
        assert pid_is_alive(grandchild), (
            f"grandchild pid {grandchild} died before the grace: it was not sent SIGTERM first"
        )
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
