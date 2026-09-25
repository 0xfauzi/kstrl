"""A process an agent's tool leaves in its worktree does not outlive the attempt (#461).

The defect: an engineer agent's shell tool starts each command in a
process group of its own. kstrl ends an agent by signalling the agent's
group, so a test server the engineer started kept its port and ran code
from a deleted worktree for an hour after the run ended, and nothing in
the manifest said so. The fix keys on the one thing every such process
carries: a working directory inside the worktree. The attempt's end, and
every removal of a worktree, kill the group of each process found there
and record it as an ``orphan_process`` finding.

The same PR fixes one SIGINT to ``ks factory --no-tui --ui plain``. With
one worker the agent runs on the main thread, so the stop the signal
requested waited for the agent's iteration to end (120 s measured). With a
pool, the workers took the terminal's SIGINT as a KeyboardInterrupt, the
parent re-raised it from ``future.result()`` and exited 1 with no cleanup
and every component left RUNNING.

Every process here is one the test started or one whose pid a fake agent
wrote to a file outside the worktree. Nothing asks what else the machine
is running (#292). Every such process ignores SIGTERM, as the survivors
the issue measured did, so only a SIGKILL ends it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kstrl.factory import _setup_worktree
from kstrl.findings import Finding
from kstrl.loop import STOP_EXIT_CODE
from kstrl.manifest import Manifest
from kstrl.worktree_sweep import ORPHAN_CATEGORY, sweep_worktree
from tests.helpers import gitrepo, procs

COMP = "comp-a"
COMPLETE = "echo '<promise>COMPLETE</promise>'"

#: ``sleep`` with SIGTERM ignored, for a process the test starts itself.
#: ``trap ''`` sets SIG_IGN, which survives ``exec``.
DEAF_SLEEP = ["sh", "-c", "trap '' TERM; exec sleep 600"]

FLAGS = (
    "--yes",
    "--ui",
    "plain",
    "--no-color",
    "--no-tui",
    "--max-retries",
    "0",
    "--no-prs",
    "--review-mode",
    "skip",
    "--contract-check",
    "skip",
    "--test-command",
    "true",
    "--typecheck-command",
    "true",
    "--lint-command",
    "true",
)

#: Seconds a stopped run may take to exit after one SIGINT. Measured on
#: the fixed tree at load average 20: 0.97 s with two workers, 2.57 s with
#: one. Before the fix, one worker took 120.16 s (the agent's whole sleep).
STOP_BOUND_SECONDS = 30.0


def _repo(tmp_path: Path) -> Path:
    """One component, one story, a real git repository."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    prd = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
    prd.parent.mkdir(parents=True)
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    prd.write_text(
        json.dumps({"branchName": f"kstrl/factory/{COMP}", "userStories": [story]}),
        encoding="utf-8",
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": COMP,
                "title": COMP,
                "description": "",
                "dependencies": [],
                "prdPath": f"scripts/kstrl/feature/{COMP}/prd.json",
                "branchName": f"kstrl/factory/{COMP}",
            }
        ],
    }
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


def _env(agent: str, path_prefix: Path | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    env["AGENT_CMD"] = agent
    env["KSTRL_KNOWLEDGE_ENABLED"] = "0"
    env["KSTRL_NO_TUI"] = "1"
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return env


def _factory(
    root: Path,
    agent: str,
    max_parallel: str,
    *extra: str,
    path_prefix: Path | None = None,
) -> subprocess.Popen[str]:
    """The real CLI in its own session, so its pgid is its pid."""
    argv = [
        sys.executable,
        "-m",
        "kstrl",
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
    return subprocess.Popen(
        argv,
        cwd=root,
        env=_env(agent, path_prefix),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def _detach(pidfile: Path) -> str:
    """A shell command that starts ``sleep 600`` with SIGTERM ignored, in a
    session of its own, in the current directory, writes its pid outside
    the worktree and returns at once. The ignored SIGTERM is inherited
    through ``exec``. Its stdio is /dev/null, so it holds no pipe of the
    agent's."""
    code = (
        "import signal,subprocess;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "p=subprocess.Popen(['sleep','600'],start_new_session=True,"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"open({str(pidfile)!r},'w').write(str(p.pid))"
    )
    return f'{sys.executable} -c "{code}"'


def _record_sigint_disposition(outfile: Path) -> str:
    """A shell command that writes how a process the agent starts handles
    SIGINT. A Python child prints ``default_int_handler`` when it inherited
    the default disposition, and ``1`` (SIG_IGN) when it inherited SIGINT
    ignored."""
    code = f"import signal;open({str(outfile)!r},'w').write(str(signal.getsignal(signal.SIGINT)))"
    return f'{sys.executable} -c "{code}"'


def _orphans(root: Path, phase: str = "") -> list[Finding]:
    """The component's orphan findings, from the manifest on disk; only
    those recorded by ``phase`` when one is named."""
    comp = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").get_component(COMP)
    assert comp is not None
    return [f for f in comp.findings if f.category == ORPHAN_CATEGORY and phase in ("", f.phase)]


def _naming(root: Path, pid: int, phase: str = "") -> list[str]:
    return [f.explanation for f in _orphans(root, phase) if f"pid {pid} " in f.explanation]


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop_factory(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        procs.kill_group(proc.pid)
        proc.wait(timeout=30)


def _run_to_end(proc: subprocess.Popen[str]) -> str:
    out, _ = proc.communicate(timeout=240)
    assert proc.returncode == 0, out
    return out


@pytest.mark.parametrize("max_parallel", ["1", "2"])
def test_a_detached_tool_process_dies_with_the_attempt_and_is_named(
    tmp_path: Path, max_parallel: str
) -> None:
    """The issue's acceptance: a fake agent starts a process that ignores
    SIGTERM in its own session inside the worktree and exits. The process
    must be gone when the run ends, and a finding on the component must
    name its pid.

    The finding must come from the ENGINEER phase, which is the sweep at the
    end of the attempt. The worktree's removal sweeps as well and would
    also kill it, but only after Phase 1 had run its checks in a worktree
    still holding the engineer's server.

    The agent also records how a process it starts handles SIGINT: a pool
    worker that ignored SIGINT with ``SIG_IGN`` would hand that disposition
    to the agent and every command its tools run, through ``exec``."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "detached.pid"
    disposition = tmp_path / "sigint.txt"
    agent = f"{_record_sigint_disposition(disposition)} && {_detach(pidfile)} && {COMPLETE}"
    proc = _factory(root, agent, max_parallel)
    pid: int | None = None
    try:
        _run_to_end(proc)
        pid = procs.read_pid(pidfile)
        assert procs.wait_for_pid_to_die(pid, timeout=10), (
            f"pid {pid}, started by the agent in its own session inside the "
            f"worktree, survived the run"
        )
        assert _naming(root, pid, "engineer"), (
            f"no engineer-phase finding names pid {pid}: {_orphans(root)}"
        )
        seen = disposition.read_text(encoding="utf-8")
        assert "default_int_handler" in seen, f"the agent started with SIGINT {seen!r}"
    finally:
        _stop_factory(proc)
        if pid is not None:
            _kill_pid(pid)


@pytest.mark.parametrize(
    ("max_parallel", "extra"),
    [("1", ()), ("2", ()), ("2", ("--keep-worktrees-on-failure",))],
    ids=["inline", "pool", "pool-keep-worktree"],
)
def test_one_sigint_stops_the_run_mid_iteration_and_leaves_no_agent_process(
    tmp_path: Path, max_parallel: str, extra: tuple[str, ...]
) -> None:
    """The coordinator's acceptance on #461: one SIGINT to the run's process
    group (what Ctrl-C sends) while the engineer is mid-iteration. The run
    exits 130 within ``STOP_BOUND_SECONDS``, the agent's own group is empty
    and the process its tool detached is dead and named.

    ``pool-keep-worktree`` keeps the aborted component's worktree as
    evidence, so the process has to be swept without the worktree being
    removed."""
    root = _repo(tmp_path)
    marker = tmp_path / "started"
    agent_pidfile = tmp_path / "agent.pid"
    detached_pidfile = tmp_path / "detached.pid"
    agent = (
        f"{_detach(detached_pidfile)} && echo $$ > '{agent_pidfile}' && "
        f"touch '{marker}' && sleep 120 && {COMPLETE}"
    )
    proc = _factory(root, agent, max_parallel, *extra)
    agent_pid: int | None = None
    detached_pid: int | None = None
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert time.monotonic() < deadline, "the engineer never started"
            assert proc.poll() is None, proc.communicate()[0]
            time.sleep(0.05)
        agent_pid = procs.read_pid(agent_pidfile)
        detached_pid = procs.read_pid(detached_pidfile)
        started = time.monotonic()
        os.killpg(proc.pid, signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=STOP_BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_factory(proc)
            pytest.fail(f"still running {STOP_BOUND_SECONDS}s after one SIGINT")
        elapsed = time.monotonic() - started
        assert proc.returncode == STOP_EXIT_CODE, (
            f"rc={proc.returncode} after {elapsed:.2f}s\n{out}"
        )
        # The shell the agent ran in leads the agent's own group.
        assert procs.wait_for_group_to_die(agent_pid, timeout=10), "the agent's group survived"
        assert procs.wait_for_pid_to_die(detached_pid, timeout=10), (
            f"pid {detached_pid}, detached by the agent's tool, survived the stop"
        )
        assert _naming(root, detached_pid), f"no {ORPHAN_CATEGORY} finding names pid {detached_pid}"
    finally:
        _stop_factory(proc)
        if agent_pid is not None:
            procs.kill_group(agent_pid)
        if detached_pid is not None:
            _kill_pid(detached_pid)


def test_a_process_beside_the_worktree_is_left_alone(tmp_path: Path) -> None:
    """``comp-a-2`` starts with ``comp-a`` as a string and is not inside it.
    The agent starts one process in its worktree and one in a sibling
    directory; only the first is the attempt's."""
    root = _repo(tmp_path)
    inside_pidfile = tmp_path / "inside.pid"
    beside_pidfile = tmp_path / "beside.pid"
    agent = (
        f"{_detach(inside_pidfile)} && mkdir -p ../{COMP}-2 && cd ../{COMP}-2 && "
        f"{_detach(beside_pidfile)} && {COMPLETE}"
    )
    proc = _factory(root, agent, "1")
    inside: int | None = None
    beside: int | None = None
    try:
        _run_to_end(proc)
        inside = procs.read_pid(inside_pidfile)
        beside = procs.read_pid(beside_pidfile)
        assert procs.wait_for_pid_to_die(inside, timeout=10), "the worktree's process survived"
        assert not procs.wait_for_pid_to_die(beside, timeout=1), (
            f"pid {beside}, running beside the worktree and not in it, was killed"
        )
        assert not _naming(root, beside), f"a finding names pid {beside}: {_orphans(root)}"
    finally:
        _stop_factory(proc)
        for pid in (inside, beside):
            if pid is not None:
                _kill_pid(pid)


def test_a_census_that_lists_nothing_is_recorded_not_read_as_clean(tmp_path: Path) -> None:
    """An ``lsof`` that exits 0 having listed nothing is a census that saw
    nothing. The run must record that it could not look, not report a
    clean worktree: the listing has to show the factory's own working
    directory before it is believed about anyone else's."""
    root = _repo(tmp_path)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "lsof").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (shim / "lsof").chmod(0o755)
    pidfile = tmp_path / "detached.pid"
    proc = _factory(root, f"{_detach(pidfile)} && {COMPLETE}", "1", path_prefix=shim)
    pid: int | None = None
    try:
        _run_to_end(proc)
        pid = procs.read_pid(pidfile)
        failed = [f for f in _orphans(root, "engineer") if "census_failed" in f.tags]
        assert failed, f"a census that listed nothing was read as clean: {_orphans(root)}"
    finally:
        _stop_factory(proc)
        if pid is not None:
            _kill_pid(pid)


def test_a_census_that_cannot_start_is_reported_not_read_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``lsof``. Called directly: ``lsof`` sits in a system
    directory on PATH, so a run cannot be made to miss it without taking
    that whole directory off PATH."""
    worktree = tmp_path / "wt"
    child = _deaf_sleep_in(worktree)
    monkeypatch.setattr("kstrl.worktree_sweep.LSOF_ARGV", ["kstrl-no-such-command-461"])
    try:
        sweep = sweep_worktree(worktree)
        assert sweep.survivors == ()
        assert sweep.error, "a census that never ran must say so"
        assert child.poll() is None
    finally:
        _dispose(child)


def test_without_worktrees_the_project_root_is_not_swept(tmp_path: Path) -> None:
    """Under ``--no-worktrees`` the engineer runs in the project root, where
    the operator's shells and editors also run. A process the agent leaves
    there is the operator's business, not the sweep's."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "detached.pid"
    proc = _factory(root, f"{_detach(pidfile)} && {COMPLETE}", "1", "--no-worktrees")
    pid: int | None = None
    try:
        _run_to_end(proc)
        pid = procs.read_pid(pidfile)
        assert not procs.wait_for_pid_to_die(pid, timeout=1), (
            f"pid {pid}, running in the project root, was killed"
        )
        assert _orphans(root) == [], "the project root was swept"
    finally:
        _stop_factory(proc)
        if pid is not None:
            _kill_pid(pid)


def _deaf_sleep_in(cwd: Path) -> subprocess.Popen[bytes]:
    cwd.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        DEAF_SLEEP,
        cwd=cwd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dispose(*children: subprocess.Popen[bytes]) -> None:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.parametrize("layout", ["run-dir", "flat"])
def test_a_stale_worktree_is_swept_before_the_next_run_prunes_it(
    tmp_path: Path, layout: str
) -> None:
    """A previous run that was killed leaves its worktrees, and whatever its
    agents' tools left running in them. The next run's prune removes the
    directories; the processes have to go first. ``flat`` is the pre-R0.5
    layout, a worktree directly under ``.kstrl/worktrees/``, which the
    prune recognises by the ``.git`` file inside it."""
    root = _repo(tmp_path)
    worktrees = root / ".kstrl" / "worktrees"
    stale = worktrees / "factory-old" / COMP if layout == "run-dir" else worktrees / "comp-old"
    stale.mkdir(parents=True)
    if layout == "flat":
        (stale / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    child = _deaf_sleep_in(stale)
    proc = _factory(root, COMPLETE, "1")
    try:
        _run_to_end(proc)
        assert child.wait(timeout=10) == -signal.SIGKILL
        assert not stale.exists()
    finally:
        _stop_factory(proc)
        _dispose(child)


def test_a_worktree_recreated_for_a_retry_is_swept_first(tmp_path: Path) -> None:
    """A retry recreates the component's worktree at the same path. What an
    earlier phase left running there (a reviewer's shell, say) goes first.

    Called directly: in a run, the attempt-end sweep has already emptied the
    worktree of the engineer's processes, and reaching this site with one
    still there needs a reviewer agent that leaves a process and a hard-mode
    review that sends the component back."""
    root = _repo(tmp_path)
    branch = f"kstrl/factory/{COMP}"
    worktree = _setup_worktree(COMP, branch, "main", root, "factory-run")
    child = _deaf_sleep_in(worktree / "sub")
    try:
        again = _setup_worktree(COMP, branch, "main", root, "factory-run", fresh_from_base=True)
        assert again == worktree
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        _dispose(child)


#: Leaves ``sleep 600`` running with SIGTERM ignored in a process group whose
#: leader has exited: a shell in a session of its own backgrounds the sleep and
#: returns. The sleep's pgid is then the dead shell's pid, not its own, which is
#: the shape ``server &`` from a tool's shell has once that shell exits.
#: Writes ``<pid> <pgid>`` to the file named by its one argument.
ORPHANED_GROUP_SCRIPT = """\
import os, signal, subprocess, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
out = sys.argv[1]
subprocess.Popen(
    ["sh", "-c", 'sleep 600 & echo $! > "$0"', out + ".tmp"],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
).wait()
pid = int(open(out + ".tmp", encoding="utf-8").read())
open(out, "w", encoding="utf-8").write(f"{pid} {os.getpgid(pid)}")
"""


def test_a_process_whose_group_leader_has_exited_is_killed_through_its_group(
    tmp_path: Path,
) -> None:
    """The sweep must signal the group the process is IN. A sweep that sent
    the signal to the group numbered by the process's own pid would reach no
    group at all for a process that is not its group's leader, record it as
    gone, and leave it running."""
    root = _repo(tmp_path)
    script = tmp_path / "orphaned_group.py"
    script.write_text(ORPHANED_GROUP_SCRIPT, encoding="utf-8")
    ids = tmp_path / "orphaned.ids"
    proc = _factory(root, f"{sys.executable} {script} {ids} && {COMPLETE}", "1")
    pid: int | None = None
    try:
        _run_to_end(proc)
        pid, pgid = (int(field) for field in ids.read_text(encoding="utf-8").split())
        assert pgid != pid, f"precondition: pid {pid} leads its own group"
        assert procs.wait_for_pid_to_die(pid, timeout=10), (
            f"pid {pid}, in group {pgid} whose leader had exited, survived the run"
        )
        assert _naming(root, pid, "engineer"), f"no engineer-phase finding names pid {pid}"
    finally:
        _stop_factory(proc)
        if pid is not None:
            _kill_pid(pid)
