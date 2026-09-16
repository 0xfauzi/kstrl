"""#203 item 3: a second scheduled firing cannot reap the run it is locked out of.

Interval mode fires a NEW `ks serve --once` PROCESS on a calendar, so
nothing in-process guards the lease reaper, which requeues a RUNNING item
on a WALL-CLOCK lease that advances across a suspend. What stops a second
firing requeueing a run that is still executing is that `serve()` takes
`serve_lock` BEFORE it reaches the reaper.
`tests/test_serve_process_tree.py::TestTheReaperRunsOnlyUnderTheDaemonLock`
pins that in one process; this module pins it the way it runs in
production, against a separate process holding the real lock file and a
real `ks serve` subprocess.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.serve import RunOutcome, serve
from kstrl.workqueue import ItemState, Queue, QueueConfig
from tests.helpers import procs
from tests.helpers.astwalk import (
    all_nodes,
    label,
    leaf_name,
    package_sources,
    parse,
    parsed,
    scope_of,
)
from tests.test_serve_process_tree import _running_item_with_a_lapsed_lease

#: Real-time fuse for every child this module starts. A mutation that
#: removes a synchronisation point fails as a HANG, not as a red
#: assertion, so nothing here is allowed to wait unbounded. Measured
#: children: the lock holder to LOCKED 0.057s, `ks serve --once` 0.40s
#: warm, the lock probe 0.014s, the slowest cold in-process `serve()`
#: seen 2.16s.
_CHILD_TIMEOUT_SECONDS = 120.0

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
#: one holding it, by taking the same `serve_lock` the holder took.
_PROBE = """
import sys
from pathlib import Path
from kstrl.serve import ServeLockedError, serve_lock
try:
    with serve_lock(Path(sys.argv[1])):
        print("free", flush=True)
except ServeLockedError:
    print("held", flush=True)
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A repo root with the open-PR bound off, so no test reaches `gh`.

    A SIBLING of the state dir `isolate_kstrl_state` points XDG_STATE_HOME
    at, not a parent of it: kstrl refuses to read the daemon ledger when
    XDG_STATE_HOME resolves under the repository tree, and that refusal
    returns from the cycle BEFORE the reaper, which would make the
    control below pass for the wrong reason.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "kstrl.toml").write_text("[serve]\nmax_open_prs = 0\n", encoding="utf-8")
    return repo


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Kill only the group this module created."""
    procs.kill_group(process.pid)
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
    procs.wait_for_line(holder, "LOCKED", _CHILD_TIMEOUT_SECONDS)
    return holder


def _run_serve_once(root: Path) -> subprocess.CompletedProcess[str]:
    """The real CLI, as its own process, against `root`.

    No HOME/XDG_STATE_HOME/KSTRL_NO_TUI env override here: the autouse
    `isolate_kstrl_state` fixture already points XDG_STATE_HOME at a
    sibling of `tmp_path`, HOME is read only when it is unset, and
    KSTRL_NO_TUI is gated on a tty a subprocess spawned under pytest
    does not have.
    """
    return procs.run_serve_subprocess(root, "--once", fuse=_CHILD_TIMEOUT_SECONDS)


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
        root: Path,
    ) -> None:
        """Without this, the guard below passes on an unreapable fixture.

        A lease that has not lapsed makes "the reaper did not touch it"
        true for the wrong reason.
        """
        item_id = _running_item_with_a_lapsed_lease(root, os.getpid())

        done = _run_serve_once(root)

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
        root: Path,
    ) -> None:
        holder = _start_lock_holder(root)
        try:
            item_id = _running_item_with_a_lapsed_lease(root, holder.pid)
            done = _run_serve_once(root)
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
                [sys.executable, "-c", _PROBE, str(root)],
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
        root: Path,
    ) -> None:
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
            return RunOutcome(0)

        serve(root, once=True, runner=runner)

        assert observed == ["held"], (
            f"the daemon lock was {observed} while the item was running; "
            f"serve() must HOLD serve_lock across the cycle, not merely "
            f"acquire it before one (#203)."
        )

    def test_control_the_probe_reports_free_with_no_serve_running(
        self,
        root: Path,
    ) -> None:
        """An "held" that is always "held" would measure nothing."""
        assert self._probe(root) == "free"


class TestReapLeasesAndServeCycleHaveOneCallerEach:
    """#203 item 9: a census over the one call site each has today.

    The six behaviour tests above pin the ORDER `serve()` takes today;
    none of them would notice a NEW, unlocked caller reaching
    `reap_leases` or `serve_cycle` some other way (a future `ks queue
    reap`, say). This walks every module under `kstrl/` and flags any
    call to `reap_leases` outside `serve_cycle`, or to `serve_cycle`
    outside `serve`, by file and enclosing function.

    Built on `leaf_name` (the call's final identifier) and `scope_of`
    (the enclosing function, by qualified name) from
    `tests/helpers/astwalk`, not on `resolved_calls`: both `reap_leases`
    and `serve_cycle` are called BARE, from the module that defines
    them, and `bindings()` deliberately does not bind a local `def` (see
    its docstring), so a resolved-name walk sees no candidates at all
    here and would pass a scratch caller in silence - measured against
    the exact shape of the plant this test exists to catch.
    """

    @staticmethod
    def _offenders_in(
        tree: ast.Module,
        where: str,
        callee: str,
        allowed_caller: str,
    ) -> list[str]:
        owner = scope_of(tree)
        offenders: list[str] = []
        for node in all_nodes(tree):
            if not (isinstance(node, ast.Call) and leaf_name(node.func) == callee):
                continue
            scope = owner.get(id(node), "<module>")
            if scope == allowed_caller or scope.startswith(f"{allowed_caller}."):
                continue
            offenders.append(f"{where}:{node.lineno} in {scope}")
        return offenders

    @classmethod
    def _callers_outside(cls, callee: str, allowed_caller: str) -> list[str]:
        offenders: list[str] = []
        for source_file in package_sources():
            offenders += cls._offenders_in(
                parsed(source_file), label(source_file), callee, allowed_caller
            )
        return offenders

    def test_the_predicate_fires_on_a_planted_violation(self) -> None:
        """Control, per #324: `assert hits == []` alone cannot tell a
        narrow walk from a switched-off one."""
        tree = parse("def rogue():\n    reap_leases(q)\n")
        offenders = self._offenders_in(tree, "<control>", "reap_leases", "serve_cycle")
        assert offenders, "the census predicate matched nothing in a planted violation"

    def test_every_reap_leases_call_sits_inside_serve_cycle(self) -> None:
        offenders = self._callers_outside("reap_leases", "serve_cycle")
        assert offenders == [], f"reap_leases called outside serve_cycle: {offenders}"

    def test_every_serve_cycle_call_sits_inside_serve(self) -> None:
        offenders = self._callers_outside("serve_cycle", "serve")
        assert offenders == [], f"serve_cycle called outside serve: {offenders}"
