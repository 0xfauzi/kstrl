"""The records of the work queue and their dict form.

This module holds the queue errors, the item states and the legal
transitions between them, the item ids, and the dataclasses that are
written to ``meta.json`` and to the journal: ``QueueItem``,
``QueueConfig``, ``PauseState`` and ``JournalEntry``. It does no file
access. The directory layout, the lock and the transitions are in
``kstrl.workqueue``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl.config_numbers import check_numbers

QUEUE_SCHEMA_VERSION = 1


class QueueError(RuntimeError):
    """A queue operation could not be completed."""


class QueueLockedError(QueueError):
    """Another process holds the queue mutex."""


class QueueBudgetExhausted(QueueError):
    """An item was asked to run with no attempts left.

    Raised by :meth:`Queue.start` - the substrate's own enforcement of
    ``max_attempts``, independent of whatever the caller believes. The
    daemon catches this and poisons the item; a caller that ignores it
    leaves the item leased for the reaper, never running.
    """


#: Names a queue path component may never take. ``.`` and ``..`` are the
#: traversal primitives; the empty string collapses a join silently.
_UNSAFE_NAMES = frozenset({"", ".", ".."})


def is_safe_component(name: str) -> bool:
    """Whether ``name`` is a single path component safe to join.

    Rejects separators, traversal, empty names, NUL, and leading dots.
    Every string that becomes part of a queue path passes through here,
    because two of them (``item_id`` and ``spec_filename``) are read back
    from a sidecar an operator or a remote adapter can write. Review
    #185 F1/F2 demonstrated both: an ``item_id`` of ``../../../outside``
    made ``remove()`` delete an unrelated directory, and a
    ``spec_filename`` of ``../../../../escaped.md`` wrote outside the
    queue while still publishing a normal-looking item.
    """
    if name in _UNSAFE_NAMES:
        return False
    if name.startswith("."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return Path(name).name == name


class ItemState(StrEnum):
    """Where an item is. Also the on-disk directory name.

    ``LEASED`` and ``RUNNING`` are deliberately distinct even though a
    worker passes through both in quick succession: the reaper (PR 2)
    treats them differently. A leased item whose owner died spent
    nothing, so it returns to ``QUEUED`` for free. A running item whose
    owner died spent real money and needs its failure classified before
    anything decides to spend more.
    """

    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    POISON = "poison"
    #: #465: the run parked work at the merge gate. Not a failure and not
    #: a finish; nothing moves the item on automatically.
    AWAITING_APPROVAL = "awaiting_approval"
    #: #644: the architect escalated a question only the owner can answer.
    #: Not a failure and not a poison; `ks queue answer` replaces the spec
    #: and sends the item back to queued.
    AWAITING_ANSWER = "awaiting_answer"


#: Every state directory, created eagerly so a scan never has to
#: distinguish "no such directory" from "no items in it".
ALL_STATES: tuple[ItemState, ...] = tuple(ItemState)

#: Legal moves. Anything absent raises rather than being silently
#: allowed: an illegal transition means a caller's state machine is
#: wrong, and in this module a wrong state machine spends money.
_LEGAL_TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.QUEUED: frozenset({ItemState.LEASED, ItemState.POISON}),
    # A lease can be dropped back to queued by the reaper (owner died
    # before spending) or fail outright if the worker could not start.
    ItemState.LEASED: frozenset(
        {
            ItemState.QUEUED,
            ItemState.RUNNING,
            ItemState.FAILED,
            ItemState.POISON,
        }
    ),
    ItemState.RUNNING: frozenset(
        {
            ItemState.DONE,
            ItemState.FAILED,
            ItemState.POISON,
            ItemState.AWAITING_APPROVAL,
            ItemState.AWAITING_ANSWER,
        }
    ),
    # Terminal-ish: a human (or the retry policy) can requeue, and a
    # failed item that exhausts its attempts is poisoned.
    ItemState.FAILED: frozenset({ItemState.QUEUED, ItemState.POISON}),
    ItemState.POISON: frozenset({ItemState.QUEUED}),
    ItemState.DONE: frozenset(),
    # #464: the run `ks inbox approve` or `ks inbox reject` starts settles
    # the item: done when it exits 0, poison otherwise. A run that parks
    # the next component leaves it here (``Queue.relink_run``).
    ItemState.AWAITING_APPROVAL: frozenset({ItemState.DONE, ItemState.POISON}),
    # #644: only `ks queue answer` moves an item on, and only back to queued.
    ItemState.AWAITING_ANSWER: frozenset({ItemState.QUEUED}),
}


class MergeDisposition(StrEnum):
    """Whether a finished item's PR merges without a human.

    ``STOP_AT_PR`` is the default for everything, and R8.6 requires it be
    the default for remote-sourced items specifically: continuous intake
    must not silently delete the human merge gate. ``AUTO_MERGE`` is
    per-item opt-in AND ladder-gated - the R8.2 flag bundle
    (``auto_merge_when_green``) can withhold it, and this field can only
    ever request it, never grant it.
    """

    STOP_AT_PR = "stop_at_pr"
    AUTO_MERGE = "auto_merge"


class ItemSource(StrEnum):
    """Where the item came from. ``source_ref`` names the specific one."""

    LOCAL = "local"
    GITHUB = "github"
    LINEAR = "linear"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def mint_item_id() -> str:
    """``q-YYYYMMDD-HHMMSS.ffffff-<nonce>``.

    Shaped like :func:`kstrl.runid.mint_run_id` and for the same reason:
    the microsecond stamp makes same-second ids order deterministically
    by creation time, which is what gives the queue FIFO ordering within
    a priority band, and the nonce guards same-microsecond collisions.
    """
    stamp = _utc_now().strftime("%Y%m%d-%H%M%S.%f")
    return f"q-{stamp}-{secrets.token_hex(3)}"


def short_item_id(item_id: str) -> str:
    """``q-20260923-205005.895694-793181`` -> ``q-793181``.

    The form a one-line surface shows where the full id does not fit:
    the TUI's active row and run header, and the titles of the inbox
    items serve files. :meth:`Queue.get` accepts it, so every
    ``ks queue`` command takes what those surfaces show (#706). Every
    other surface prints the full id.
    """
    tail = item_id.rsplit("-", 1)[-1]
    return f"q-{tail}" if tail and tail != item_id else item_id


def _as_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def _as_str_tuple(data: dict[str, Any], key: str) -> tuple[str, ...]:
    """Decode a list of strings, or fall back to ``()`` whole.

    One tolerance rule: a value that is a list where every entry is a
    string decodes as-is; anything else - not a list, or a list with one
    non-string entry - decodes to the field's default.
    """
    value = data.get(key)
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return tuple(value)
    return ()


@dataclass
class QueueItem:
    """One unit of intake: a spec plus everything decided about it.

    Not frozen: an item's ``state``, ``attempts``, and lease fields are
    exactly what a transition mutates. The frozen container in this
    module is :class:`QueueConfig`, which genuinely never changes after
    load.
    """

    item_id: str
    title: str
    spec_filename: str
    state: ItemState = ItemState.QUEUED
    #: Higher runs first; ties break by ``item_id`` (creation order).
    priority: int = 0
    created_at: str = ""
    updated_at: str = ""
    #: Execution attempts CHARGED so far - incremented before the rename
    #: into ``running/``, never after. See the module docstring.
    attempts: int = 0
    max_attempts: int = 3
    merge_disposition: MergeDisposition = MergeDisposition.STOP_AT_PR
    source: ItemSource = ItemSource.LOCAL
    #: Identifies the specific origin, e.g. ``0xfauzi/kstrl#153``. The
    #: processed-ids ledger (PR 3) dedupes on this.
    source_ref: str = ""
    #: Forward-compatibility for a global ``~/.kstrl/queue`` (roadmap
    #: open question 3). Empty means "the repo this queue lives in".
    #: Carried from day one so moving to a global queue is a config
    #: change rather than a migration of on-disk items.
    target_repo: str = ""
    project_name: str = ""
    lease_pid: int = 0
    lease_host: str = ""
    lease_expires_at: str = ""
    last_error: str = ""
    last_run_id: str = ""
    #: Why this item may never be retried automatically. Set only on the
    #: transition into ``poison/``.
    poison_reason: str = ""
    #: Earliest time this item may be claimed again (retry backoff, R8.6
    #: PR 2). Empty means "now". A payload written before this field
    #: existed decodes to empty, i.e. immediately ready.
    not_before: str = ""
    #: PR URLs the last factory run recorded for this item, read out of
    #: the run's manifest after it finished. Data on an existing record,
    #: not a state: nothing in the queue branches on it. An item written
    #: before this field existed decodes to (), which is also what a run
    #: that opened no PR leaves.
    pr_urls: tuple[str, ...] = ()
    #: Whether the run gets `ks factory --design-acceptance --bug-report`
    #: (#654: an issue with the `bug` label). Only a JSON `true` decodes as on, so an
    #: item written before this field existed runs as it did.
    design_acceptance: bool = False
    schema_version: int = QUEUE_SCHEMA_VERSION

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def ready_at(self, now: datetime | None = None) -> bool:
        """Whether the retry backoff has elapsed.

        An UNPARSEABLE ``not_before`` counts as not-ready, the opposite of
        the lease-expiry default: there the fail-safe direction is to
        reclaim a stuck item, here it is to hold off spending. Both
        choices err away from launching a run.
        """
        if not self.not_before:
            return True
        deadline = _parse_iso(self.not_before)
        if deadline is None:
            return False
        return (now or _utc_now()) >= deadline

    @property
    def sort_key(self) -> tuple[int, str]:
        """Highest priority first, then oldest first."""
        return (-self.priority, self.item_id)

    def lease_expired(self, now: datetime | None = None) -> bool:
        """Whether this item's lease has lapsed.

        A missing or unparseable expiry counts as EXPIRED. An item
        holding a lease nobody can read is exactly the sleep/crash case
        the reaper exists for; treating it as live would wedge the
        queue forever.
        """
        expires = _parse_iso(self.lease_expires_at)
        if expires is None:
            return True
        return (now or _utc_now()) >= expires

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "title": self.title,
            "spec_filename": self.spec_filename,
            "state": str(self.state),
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "merge_disposition": str(self.merge_disposition),
            "source": str(self.source),
            "source_ref": self.source_ref,
            "target_repo": self.target_repo,
            "project_name": self.project_name,
            "lease_pid": self.lease_pid,
            "lease_host": self.lease_host,
            "lease_expires_at": self.lease_expires_at,
            "last_error": self.last_error,
            "last_run_id": self.last_run_id,
            "poison_reason": self.poison_reason,
            "not_before": self.not_before,
            "pr_urls": list(self.pr_urls),
            "design_acceptance": self.design_acceptance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueItem | None:
        """Decode a sidecar, tolerating fields written by older versions.

        Returns None only when the item has no identity or no spec -
        without those there is nothing to run. Every other malformed
        field falls back to its default, because dropping a whole unit
        of work over an unreadable priority would lose real work.
        """
        item_id = _as_str(data, "item_id")
        spec_filename = _as_str(data, "spec_filename")
        if not item_id or not spec_filename:
            return None
        # Both become path components. A sidecar is operator-writable and
        # (from PR 3) remote-adapter-writable, so it is untrusted input:
        # reject traversal here rather than at each join site (#185 F2).
        if not is_safe_component(spec_filename):
            return None

        raw_state = _as_str(data, "state", str(ItemState.QUEUED))
        try:
            state = ItemState(raw_state)
        except ValueError:
            state = ItemState.QUEUED

        raw_disposition = _as_str(
            data,
            "merge_disposition",
            str(MergeDisposition.STOP_AT_PR),
        )
        try:
            disposition = MergeDisposition(raw_disposition)
        except ValueError:
            # An unreadable disposition falls back to the gated value,
            # never to auto-merge: a corrupt field must not be able to
            # grant a permission nobody asked for.
            disposition = MergeDisposition.STOP_AT_PR

        raw_source = _as_str(data, "source", str(ItemSource.LOCAL))
        try:
            source = ItemSource(raw_source)
        except ValueError:
            source = ItemSource.LOCAL

        return cls(
            item_id=item_id,
            title=_as_str(data, "title") or item_id,
            spec_filename=spec_filename,
            state=state,
            priority=_as_int(data, "priority", 0),
            created_at=_as_str(data, "created_at"),
            updated_at=_as_str(data, "updated_at"),
            attempts=_as_int(data, "attempts", 0),
            max_attempts=_as_int(data, "max_attempts", 3),
            merge_disposition=disposition,
            source=source,
            source_ref=_as_str(data, "source_ref"),
            target_repo=_as_str(data, "target_repo"),
            project_name=_as_str(data, "project_name"),
            lease_pid=_as_int(data, "lease_pid", 0),
            lease_host=_as_str(data, "lease_host"),
            lease_expires_at=_as_str(data, "lease_expires_at"),
            last_error=_as_str(data, "last_error"),
            last_run_id=_as_str(data, "last_run_id"),
            poison_reason=_as_str(data, "poison_reason"),
            not_before=_as_str(data, "not_before"),
            pr_urls=_as_str_tuple(data, "pr_urls"),
            design_acceptance=data.get("design_acceptance") is True,
            schema_version=_as_int(
                data,
                "schema_version",
                QUEUE_SCHEMA_VERSION,
            ),
        )


@dataclass(frozen=True)
class QueueConfig:
    """``[queue]`` config.

    Only the fields the substrate itself needs. The daemon's knobs
    (poll interval, ``daily_budget_usd``, the poison breaker) land with
    ``ks serve`` in PR 2 rather than being declared here unused.
    """

    #: Execution attempts per item before it is poisoned. Bounds the
    #: retry loop even when the classifier is wrong.
    max_attempts: int = 3
    #: How long a claim stays valid without a heartbeat. The reaper
    #: recovers anything past this; it is the sleep/crash recovery knob.
    lease_ttl_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise QueueError(f"queue.max_attempts must be >= 1, got {self.max_attempts}")
        if self.lease_ttl_seconds <= 0:
            raise QueueError(f"queue.lease_ttl_seconds must be > 0, got {self.lease_ttl_seconds}")

    @classmethod
    def from_env(cls) -> QueueConfig:
        defaults = cls()
        attempts = os.environ.get("KSTRL_QUEUE_MAX_ATTEMPTS")
        ttl = os.environ.get("KSTRL_QUEUE_LEASE_TTL")
        return cls(
            max_attempts=(defaults.max_attempts if attempts is None else int(attempts)),
            lease_ttl_seconds=(defaults.lease_ttl_seconds if ttl is None else float(ttl)),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> QueueConfig:
        """Precedence: env > toml > defaults; reads ``[queue]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "queue")
        defaults = cls()
        max_attempts = (
            int(section["max_attempts"]) if "max_attempts" in section else defaults.max_attempts
        )
        lease_ttl_seconds = (
            float(section["lease_ttl_seconds"])
            if "lease_ttl_seconds" in section
            else defaults.lease_ttl_seconds
        )
        if "KSTRL_QUEUE_MAX_ATTEMPTS" in os.environ:
            max_attempts = int(os.environ["KSTRL_QUEUE_MAX_ATTEMPTS"])
        if "KSTRL_QUEUE_LEASE_TTL" in os.environ:
            lease_ttl_seconds = float(os.environ["KSTRL_QUEUE_LEASE_TTL"])
        return check_numbers(
            cls(
                max_attempts=max_attempts,
                lease_ttl_seconds=lease_ttl_seconds,
            )
        )


@dataclass(frozen=True)
class PauseState:
    """Whether intake is paused, and until when.

    ``resume_after`` is what makes the R8.6 daily-budget stop
    self-clearing: the budget pause sets tomorrow's local midnight and
    the queue admits work again on its own, so a Friday-night budget hit
    does not mean a dead queue all weekend.
    """

    paused: bool = False
    reason: str = ""
    since: str = ""
    resume_after: str = ""

    def active(self, now: datetime | None = None) -> bool:
        """Paused right now, accounting for a lapsed ``resume_after``."""
        if not self.paused:
            return False
        deadline = _parse_iso(self.resume_after)
        if deadline is None:
            return True
        return (now or _utc_now()) < deadline

    def to_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "reason": self.reason,
            "since": self.since,
            "resume_after": self.resume_after,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PauseState:
        return cls(
            paused=bool(data.get("paused", False)),
            reason=_as_str(data, "reason"),
            since=_as_str(data, "since"),
            resume_after=_as_str(data, "resume_after"),
        )


@dataclass
class JournalEntry:
    """One recorded transition. The queue's audit trail."""

    ts: str
    item_id: str
    from_state: str
    to_state: str
    reason: str = ""
    actor: str = ""
    attempts: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "item_id": self.item_id,
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "actor": self.actor,
            "attempts": self.attempts,
            "detail": self.detail,
        }
