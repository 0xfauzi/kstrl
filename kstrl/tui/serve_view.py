"""What ``ks serve`` is doing, read from the files it already writes (#433 M1, Q1).

A ``ks serve`` daemon runs work the TUI did not start, and nothing on
home or on a run board said so: the D11 audit watched ``ks dash`` report
the previous run as finished while the daemon ran an architect call.

Sources, all under ``.kstrl/queue/`` (``kstrl.workqueue``):

- the item directories, one per item, under the state directory that IS
  the item's state (``Queue.items``, a pure read). ``leased`` and
  ``running`` items are in flight, ``queued`` ones wait in run order.
- ``serve.lock``: the daemon holds its flock for its whole lifetime
  (``serve.serve_lock``) and writes its pid there, which it never
  clears. The flock, probed without blocking, says whether it runs; the
  pid is only shown.

The queue does not record which run an in-flight item executes: the
daemon calls ``Queue.start`` without a run id, and the factory child is
not told its item. The one join available is a pid: the daemon adopts
the child's pid into the item's ``lease_pid`` (``Queue.adopt_lease``) and
the factory writes its own pid into ``.kstrl/factory.lock``. When the two
agree and the lock is held, the item's run is the newest factory run,
which is the run the lock belongs to (``runs.discover_runs`` attributes
it the same way). Otherwise the run is shown as not recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

RUNNING = "running"
NOT_RUNNING = "not running"
UNKNOWN = "unknown"

_IN_FLIGHT = ("leased", "running")
_SHOWN = ("running", "leased", "queued")


@dataclass(frozen=True)
class ServeItem:
    item_id: str
    title: str
    #: queued | leased | running.
    state: str
    #: 1-based place in the run order for a queued item, else 0.
    position: int = 0
    #: The run an in-flight item executes, "" when the join fails.
    run_id: str = ""


@dataclass(frozen=True)
class ServeState:
    #: RUNNING | NOT_RUNNING | UNKNOWN.
    daemon: str
    daemon_pid: int = 0
    items: tuple[ServeItem, ...] = ()
    problem: str = ""

    @property
    def in_flight(self) -> tuple[ServeItem, ...]:
        return tuple(item for item in self.items if item.state in _IN_FLIGHT)

    @property
    def queued(self) -> tuple[ServeItem, ...]:
        return tuple(item for item in self.items if item.state == "queued")

    def item_for_run(self, run_id: str) -> ServeItem | None:
        return next((item for item in self.in_flight if item.run_id == run_id), None)


def _read_pid(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:64].strip()
    except OSError:
        return 0
    return int(text) if text.isdigit() else 0


def _daemon(queue_dir: Path) -> tuple[str, int]:
    """RUNNING while the daemon holds its flock; the pid is what it wrote."""
    from kstrl.tui.runs import lock_held

    lock = queue_dir / "serve.lock"
    if not lock.exists():
        return NOT_RUNNING, 0
    if not lock_held(lock):
        return NOT_RUNNING, 0
    return RUNNING, _read_pid(lock)


def read_serve_state(
    root_dir: Path,
    newest_factory_run_id: str,
    factory_lock_held: bool,
) -> ServeState | None:
    """The daemon and its visible items; None when this project has no queue."""
    from kstrl.tui.runs import factory_lock_path
    from kstrl.workqueue import ItemState, Queue, queue_root

    queue_dir = queue_root(root_dir)
    if not queue_dir.is_dir():
        return None
    daemon, daemon_pid = _daemon(queue_dir)
    try:
        found = Queue(root_dir).items(tuple(ItemState(state) for state in _SHOWN))
    except (OSError, ValueError) as exc:
        return ServeState(daemon=daemon, daemon_pid=daemon_pid, problem=f"queue unreadable: {exc}")
    lock_pid = _read_pid(factory_lock_path(root_dir)) if factory_lock_held else 0
    items: list[ServeItem] = []
    position = 0
    for item in found:
        state = str(item.state)
        run_id = ""
        if state in _IN_FLIGHT and lock_pid and item.lease_pid == lock_pid:
            run_id = newest_factory_run_id
        if state == "queued":
            position += 1
        items.append(
            ServeItem(
                item_id=item.item_id,
                title=item.title,
                state=state,
                position=position if state == "queued" else 0,
                run_id=run_id,
            )
        )
    return ServeState(daemon=daemon, daemon_pid=daemon_pid, items=tuple(items))


def short_item_id(item_id: str) -> str:
    """``q-20260923-205005.895694-793181`` -> ``q-793181``."""
    tail = item_id.rsplit("-", 1)[-1]
    return f"q-{tail}" if tail and tail != item_id else item_id
