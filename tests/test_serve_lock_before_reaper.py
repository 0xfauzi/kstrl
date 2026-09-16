"""#203 item 3: a second scheduled firing cannot reap the run it is locked out of.

Interval mode fires a NEW `ks serve --once` PROCESS on a calendar, so
nothing in-process guards the lease reaper. `reap_leases` requeues a
RUNNING item on `lease_expired(moment)` measured against the WALL CLOCK,
which advances across a suspend, so a run suspended overnight blows its
3600s lease while its pid is alive. What stops the next firing requeueing
a run that is still executing is that `serve()` takes `serve_lock` BEFORE
it reaches the reaper.

`tests/test_serve_process_tree.py::TestTheReaperRunsOnlyUnderTheDaemonLock`
pins that in one process. This module pins it the way it actually
happens: a separate process holds the real lock file, and the real CLI
runs as a real subprocess against its own root, HOME and XDG state dir.

The second class is the half the in-process tests could not reach, and
its absence was measured and recorded in that class's docstring: a
`serve()` that takes the lock, RELEASES it, and then runs the cycle
leaves every contended test green, because the contended case still
raises at acquisition. The only way to see it is to ask, from OUTSIDE,
whether the lock is held WHILE the item runs.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kstrl.serve import SERVE_LOCK_FILENAME, RunOutcome, serve
from kstrl.workqueue import ItemState, Queue, QueueConfig, queue_root

#: Real-time fuse for every child this module starts. A mutation that
#: removes a synchronisation point fails as a HANG, not as a red
#: assertion, so nothing here is allowed to wait unbounded.
_CHILD_TIMEOUT_SECONDS = 120.0

#: The lease TTL the seeded item is given, and the wall-clock wait that
#: makes it lapse. Real time, not a mocked clock, because
#: `lease_expired` compares against the wall clock and that is the whole
#: reason a suspended run is at risk.
_LAPSED_LEASE_TTL_SECONDS = 0.01
_LAPSE_WAIT_SECONDS = 0.1

#: Holds the daemon lock and then exits on its own, so a killpg that
#: never runs cannot leave a process behind.
_HOLDER = """
import sys, time
from pathlib import Path
from kstrl.serve import serve_lock
with serve_lock(Path(sys.argv[1])):
    print("LOCKED", flush=True)
    time.sleep(float(sys.argv[2]))
"""

#: Asks whether the daemon lock is held, from a process that is not the
#: one holding it. Opens the file by PATH so it gets its own open file
#: description: an inherited descriptor would share the parent's lock
#: and report "free" whatever the truth is.
_PROBE = """
import fcntl, sys
from pathlib import Path
handle = open(Path(sys.argv[1]), "a+", encoding="utf-8")
try:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("held", flush=True)
else:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    print("free", flush=True)
finally:
    handle.close()
"""

pytestmark = [
    pytest.mark.usefixtures("no_open_prs"),
    pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only"),
]


@pytest.fixture(autouse=True)
def _no_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading real spend needs a run dir; nothing here is about cost."""
    from kstrl.serve import RunSpend

    monkeypatch.setattr(
        "kstrl.serve.read_run_spend",
        lambda root, run_id: RunSpend(),
    )


def _serve_root(tmp_path: Path) -> Path:
    """A repo root with the open-PR bound off, so no test reaches `gh`.

    A SIBLING of the state dir below, not a parent of it: kstrl refuses
    to read the daemon ledger when XDG_STATE_HOME resolves under the
    repository tree, and that refusal returns from the cycle BEFORE the
    reaper, which would make the control below pass for the wrong
    reason.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "kstrl.toml").write_text("[serve]\nmax_open_prs = 0\n", encoding="utf-8")
    return root


def _seed_running_item_with_a_lapsed_lease(root: Path, pid: int) -> str:
    """Exactly the shape a suspended run leaves: live pid, lapsed lease."""
    queue = Queue(root, QueueConfig(lease_ttl_seconds=_LAPSED_LEASE_TTL_SECONDS, max_attempts=3))
    item = queue.add("# Spec\n\nDo the thing.\n")
    queue.start(queue.lease(item, pid=pid))
    time.sleep(_LAPSE_WAIT_SECONDS)
    return item.item_id


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Kill only the group this module created."""
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=_CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pytest.fail("a child this test started outlived its fuse (hung, not failed)")


def _start_lock_holder(root: Path) -> subprocess.Popen[str]:
    """A separate process holding the real `serve.lock`, ready when it says so."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root), str(_CHILD_TIMEOUT_SECONDS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert holder.stdout is not None
    # select() before every read, never a bare readline: a readline on a
    # child that never writes blocks forever, and a fuse the child can
    # switch off by hanging is the defect class this module is about.
    deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(holder.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_group(holder)
                pytest.fail("the lock holder never reported LOCKED (hung, not failed)")
            if not selector.select(remaining):
                continue
            line = holder.stdout.readline()
            if line == "":
                _kill_group(holder)
                pytest.fail("the lock holder exited before it took the lock")
            if line.strip() == "LOCKED":
                return holder
    finally:
        selector.close()


def _run_serve_once(tmp_path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    """The real CLI, as its own process, with its own HOME and state dir."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(state)
    env["KSTRL_NO_TUI"] = "1"
    child = subprocess.Popen(
        [sys.executable, "-m", "kstrl", "serve", "--once", "--root", str(root), "--no-color"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = child.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_group(child)
        pytest.fail("`ks serve --once` outlived its fuse (hung, not failed)")
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


def _reaper_entries(root: Path, item_id: str) -> list[dict[str, object]]:
    return [
        entry
        for entry in Queue(root, QueueConfig()).journal_entries(item_id)
        if entry.get("actor") == "reaper"
    ]


class TestASecondFiringCannotReapARunItIsLockedOutOf:
    """The real CLI against a lock a real other process holds."""

    def test_control_the_same_item_is_reaped_when_the_lock_is_free(
        self,
        tmp_path: Path,
    ) -> None:
        """Without this, the guard below passes on an unreapable fixture.

        A lease that has not actually lapsed makes "the reaper did not
        touch it" true for the wrong reason.
        """
        root = _serve_root(tmp_path)
        item_id = _seed_running_item_with_a_lapsed_lease(root, os.getpid())

        done = _run_serve_once(tmp_path, root)

        assert done.returncode == 0, f"stderr: {done.stderr}"
        item = Queue(root, QueueConfig()).get(item_id)
        assert item is not None
        assert item.state is ItemState.QUEUED, (
            f"the fixture is not reapable, so the guard next door would "
            f"pass whatever the lock ordering is: {item.state.value}"
        )
        assert _reaper_entries(root, item_id), "the reaper left no journal record"

    def test_a_held_lock_stops_the_second_firing_before_the_reaper(
        self,
        tmp_path: Path,
    ) -> None:
        root = _serve_root(tmp_path)
        holder = _start_lock_holder(root)
        try:
            item_id = _seed_running_item_with_a_lapsed_lease(root, holder.pid)
            done = _run_serve_once(tmp_path, root)
        finally:
            _kill_group(holder)

        assert done.returncode == 2, (
            f"a second firing did not exit on the daemon lock: rc="
            f"{done.returncode}, stderr: {done.stderr}"
        )
        assert "another ks serve" in done.stderr, done.stderr
        item = Queue(root, QueueConfig()).get(item_id)
        assert item is not None
        assert item.state is ItemState.RUNNING, (
            f"a second firing reaped a run that is still executing: the "
            f"item is {item.state.value}, not running. serve() must take "
            f"serve_lock BEFORE the cycle's reaper (#203)."
        )
        assert item.attempts == 1, (
            f"the item has been charged {item.attempts} attempts; a refused "
            f"second firing must spend nothing"
        )
        assert _reaper_entries(root, item_id) == [], (
            "the reaper wrote a journal record while another process held the daemon lock"
        )


class TestTheLockIsHeldForTheWholeCycle:
    """Not "the contended firing exits", but "the lock is held while work runs".

    The contended tests above pass for a `serve()` that takes the lock,
    releases it and then runs the cycle, because acquisition still
    raises. These two ask a child process whether the lock is held at
    the moment the item is running, which is the only reading that
    separates the two.
    """

    @staticmethod
    def _probe(root: Path) -> str:
        try:
            done = subprocess.run(
                [sys.executable, "-c", _PROBE, str(queue_root(root) / SERVE_LOCK_FILENAME)],
                capture_output=True,
                text=True,
                timeout=_CHILD_TIMEOUT_SECONDS,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("the lock probe outlived its fuse (hung, not failed)")
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def test_a_child_cannot_take_the_lock_while_the_item_runs(
        self,
        tmp_path: Path,
    ) -> None:
        root = _serve_root(tmp_path)
        Queue(root, QueueConfig()).add("# Spec\n\nDo the thing.\n")
        observed: list[str] = []

        def runner(
            *,
            root_dir: Path,
            spec_path: Path,
            project_name: str,
            pause_before_pr_merge: bool,
            timeout_seconds: float,
            on_spawn: object = None,
        ) -> RunOutcome:
            observed.append(self._probe(root_dir))
            run_dir = root_dir / ".kstrl" / "runs" / "factory-20260730-000000.000000-aaa"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(0)

        serve(root, once=True, runner=runner)

        assert observed == ["held"], (
            f"the daemon lock was {observed} while the item was running; "
            f"serve() must HOLD serve_lock across the cycle, not merely "
            f"acquire it before one (#203)."
        )

    def test_control_the_probe_reports_free_with_no_serve_running(
        self,
        tmp_path: Path,
    ) -> None:
        """An "held" that is always "held" would measure nothing."""
        root = _serve_root(tmp_path)
        Queue(root, QueueConfig()).ensure_dirs()
        assert self._probe(root) == "free"
