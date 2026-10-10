"""The work queue on disk, with no item transition.

This module holds the queue paths, the lock, the scan of the state
directories, the journal, the pause marker, and ``atomic_write``. The life
of an item (``add`` and each transition) is in ``kstrl.workqueue``.

**Layout.** Each state is a directory under ``.kstrl/queue/``; each item
is a DIRECTORY holding its spec file plus ``meta.json``::

    .kstrl/queue/
      queued/<item_id>/{<spec>, meta.json}
      leased/   claimed by a worker, nothing spent yet
      running/  executing; money is being spent
      done/     finished green
      failed/   finished red and eligible to retry
      poison/   finished red and NOT eligible to retry
      .staging/ items being assembled; NEVER scanned
      journal.jsonl
      queue.lock    (short-lived per-transition mutex)

Pause and spend ledgers live in the XDG control directory (R8.9), not
under ``.kstrl/queue/``, so an agent in a worktree cannot edit them.

The item is a directory rather than two sibling files so that one
``os.replace`` moves the spec and its sidecar together. Two sibling files
would need two renames and could be interrupted between them, leaving an
item whose spec and metadata disagree about what state it is in.

**The directory is the source of truth.** ``meta.json`` also carries a
``state`` field, but it is a convenience mirror: readers derive state
from the parent directory name and overwrite the mirror. That makes the
crash window between "write meta" and "rename dir" harmless in both
directions - whichever step got interrupted, the directory still says
what is true and the mirror self-heals on the next write.
"""

from __future__ import annotations

import json
import shutil
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT, append_records
from kstrl.atomicio import atomic_write_text
from kstrl.jsonread import read_json
from kstrl.statedir import (
    CONTROL_PAUSE,
    control_file,
    control_lock,
    control_untrusted_reason,
    ensure_control_state,
    state_dir,
)
from kstrl.workqueue_items import (
    ALL_STATES,
    ItemState,
    JournalEntry,
    PauseState,
    QueueConfig,
    QueueError,
    QueueItem,
    QueueLockedError,
    _iso,
    _utc_now,
    is_safe_component,
    short_item_id,
)

QUEUE_DIR_NAME = "queue"
META_FILENAME = "meta.json"
JOURNAL_FILENAME = "journal.jsonl"
LOCK_FILENAME = "queue.lock"


#: Prefix for the hidden staging directory used while publishing an item.
#: It lives OUTSIDE the state directories so a scan cannot reach it even
#: if the name filter were removed (review #185 F3).
STAGING_DIR_NAME = ".staging"


def _warn_rejected(path: Path, reason: str) -> None:
    """Announce a skipped item rather than swallowing it.

    A silently dropped queue item is work that vanished. Mirrors
    ``autonomy._warn_rejected_state``.
    """
    warnings.warn(
        f"queue: rejected item {path} ({reason}); skipping",
        RuntimeWarning,
        stacklevel=3,
    )


def _journal_repair_entry() -> dict[str, Any]:
    """The row :meth:`Queue._journal` writes on finding a torn tail.

    Deliberately NOT a :class:`JournalEntry`: it is not a transition,
    it has no item and no states, and giving it the transition shape
    would make it one to every reader that walks the journal. It
    carries ``ts`` and ``event`` and nothing else, so
    ``journal_entries(item_id)`` filters it out by the same
    ``item_id`` check it already applies.
    """
    return {"ts": _iso(_utc_now()), "event": JOURNAL_REPAIR_EVENT}


def atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically, creating its directory.

    The write is ``atomicio.atomic_write_text`` (#291, where the mode and
    encoding rules are explained). What this adds is the ``mkdir``, for
    callers that write into a directory they have not created yet, which
    is every publish path in this module.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, content)


@contextmanager
def queue_lock(root_dir: Path, *, blocking: bool = False) -> Iterator[None]:
    """Hold the per-transition queue mutex.

    Short-lived by design: taken around one transition and released, so
    ``ks queue ls`` stays responsive while a run is in flight. The
    daemon's singleton lock is a SEPARATE file (``serve.lock``, PR 2) -
    conflating them would make listing the queue block on a running
    factory.

    POSIX only, like the A4 per-component lock and the run-level factory
    lock. Without ``fcntl`` we degrade to no exclusion rather than
    refusing to work; the sequencing that protects ``attempts`` does not
    depend on the lock.
    """
    lock_path = queue_root(root_dir) / LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return

    handle: IO[str] = open(lock_path, "a+", encoding="utf-8")
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            raise QueueLockedError(f"queue is locked by another process ({lock_path})") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def queue_root(root_dir: Path) -> Path:
    """The queue directory for ``root_dir``."""
    return state_dir(root_dir) / QUEUE_DIR_NAME


def relocated_spec(root_dir: Path, recorded: Path) -> Path | None:
    """Where the queue item spec once at ``recorded`` is now, or None (#639).

    ``ks serve`` plans from ``<queue>/running/<id>/<spec>``, and every later
    transition renames the item directory into another state directory, so
    a plan's pinned spec path stops existing while the file moves with its item.
    None when ``recorded`` is not an item's spec path or no state holds it.
    """
    queue = queue_root(root_dir).resolve()
    try:
        _state, item_id, name = recorded.resolve().relative_to(queue).parts
    except ValueError:
        return None
    for state in ALL_STATES:
        candidate = queue / str(state) / item_id / name
        if candidate.is_file():
            return candidate
    return None


def queue_item_for_spec(root_dir: Path, spec_path: Path) -> str | None:
    """The id of the running queue item whose spec ``spec_path`` is, or None (#644).

    ``ks serve`` runs ``<queue>/running/<id>/<spec>``, so the id is the name
    of the spec's directory when that directory sits in ``running/``. Path
    arithmetic only: no config read and no scan of the queue, so the
    architect's halt path gains no new way to fail. None for every other
    spec, which keeps a `ks factory --spec` outside the queue as it was.
    """
    running = queue_root(root_dir).resolve() / str(ItemState.RUNNING)
    directory = spec_path.resolve().parent
    if directory.parent != running or not is_safe_component(directory.name):
        return None
    return directory.name


class QueueStore:
    """The queue on disk: paths, scan, journal and pause marker.

    ``kstrl.workqueue.Queue`` adds the life of an item on top of it.
    """

    def __init__(self, root_dir: Path, config: QueueConfig | None = None) -> None:
        self.root_dir = root_dir
        self.config = config or QueueConfig()

    @property
    def path(self) -> Path:
        return queue_root(self.root_dir)

    @property
    def journal_path(self) -> Path:
        return self.path / JOURNAL_FILENAME

    def state_path(self, state: ItemState) -> Path:
        return self.path / str(state)

    @property
    def staging_path(self) -> Path:
        """Where items are assembled before being published.

        Deliberately a sibling of the state directories rather than a
        hidden entry inside ``queued/``: a scan cannot reach it even if
        the name filter were removed. Same filesystem, so the publishing
        ``os.replace`` is still atomic (#185 F3).
        """
        return self.path / STAGING_DIR_NAME

    def item_dir(self, item: QueueItem) -> Path:
        if not is_safe_component(item.item_id):
            raise QueueError(f"unsafe queue item id {item.item_id!r}")
        return self.state_path(item.state) / item.item_id

    def ensure_dirs(self) -> None:
        for state in ALL_STATES:
            self.state_path(state).mkdir(parents=True, exist_ok=True)
        self.staging_path.mkdir(parents=True, exist_ok=True)

    def sweep_staging(self) -> int:
        """Delete every half-published item, returning how many went.

        A staging directory only exists while ``add`` is mid-publish, and
        ``add`` runs under the queue mutex, so anything found here while
        holding that mutex is by definition abandoned - no age heuristic
        is needed. Call it under ``queue_lock``; calling it without the
        lock can race a concurrent enqueue. This is the recovery half of
        the staging policy the scan filter enforces (#185 F3).
        """
        if not self.staging_path.is_dir():
            return 0
        swept = 0
        for entry in sorted(self.staging_path.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
                swept += 1
        if swept:
            self._journal(
                JournalEntry(
                    ts=_iso(_utc_now()),
                    item_id="",
                    from_state="staging",
                    to_state="swept",
                    reason="abandoned mid-publish",
                    detail={"count": swept},
                )
            )
        return swept

    # ---------------------------------------------------------------- read

    def _load_item_dir(self, item_path: Path, state: ItemState) -> QueueItem | None:
        # The DIRECTORY ENTRY is the item's identity, exactly as it is the
        # item's state. A sidecar that disagrees is corrupt or tampered
        # with, and trusting its ``item_id`` let a crafted value redirect
        # rmtree outside the queue (#185 F1).
        if item_path.is_symlink():
            _warn_rejected(item_path, "queue items may not be symlinks")
            return None
        if not is_safe_component(item_path.name):
            _warn_rejected(item_path, "unsafe item directory name")
            return None

        meta_path = item_path / META_FILENAME
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except OSError as exc:
            _warn_rejected(item_path, f"unreadable {META_FILENAME}: {exc}")
            return None
        except UnicodeDecodeError as exc:
            # Its own message: "unreadable" points the operator at
            # permissions, and this file's permissions are fine.
            _warn_rejected(item_path, f"{META_FILENAME} is not valid UTF-8: {exc}")
            return None
        try:
            data = read_json(raw)
        except json.JSONDecodeError as exc:
            _warn_rejected(item_path, f"malformed {META_FILENAME}: {exc}")
            return None
        if not isinstance(data, dict):
            _warn_rejected(item_path, f"{META_FILENAME} is not an object")
            return None
        item = QueueItem.from_dict(data)
        if item is None:
            _warn_rejected(
                item_path,
                "missing/unsafe item_id or spec_filename",
            )
            return None
        # The DIRECTORY is authoritative for both identity and state; the
        # sidecar's copies are mirrors that may be one crash behind. See
        # the module docstring.
        if item.item_id != item_path.name:
            warnings.warn(
                f"queue: sidecar item_id {item.item_id!r} disagrees with "
                f"directory {item_path.name!r}; using the directory",
                RuntimeWarning,
                stacklevel=3,
            )
            item.item_id = item_path.name
        item.state = state
        return item

    def items(self, states: tuple[ItemState, ...] | None = None) -> list[QueueItem]:
        """Every item in the given states, in run order."""
        found: list[QueueItem] = []
        for state in states or ALL_STATES:
            directory = self.state_path(state)
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not entry.is_dir():
                    continue
                # Dotted entries are never published items: staging lives
                # under its own directory now, but the filter stays as
                # defence in depth so a leftover from any source is inert
                # rather than surfacing as a phantom item (#185 F3).
                if entry.name.startswith("."):
                    continue
                item = self._load_item_dir(entry, state)
                if item is not None:
                    found.append(item)
        found.sort(key=lambda candidate: candidate.sort_key)
        return found

    def get(self, item_id: str) -> QueueItem | None:
        """Find one item by full id, unique prefix, or its short form.

        The short form is what :func:`short_item_id` prints (#706). An
        item matches if the text is a prefix of its id OR its short form,
        and the two are pooled before the count: a text that is one
        item's prefix and another item's short form is ambiguous, never a
        silent pick of either. More than one match raises rather than
        picking one: silently operating on the wrong unit of work is
        worse than making the operator type more characters.
        """
        exact: QueueItem | None = None
        prefixed: list[QueueItem] = []
        for item in self.items():
            if item.item_id == item_id:
                exact = item
                break
            if item.item_id.startswith(item_id) or short_item_id(item.item_id) == item_id:
                prefixed.append(item)
        if exact is not None:
            return exact
        if not prefixed:
            return None
        if len(prefixed) > 1:
            matches = ", ".join(candidate.item_id for candidate in prefixed)
            raise QueueError(f"{item_id!r} matches multiple items: {matches}")
        return prefixed[0]

    def find_by_source_ref(self, source_ref: str) -> QueueItem | None:
        """First item from a given origin, in any state.

        The in-queue half of PR 3's idempotency: a re-seen GitHub issue
        must not enqueue a second copy of the same work.
        """
        if not source_ref:
            return None
        for item in self.items():
            if item.source_ref == source_ref:
                return item
        return None

    def spec_path(self, item: QueueItem) -> Path:
        """The item's spec file, guaranteed to be inside the item.

        Belt and braces over the decode-time filter: an item constructed
        in memory rather than loaded from disk never passed through
        ``from_dict``, so the containment check is re-done here (#185 F2).
        """
        directory = self.item_dir(item)
        if not is_safe_component(item.spec_filename):
            raise QueueError(f"unsafe spec filename {item.spec_filename!r} on {item.item_id}")
        candidate = directory / item.spec_filename
        if directory.resolve() not in candidate.resolve().parents:
            raise QueueError(f"spec path for {item.item_id} escapes its item directory")
        return candidate

    def read_spec(self, item: QueueItem) -> str:
        return self.spec_path(item).read_text(encoding="utf-8")

    # ------------------------------------------------------------- journal

    def _journal(self, entry: JournalEntry) -> None:
        """Append one transition. Never raises into a caller's path.

        A failed journal write must not undo a transition that already
        happened on disk - the directory is the truth and the journal is
        the narration. Losing a line is visible (journal replay vs a
        directory scan disagree); rolling back a committed rename would
        be silent.

        #331: through ``appendio``, which repairs an unterminated tail
        before appending onto it. Without that, a crash mid-write cost
        the NEXT transition as well as the torn one, measured through
        :meth:`journal_entries`: ``['a']`` where a and b were both
        recorded. The ``"a+b"`` open widens what can fail - a journal
        this process can write but not read is refused rather than
        appended to blind - and the ``OSError`` handler below is what
        that widening lands in, unchanged.

        The repair row carries NO ``item_id``. Measured: that keeps it
        out of ``journal_entries(item_id)``, which is what ``ks queue
        show <id>`` renders, so a repair does not appear in one item's
        history as something that happened to it. The whole-journal
        read still returns it, which is where an operator looking for
        the incident would go.

        No lock. The callers hold ``queue_lock`` around the transition
        this narrates, and this method has never taken one; a lock here
        would nest a second under the first for no measured gain, and
        the journal is one file with one writer (#330's argument for
        the descriptor being its own lock does not apply either, because
        the exclusion the callers already hold covers it).
        """
        self.path.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        try:
            append_records(
                self.journal_path,
                line + "\n",
                # ensure_ascii at its default, unlike the entry line
                # above it. CLAUDE.md: kstrl must not be the SOURCE of
                # non-ASCII bytes, because #291 measured one curly quote
                # breaking six readers under LC_ALL=C. This row's
                # content is ASCII either way today; what the default
                # buys is that it stays so if the helper gains a field.
                # The entry line's setting is pre-existing and out of
                # this class.
                repair=json.dumps(_journal_repair_entry()) + "\n",
            )
        except OSError as exc:
            warnings.warn(
                f"queue: journal append failed ({exc}); the transition itself succeeded",
                RuntimeWarning,
                stacklevel=3,
            )

    def journal_entries(self, item_id: str = "") -> list[dict[str, Any]]:
        """Read the journal, optionally filtered to one item.

        ONE clause for both causes here, and that is not the collapse
        #320 forbids: the two causes get separate handlers wherever a
        site has something to SAY, and this one says nothing to anybody -
        it returns ``[]`` and the caller renders an empty history. Two
        handlers with the same empty body would be a distinction no
        reader could ever observe. The malformed-line case below is
        already skipped per line for the same reason.
        """
        try:
            raw = self.journal_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        entries: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                data = read_json(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if item_id and data.get("item_id") != item_id:
                continue
            entries.append(data)
        return entries

    # ---------------------------------------------------------------- pause

    @property
    def pause_path(self) -> Path:
        return control_file(self.root_dir, CONTROL_PAUSE)

    def pause_state(self) -> PauseState:
        ensure_control_state(self.root_dir)
        untrusted = control_untrusted_reason(self.root_dir)
        if untrusted is not None:
            return PauseState(paused=True, reason=untrusted)
        try:
            raw = self.pause_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # The ONLY read failure that means "not paused". Everything
            # else is a marker we could not read, and a broad `except
            # OSError` here made a PermissionError read as RUNNING -
            # exactly the fail-open this module claims not to do (#185 F7).
            return PauseState()
        except OSError as exc:
            return PauseState(
                paused=True,
                reason=f"unreadable pause marker: {exc}",
            )
        except UnicodeDecodeError as exc:
            # The same fail-closed direction as the clause above, for the
            # failure that clause never caught. ``UnicodeDecodeError`` is
            # a ``ValueError``, so until #320 one non-utf-8 byte in the
            # marker escaped this handler entirely - not fail-open, which
            # is what #185 F7 fixed, but no answer at all: the traceback
            # left the caller with neither PAUSED nor RUNNING.
            return PauseState(
                paused=True,
                reason=f"pause marker is not valid UTF-8: {exc}",
            )
        try:
            data = read_json(raw)
        except json.JSONDecodeError:
            # An unreadable pause marker means PAUSED. Failing open here
            # would resume unattended spending on the strength of a
            # corrupt file.
            return PauseState(paused=True, reason="unreadable pause marker")
        if not isinstance(data, dict):
            return PauseState(paused=True, reason="malformed pause marker")
        return PauseState.from_dict(data)

    def is_paused(self, now: datetime | None = None) -> bool:
        return self.pause_state().active(now)

    def pause(
        self,
        *,
        reason: str = "",
        actor: str = "",
        resume_after: str = "",
    ) -> PauseState:
        state = PauseState(
            paused=True,
            reason=reason,
            since=_iso(_utc_now()),
            resume_after=resume_after,
        )
        ensure_control_state(self.root_dir)
        path = self.pause_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with control_lock(self.root_dir):
            atomic_write(
                path,
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
        self._journal(
            JournalEntry(
                ts=state.since,
                item_id="",
                from_state="",
                to_state="paused",
                reason=reason,
                actor=actor,
                detail={"resume_after": resume_after},
            )
        )
        return state

    def resume(self, *, actor: str = "", detail: dict[str, Any] | None = None) -> PauseState:
        state = PauseState()
        ensure_control_state(self.root_dir)
        path = self.pause_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with control_lock(self.root_dir):
            atomic_write(
                path,
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
        self._journal(
            JournalEntry(
                ts=_iso(_utc_now()),
                item_id="",
                from_state="paused",
                to_state="running",
                reason="resumed",
                actor=actor,
                detail=dict(detail or {}),
            )
        )
        return state
