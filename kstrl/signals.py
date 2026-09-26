"""Runtime signal polling: observe and record, never enqueue (R8.8 slice 1, #155).

``ks signals poll`` fetches one tracker's issue list, classifies each
issue against an append-only ledger in the XDG control directory, and
writes a verdict: ``new_issue``, ``repeat`` or ``recurrence``, each
carrying a ``would_enqueue`` / ``would_watch`` / ``would_notify``
disposition. Nothing here queues anything: ``SignalsConfig`` has no
``enqueue`` field, this module imports nothing from ``kstrl.workqueue``,
and no code path here writes to the inbox or to ``events.jsonl``.

Two reasons put the boundary there. First, the spend a queued item
represents is real and the thresholds that would gate it are not: there
is no runtime history yet to replay a threshold against
(``docs/dark-factory-roadmap.md`` forbids exactly that gate). Second,
roadmap user decision 8 (queue directly, or human triage first) is the
owner's and is open; a ledger is the artifact both answers read.

Bugsink 2.6.0, run locally and captured (``tests/fixtures/signals/``),
serves ``/api/canonical/0/issues/?project=<id>`` with fifteen fields per
issue and 404s on both Sentry-shaped read paths. So there is one
adapter, named for its tracker: ``fetch_bugsink`` / ``normalise_bugsink``.
``Signal.tracker`` records the literal ``"bugsink"``; there is no
``tracker`` config key, because a key with exactly one legal value can
never differ from its default and so can never satisfy
``scripts/gen_docs.py``'s "setting a documented key changes the loaded
value" probe.

``release`` is ``None`` in every row this slice writes: the issue-list
endpoint carries no release, and recovering one costs one
``GET /api/canonical/0/events/<uuid>/`` per event per issue per poll,
measured and not paid here. There is no distinct-user count for the same
reason. ``None`` means "not collected"; an empty string would mean "the
tracker reported an empty release" and a reader could not tell the two
apart.

The storm figures (``poll_new_issues``, ``poll_max_events_on_a_new_issue``)
are computed once per poll and written onto every row that poll
produces. They gate nothing. A breaker that only counted distinct new
issues would be blind to one issue with five events from a single bad
source line, which the captured page proves happens; recording both
figures and gating on neither is the fix measured for.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl.appendio import append_records
from kstrl.atomicio import atomic_write_text
from kstrl.config import _parse_bool, load_toml_section, resolve_config_file
from kstrl.config_numbers import check_numbers
from kstrl.jsonread import read_json
from kstrl.statedir import CONTROL_SIGNALS, control_file, ensure_control_state

#: Every string copied out of a tracker payload is clipped to this many
#: characters before it reaches the ledger. A bound on the ledger's size,
#: stated once rather than assumed: this is NOT a sanitiser, and nothing
#: in this module puts a clipped (or unclipped) string in front of an
#: LLM.
MAX_TEXT_CHARS = 500


class SignalsError(RuntimeError):
    """A signals poll could not be completed."""


class SignalKind(StrEnum):
    """What a poll decided about one issue, against the ledger.

    The only spelling of this vocabulary: the ledger writer serialises
    ``str(member)`` and every reader coerces through this enum. A second
    string literal of a member value anywhere else is the #260 round-2
    defect (two definitions of a valid record; the weaker one is what
    the gate consults).
    """

    NEW_ISSUE = "new_issue"
    REPEAT = "repeat"
    RECURRENCE = "recurrence"


class Disposition(StrEnum):
    """What a poll would have done, if slice 2 acted on it. Nothing does."""

    WOULD_ENQUEUE = "would_enqueue"
    WOULD_WATCH = "would_watch"
    WOULD_NOTIFY = "would_notify"


@dataclass
class SignalsConfig:
    """``[signals]`` section. Off by default, one tracker, no secrets.

    ``enabled`` is checked by the ``ks signals poll`` CLI callback before
    it opens a socket or replays a ``--from-file`` page, refusing with
    exit 2 when it is false (#155 fix round A1; #452).

    ``token_env`` is the NAME of the environment variable holding the
    tracker's bearer token, read at call time by ``fetch_bugsink`` - the
    same shape ``LinearConfig.token_env`` uses and for the same reason:
    ``kstrl.toml`` is a tracked file gitleaks scans, so there is no
    ``token`` field here at all, checked by
    ``tests/test_signals_absence.py``.

    ``new_issue_events`` and ``repeat_growth_events`` are advisory
    thresholds that only ever choose a label
    (``would_enqueue``/``would_watch``/``would_notify``); nothing reads
    a disposition to decide what runs.
    """

    enabled: bool = False
    product: str = ""
    base_url: str = "http://127.0.0.1:8000"
    project_id: str = ""
    token_env: str = "KSTRL_SIGNALS_TOKEN"
    http_timeout: float = 10.0
    new_issue_events: int = 3
    repeat_growth_events: int = 10

    def __post_init__(self) -> None:
        if self.http_timeout <= 0:
            raise ValueError("SignalsConfig.http_timeout must be positive")
        if not self.token_env:
            raise ValueError("SignalsConfig.token_env must not be empty")

    @classmethod
    def from_env(cls) -> SignalsConfig:
        defaults = cls()
        enabled = os.environ.get("KSTRL_SIGNALS_ENABLED")
        return cls(
            enabled=defaults.enabled if enabled is None else _parse_bool(enabled),
            product=os.environ.get("KSTRL_SIGNALS_PRODUCT", defaults.product),
            base_url=os.environ.get("KSTRL_SIGNALS_BASE_URL", defaults.base_url),
            project_id=os.environ.get("KSTRL_SIGNALS_PROJECT_ID", defaults.project_id),
            token_env=os.environ.get("KSTRL_SIGNALS_TOKEN_ENV", defaults.token_env),
            http_timeout=float(os.environ.get("KSTRL_SIGNALS_HTTP_TIMEOUT", defaults.http_timeout)),
            new_issue_events=int(
                os.environ.get("KSTRL_SIGNALS_NEW_ISSUE_EVENTS", defaults.new_issue_events)
            ),
            repeat_growth_events=int(
                os.environ.get("KSTRL_SIGNALS_REPEAT_GROWTH_EVENTS", defaults.repeat_growth_events)
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> SignalsConfig:
        """Precedence: env > toml > defaults; reads ``[signals]``."""
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "signals")
        _overlay_toml_section(config, section)
        _overlay_env(config)
        config.__post_init__()
        return check_numbers(config)


def _overlay_toml_section(config: SignalsConfig, section: dict[str, Any]) -> None:
    if "enabled" in section:
        config.enabled = bool(section["enabled"])
    if "product" in section:
        config.product = str(section["product"])
    if "base_url" in section:
        config.base_url = str(section["base_url"])
    if "project_id" in section:
        config.project_id = str(section["project_id"])
    if "token_env" in section:
        config.token_env = str(section["token_env"])
    if "http_timeout" in section:
        config.http_timeout = float(section["http_timeout"])
    if "new_issue_events" in section:
        config.new_issue_events = int(section["new_issue_events"])
    if "repeat_growth_events" in section:
        config.repeat_growth_events = int(section["repeat_growth_events"])


def _overlay_env(config: SignalsConfig) -> None:
    if "KSTRL_SIGNALS_ENABLED" in os.environ:
        config.enabled = _parse_bool(os.environ["KSTRL_SIGNALS_ENABLED"])
    if "KSTRL_SIGNALS_PRODUCT" in os.environ:
        config.product = os.environ["KSTRL_SIGNALS_PRODUCT"]
    if "KSTRL_SIGNALS_BASE_URL" in os.environ:
        config.base_url = os.environ["KSTRL_SIGNALS_BASE_URL"]
    if "KSTRL_SIGNALS_PROJECT_ID" in os.environ:
        config.project_id = os.environ["KSTRL_SIGNALS_PROJECT_ID"]
    if "KSTRL_SIGNALS_TOKEN_ENV" in os.environ:
        config.token_env = os.environ["KSTRL_SIGNALS_TOKEN_ENV"]
    if "KSTRL_SIGNALS_HTTP_TIMEOUT" in os.environ:
        config.http_timeout = float(os.environ["KSTRL_SIGNALS_HTTP_TIMEOUT"])
    if "KSTRL_SIGNALS_NEW_ISSUE_EVENTS" in os.environ:
        config.new_issue_events = int(os.environ["KSTRL_SIGNALS_NEW_ISSUE_EVENTS"])
    if "KSTRL_SIGNALS_REPEAT_GROWTH_EVENTS" in os.environ:
        config.repeat_growth_events = int(os.environ["KSTRL_SIGNALS_REPEAT_GROWTH_EVENTS"])


@dataclass(frozen=True)
class Signal:
    """One issue, as of one poll. Every field is either measured present
    in the captured Bugsink payload or produced by kstrl itself."""

    schema_version: int
    poll_id: str
    observed_at: str
    product: str
    tracker: str
    issue_id: str
    friendly_id: str
    event_count: int
    resolved: bool
    first_seen: str
    last_seen: str
    error_type: str
    error_value: str
    transaction: str
    release: str | None
    kind: SignalKind
    disposition: Disposition
    poll_new_issues: int
    poll_max_events_on_a_new_issue: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "poll_id": self.poll_id,
            "observed_at": self.observed_at,
            "product": self.product,
            "tracker": self.tracker,
            "issue_id": self.issue_id,
            "friendly_id": self.friendly_id,
            "event_count": self.event_count,
            "resolved": self.resolved,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "error_type": self.error_type,
            "error_value": self.error_value,
            "transaction": self.transaction,
            "release": self.release,
            "kind": str(self.kind),
            "disposition": str(self.disposition),
            "poll_new_issues": self.poll_new_issues,
            "poll_max_events_on_a_new_issue": self.poll_max_events_on_a_new_issue,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal | None:
        """Rebuild one ledger row, or ``None`` for a line this reader
        cannot use.

        Tolerant by design, matching ``InboxItem.from_dict`` and
        ``QueueItem.from_dict`` (#155 fix round A2c): every field this
        module itself ever writes is present, but the ledger is
        append-only, so a line from an older schema or a manual edit can
        still be missing one. The old form subscripted every field and
        raised ``KeyError`` on the first one absent, which bricked both
        ``ks signals poll`` and ``ks signals ls`` permanently, because
        there was no way to write a corrected line over a bad one.

        ``issue_id`` is the identity key and ``kind``/``disposition`` are
        the row's whole reason for existing, so all three are required
        and an invalid or absent one drops the row. Every other field
        defaults the way an absent optional field always has.
        """
        issue_id = data.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id:
            return None
        try:
            kind = SignalKind(str(data["kind"]))
            disposition = Disposition(str(data["disposition"]))
        except (KeyError, ValueError):
            return None
        schema_version = data.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            schema_version = 1
        event_count = data.get("event_count", 0)
        if not isinstance(event_count, int) or isinstance(event_count, bool):
            event_count = 0
        poll_new_issues = data.get("poll_new_issues", 0)
        if not isinstance(poll_new_issues, int) or isinstance(poll_new_issues, bool):
            poll_new_issues = 0
        poll_max_events_on_a_new_issue = data.get("poll_max_events_on_a_new_issue", 0)
        if not isinstance(poll_max_events_on_a_new_issue, int) or isinstance(
            poll_max_events_on_a_new_issue, bool
        ):
            poll_max_events_on_a_new_issue = 0
        release = data.get("release")
        return cls(
            schema_version=schema_version,
            poll_id=str(data.get("poll_id", "")),
            observed_at=str(data.get("observed_at", "")),
            product=str(data.get("product", "")),
            tracker=str(data.get("tracker", "")),
            issue_id=issue_id,
            friendly_id=str(data.get("friendly_id", "")),
            event_count=event_count,
            resolved=bool(data.get("resolved", False)),
            first_seen=str(data.get("first_seen", "")),
            last_seen=str(data.get("last_seen", "")),
            error_type=str(data.get("error_type", "")),
            error_value=str(data.get("error_value", "")),
            transaction=str(data.get("transaction", "")),
            release=None if release is None else str(release),
            kind=kind,
            disposition=disposition,
            poll_new_issues=poll_new_issues,
            poll_max_events_on_a_new_issue=poll_max_events_on_a_new_issue,
        )


@dataclass(frozen=True)
class PollReport:
    """What one ``poll()`` call did, for the CLI to print."""

    poll_id: str
    observed_at: str
    signals: tuple[Signal, ...]
    dropped_rows: int
    poll_new_issues: int
    poll_max_events_on_a_new_issue: int


def _clip(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# --- the pure half: normalise, classify --------------------------------


def normalise_bugsink(row: dict[str, Any], *, product: str) -> Signal | None:
    """One Bugsink canonical-API row, or ``None`` for a row this adapter
    cannot use.

    Returns ``None`` (never a defaulted ``Signal``) for a row with no
    ``id`` - a record with no dedup key looks new on every poll - and for
    a row with no ``digested_event_count``: a missing count is a
    refusal, never coerced to 0, because a coerced zero looks like a
    measurement.

    ``poll_id``, ``observed_at``, ``kind``, ``disposition`` and the two
    poll-level storm figures are placeholders here; :func:`poll` fills
    them in once the whole batch has been classified.
    """
    issue_id = row.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        return None
    if "digested_event_count" not in row:
        return None
    event_count = row["digested_event_count"]
    if not isinstance(event_count, int) or isinstance(event_count, bool):
        return None
    return Signal(
        schema_version=1,
        poll_id="",
        observed_at="",
        product=product,
        tracker="bugsink",
        issue_id=issue_id,
        friendly_id=_clip(str(row.get("friendly_id", ""))),
        event_count=event_count,
        resolved=bool(row.get("is_resolved", False)),
        first_seen=str(row.get("first_seen", "")),
        last_seen=str(row.get("last_seen", "")),
        error_type=_clip(str(row.get("calculated_type", ""))),
        error_value=_clip(str(row.get("calculated_value", ""))),
        transaction=_clip(str(row.get("transaction", ""))),
        release=None,
        kind=SignalKind.NEW_ISSUE,
        disposition=Disposition.WOULD_WATCH,
        poll_new_issues=0,
        poll_max_events_on_a_new_issue=0,
    )


def classify(
    signal: Signal,
    previous: Signal | None,
    *,
    new_issue_events: int,
    repeat_growth_events: int,
) -> tuple[SignalKind, Disposition]:
    """Pure: no clock, no disk, thresholds passed in rather than read off
    a module constant.

    1. No previous record for this key -> ``NEW_ISSUE``; ``WOULD_ENQUEUE``
       when the event count already clears the threshold, else
       ``WOULD_WATCH``.
    2. The previous record was resolved AND this payload's ``last_seen``
       moved past it (plain string compare on the ISO-8601 UTC values,
       which sort lexicographically) -> ``RECURRENCE``, ``WOULD_ENQUEUE``.
       An unchanged ``last_seen`` on a resolved key is a re-seen repeat,
       not a recurrence - two polls cannot tell that apart from a
       first-seen recurrence, which is why the tests drive three.
    3. Otherwise -> ``REPEAT``; ``WOULD_NOTIFY`` when the event count grew
       by at least the threshold since the previous record, else
       ``WOULD_WATCH``.
    """
    if previous is None:
        kind = SignalKind.NEW_ISSUE
        disposition = (
            Disposition.WOULD_ENQUEUE
            if signal.event_count >= new_issue_events
            else Disposition.WOULD_WATCH
        )
        return kind, disposition
    if previous.resolved and signal.last_seen > previous.last_seen:
        return SignalKind.RECURRENCE, Disposition.WOULD_ENQUEUE
    growth = signal.event_count - previous.event_count
    disposition = (
        Disposition.WOULD_NOTIFY if growth >= repeat_growth_events else Disposition.WOULD_WATCH
    )
    return SignalKind.REPEAT, disposition


# --- the ledger half: read, index, append -------------------------------


@dataclass(frozen=True)
class LedgerRead:
    """One pass over the signal ledger: the readable rows, and a count
    of the ones this reader could not use (#155 fix round A2c).

    The count, not just the tolerance, is the fix: a torn tail or a
    valid-JSON-but-incomplete line is a known shape, same as
    ``Inbox.scan``'s ``skipped_lines``, and a caller that silently
    dropped it would report a ledger shorter than what was actually
    written with nothing to show for the difference.
    """

    signals: tuple[Signal, ...] = ()
    dropped: int = 0


def read_ledger(path: Path) -> LedgerRead:
    """The whole ledger, oldest first, plus how many lines were unusable.

    Raises rather than reading empty. A missing file is the one
    legitimate empty read, checked with ``path.exists()``. Everything
    else - ``OSError`` from ``read_bytes()``, ``UnicodeDecodeError`` (a
    ``ValueError``) from ``decode`` - reaches the caller: a read failure
    that returned an empty ledger would make every known key look new
    again and inflate the very counts this slice exists to collect. Both
    calls are OUTSIDE any ``try``, which is stronger than catching them
    and re-raising: no later widening of a guard here can swallow
    either.

    Two DIFFERENT unusable shapes are both counted in ``dropped``: a
    torn tail line (``json.JSONDecodeError``, matching ``Inbox.scan``)
    and valid JSON that ``Signal.from_dict`` cannot use - missing or
    unusable required fields. Neither raises; a torn or incomplete
    ledger line is a known, countable shape, not an unreadable file.
    """
    if not path.exists():
        return LedgerRead()
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    records: list[Signal] = []
    dropped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = read_json(line)
        except json.JSONDecodeError:
            dropped += 1
            continue
        if not isinstance(record, dict):
            dropped += 1
            continue
        signal = Signal.from_dict(record)
        if signal is None:
            dropped += 1
            continue
        records.append(signal)
    return LedgerRead(signals=tuple(records), dropped=dropped)


def append_signals(path: Path, signals: Sequence[Signal]) -> None:
    """Append ``signals`` to the ledger under an exclusive lock.

    ``lock=True``: unlike every other ``append_records`` caller but the
    evolution journal, this file has more than one writer - a
    cron-driven ``ks signals poll`` and an operator's can interleave -
    and the whole product of this slice is a count that must not be
    short. ``ensure_ascii`` stays at its default so this module never
    becomes the source of non-ASCII bytes on a tracker's own text (#291).
    """
    payload = "".join(json.dumps(s.to_dict()) + "\n" for s in signals)
    append_records(path, payload, repair="", lock=True)


# --- file replay: --from-file / --capture -------------------------------


def read_page_text(path: Path) -> str:
    """The exact text of a captured or replayed tracker page."""
    raw = path.read_bytes()
    return raw.decode("utf-8")


def read_page_file(path: Path) -> Any:
    """A captured or replayed tracker page, parsed."""
    return read_json(read_page_text(path))


def write_capture(path: Path, text: str) -> None:
    """Write a fetched page's raw text to ``path``, for later replay."""
    atomic_write_text(path, text)


def _rows_from_document(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        raise SignalsError("signals: response is not a JSON object")
    results = document.get("results")
    if not isinstance(results, list):
        raise SignalsError("signals: response 'results' is not a list")
    return results


# --- the transport -------------------------------------------------------


def _fetch_bugsink_text(config: SignalsConfig) -> str:
    token = os.environ.get(config.token_env)
    if not token:
        raise SignalsError(
            f"signals: env var {config.token_env} is unset or empty; cannot authenticate"
        )
    url = f"{config.base_url}/api/canonical/0/issues/?project={config.project_id}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "kstrl-factory",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.http_timeout) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as exc:
        # Names the STATUS, never the token (linear.py:71's contract).
        raise SignalsError(f"signals: fetch failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignalsError(f"signals: transport failure: {exc}") from exc
    return raw.decode("utf-8")


def fetch_bugsink(config: SignalsConfig) -> list[dict[str, Any]]:
    """One page of Bugsink's canonical issue list. Never returns ``[]``
    for a failure - every transport or shape failure is a
    :class:`SignalsError`, because the product of this slice is a count."""
    text = _fetch_bugsink_text(config)
    return _rows_from_document(read_json(text))


# --- poll: fan-out, classify, append, report ------------------------------


def _rows_for_poll(
    config: SignalsConfig,
    *,
    from_file: Path | None,
    capture: Path | None,
) -> list[dict[str, Any]]:
    text = _fetch_bugsink_text(config) if from_file is None else read_page_text(from_file)
    if capture is not None:
        write_capture(capture, text)
    return _rows_from_document(read_json(text))


def _classify_rows(
    rows: Sequence[dict[str, Any]],
    previous_by_id: dict[str, Signal],
    config: SignalsConfig,
) -> tuple[list[tuple[Signal, SignalKind, Disposition]], int]:
    dropped_rows = 0
    classified: list[tuple[Signal, SignalKind, Disposition]] = []
    for row in rows:
        signal = normalise_bugsink(row, product=config.product)
        if signal is None:
            dropped_rows += 1
            continue
        previous = previous_by_id.get(signal.issue_id)
        kind, disposition = classify(
            signal,
            previous,
            new_issue_events=config.new_issue_events,
            repeat_growth_events=config.repeat_growth_events,
        )
        classified.append((signal, kind, disposition))
    return classified, dropped_rows


def _storm_figures(classified: Sequence[tuple[Signal, SignalKind, Disposition]]) -> tuple[int, int]:
    """Both figures the minimal lane's storm verdict hid: how many issues
    in this poll are new, and the largest event count among them.

    Measured on the captured page: one issue with five events from one
    source line, and five issues with one event each from five different
    lines, are told apart here and blind to a count of distinct new
    issues alone.
    """
    counts = [signal.event_count for signal, kind, _ in classified if kind is SignalKind.NEW_ISSUE]
    if not counts:
        return 0, 0
    return len(counts), max(counts)


def _index_ledger(records: Sequence[Signal]) -> dict[str, Signal]:
    """The latest ledger row per key. Later rows overwrite earlier ones,
    so the ledger's own append order decides which record classify()
    sees - the FIRST matching row winning would be the wrong rule
    (measured: it cannot tell a resolved key re-seen unchanged from one
    seen again after moving)."""
    index: dict[str, Signal] = {}
    for record in records:
        index[record.issue_id] = record
    return index


def _finalize(
    classified: Sequence[tuple[Signal, SignalKind, Disposition]],
    *,
    poll_id: str,
    observed_at: str,
    poll_new_issues: int,
    poll_max_events_on_a_new_issue: int,
) -> tuple[Signal, ...]:
    return tuple(
        replace(
            signal,
            poll_id=poll_id,
            observed_at=observed_at,
            kind=kind,
            disposition=disposition,
            poll_new_issues=poll_new_issues,
            poll_max_events_on_a_new_issue=poll_max_events_on_a_new_issue,
        )
        for signal, kind, disposition in classified
    )


def poll(
    root_dir: Path,
    config: SignalsConfig,
    *,
    now: Callable[[], datetime] | None = None,
    from_file: Path | None = None,
    capture: Path | None = None,
) -> PollReport:
    """Fetch, classify against the ledger, append, report. Never queues."""
    clock = _utc_now if now is None else now
    moment = clock()
    rows = _rows_for_poll(config, from_file=from_file, capture=capture)

    ensure_control_state(root_dir)
    ledger_path = control_file(root_dir, CONTROL_SIGNALS)
    ledger = read_ledger(ledger_path)
    previous_by_id = _index_ledger(ledger.signals)

    classified, page_dropped = _classify_rows(rows, previous_by_id, config)
    poll_new_issues, poll_max_events_on_a_new_issue = _storm_figures(classified)
    poll_id = uuid.uuid4().hex
    observed_at = _iso(moment)
    finalized = _finalize(
        classified,
        poll_id=poll_id,
        observed_at=observed_at,
        poll_new_issues=poll_new_issues,
        poll_max_events_on_a_new_issue=poll_max_events_on_a_new_issue,
    )

    append_signals(ledger_path, finalized)

    return PollReport(
        poll_id=poll_id,
        observed_at=observed_at,
        signals=finalized,
        dropped_rows=page_dropped + ledger.dropped,
        poll_new_issues=poll_new_issues,
        poll_max_events_on_a_new_issue=poll_max_events_on_a_new_issue,
    )
