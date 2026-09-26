"""The CI state of each commit a kstrl merge produced (#553).

#442 records the commit each merge produced (``Component.merge_sha``),
and nothing recorded whether CI passed on it, so no surface could say
whether main is green after a run. ``ks ci poll`` reads the checks on
every recorded merge commit through ``gh`` and appends one line per
commit to a ledger in the control directory. :func:`read_ci_ledger` is
the reader any other surface calls; it makes no network call.

Four states and no fifth. ``unknown`` is every reading kstrl could not
make: ``gh`` missing, unauthenticated, timed out or exiting non-zero; a
reply that is not JSON or not the shape asked for; GitHub reporting
errors, no repository, no commit, an object that is not a commit, or no
checks; a check list longer than the one page read; and any single
entry outside the vocabulary below. None of those is ever ``passed``.
Every entry is validated before any entry is counted, and one entry
that cannot be read makes the whole reading unknown, because the unread
entry could be the failing one.

One GraphQL call per commit rather than the two REST lists. GitHub keeps
check runs (Actions and apps) and commit statuses (older integrations)
in two systems, and ``statusCheckRollup.contexts`` returns both in one
list, so a failing status beside passing check runs is seen. The
rollup's own ``state`` is not requested: kstrl folds the entries with
the tables below, so there is one definition of passed, not two. REST
``/commits/<sha>/status`` is not read because it answers
``"state": "pending"`` for a commit with no statuses at all (measured,
``tests/fixtures/ci/PROVENANCE.md``).

Every reading carries the sha and ``observed_at`` (UTC, the format of
``Component.completed_at``), so a reader can tell how old a ``running``
is. The ledger is append-only and the newest line per sha wins.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl.appendio import append_records
from kstrl.intake_github import run_gh
from kstrl.jsonread import read_json
from kstrl.pr_state import GH_POLL_TIMEOUT
from kstrl.statedir import CONTROL_CI_CHECKS, control_file, ensure_control_state

#: Every reason is clipped to this many characters before it reaches the
#: ledger: a bound on the ledger's size, not a sanitiser.
MAX_TEXT_CHARS = 500

CI_SCHEMA_VERSION = 1

#: The one query. Its text is also the query the fixtures under
#: ``tests/fixtures/ci/`` were captured with.
CI_QUERY = (
    "query($owner: String!, $repo: String!, $oid: GitObjectID!) {"
    " repository(owner: $owner, name: $repo) { object(oid: $oid) { ... on Commit {"
    " statusCheckRollup { contexts(first: 100) { totalCount nodes { __typename"
    " ... on CheckRun { name status conclusion }"
    " ... on StatusContext { context state } } } } } } } }"
)

_SHA = re.compile(r"[0-9a-f]{40}")


class CiState(StrEnum):
    """The only spelling of this vocabulary: the ledger writes
    ``str(member)`` and every reader coerces through this enum."""

    PASSED = "passed"
    FAILED = "failed"
    RUNNING = "running"
    UNKNOWN = "unknown"


#: GitHub's CheckStatusState values for a check run that has not finished.
_CHECK_RUN_RUNNING = frozenset({"REQUESTED", "QUEUED", "IN_PROGRESS", "WAITING", "PENDING"})

#: GitHub's CheckConclusionState, for a COMPLETED check run. Closed: a
#: value not here makes the reading unknown.
_CHECK_RUN_CONCLUSIONS: dict[str, CiState] = {
    "SUCCESS": CiState.PASSED,
    "NEUTRAL": CiState.PASSED,
    "SKIPPED": CiState.PASSED,
    "FAILURE": CiState.FAILED,
    "TIMED_OUT": CiState.FAILED,
    "CANCELLED": CiState.FAILED,
    "ACTION_REQUIRED": CiState.FAILED,
    "STARTUP_FAILURE": CiState.FAILED,
    "STALE": CiState.FAILED,
}

#: GitHub's StatusState, for a commit status. Closed, as above.
_STATUS_CONTEXT_STATES: dict[str, CiState] = {
    "SUCCESS": CiState.PASSED,
    "FAILURE": CiState.FAILED,
    "ERROR": CiState.FAILED,
    "PENDING": CiState.RUNNING,
    "EXPECTED": CiState.RUNNING,
}


class _Unreadable(Exception):
    """A reply, or one entry of it, this module cannot read. Internal:
    :func:`classify_reply` turns it into an ``unknown`` reading."""


@dataclass(frozen=True)
class CiReading:
    """What one read of one commit found."""

    sha: str
    state: CiState
    #: Why this state, in words: the failing or running check names, the
    #: count that passed, or what could not be read. Never "".
    reason: str
    observed_at: str
    #: How many check entries the reading counted; 0 when it counted none.
    checks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CI_SCHEMA_VERSION,
            "sha": self.sha,
            "state": str(self.state),
            "reason": self.reason,
            "observed_at": self.observed_at,
            "checks": self.checks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CiReading | None:
        """One ledger line, or ``None`` for a line this reader cannot use.

        ``sha``, ``state`` and ``observed_at`` are required: a reading
        with no commit, no state or no time cannot be shown or judged
        stale. The others default.
        """
        sha = data.get("sha")
        state = data.get("state")
        observed_at = data.get("observed_at")
        if not isinstance(sha, str) or not sha:
            return None
        if not isinstance(observed_at, str) or not observed_at:
            return None
        if not isinstance(state, str):
            return None
        try:
            parsed = CiState(state)
        except ValueError:
            return None
        checks = data.get("checks", 0)
        if not isinstance(checks, int) or isinstance(checks, bool):
            checks = 0
        return cls(
            sha=sha,
            state=parsed,
            reason=str(data.get("reason", "")),
            observed_at=observed_at,
            checks=checks,
        )


@dataclass(frozen=True)
class CiLedger:
    """One pass over the ledger: the usable lines, oldest first, and a
    count of the lines this reader could not use."""

    readings: tuple[CiReading, ...] = ()
    dropped: int = 0

    def latest(self, sha: str) -> CiReading | None:
        """The newest reading of ``sha``, or ``None`` when it was never
        read. A surface shows ``None`` as unknown, never as passed."""
        for reading in reversed(self.readings):
            if reading.sha == sha:
                return reading
        return None


def _clip(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


# --- the pure half: validate every entry, then fold ----------------------


def _field(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict):
        raise _Unreadable(f"{where} is not an object")
    if key not in mapping:
        raise _Unreadable(f"{where} has no {key!r}")
    return mapping[key]


def _text(mapping: Any, key: str, where: str) -> str:
    value = _field(mapping, key, where)
    if not isinstance(value, str):
        raise _Unreadable(f"{where}: {key} is {value!r}, not a string")
    return value


def _entry_state(index: int, node: Any) -> tuple[CiState, str]:
    """(state, check name) for entry ``index``, or :class:`_Unreadable`."""
    where = f"check {index}"
    kind = _text(node, "__typename", where)
    if kind == "CheckRun":
        name = _text(node, "name", where)
        status = _text(node, "status", where)
        if status in _CHECK_RUN_RUNNING:
            return CiState.RUNNING, name
        if status != "COMPLETED":
            raise _Unreadable(f"{where} ({name}): status {status!r} is not one kstrl reads")
        conclusion = _text(node, "conclusion", where)
        if conclusion not in _CHECK_RUN_CONCLUSIONS:
            raise _Unreadable(f"{where} ({name}): conclusion {conclusion!r} is not one kstrl reads")
        return _CHECK_RUN_CONCLUSIONS[conclusion], name
    if kind == "StatusContext":
        name = _text(node, "context", where)
        value = _text(node, "state", where)
        if value not in _STATUS_CONTEXT_STATES:
            raise _Unreadable(f"{where} ({name}): state {value!r} is not one kstrl reads")
        return _STATUS_CONTEXT_STATES[value], name
    raise _Unreadable(f"{where}: type {kind!r} is not one kstrl reads")


def _entry_states(document: Any) -> list[tuple[CiState, str]]:
    """Every entry's state, or :class:`_Unreadable` for the first thing
    in the reply that cannot be read. Nothing is counted until all of it
    has been read."""
    if isinstance(document, dict) and "errors" in document:
        raise _Unreadable(f"GitHub answered with errors: {document['errors']!r}")
    data = _field(document, "data", "the reply")
    repository = _field(data, "repository", "data")
    if repository is None:
        raise _Unreadable("GitHub found no repository for this checkout")
    commit = _field(repository, "object", "repository")
    if commit is None:
        raise _Unreadable("GitHub has no commit with this sha")
    if not isinstance(commit, dict) or "statusCheckRollup" not in commit:
        raise _Unreadable("the object with this sha is not a commit")
    rollup = commit["statusCheckRollup"]
    if rollup is None:
        return []
    contexts = _field(rollup, "contexts", "statusCheckRollup")
    total = _field(contexts, "totalCount", "contexts")
    nodes = _field(contexts, "nodes", "contexts")
    if not isinstance(total, int) or isinstance(total, bool):
        raise _Unreadable(f"contexts: totalCount is {total!r}, not an integer")
    if not isinstance(nodes, list):
        raise _Unreadable("contexts: nodes is not a list")
    if len(nodes) != total:
        raise _Unreadable(f"read {len(nodes)} of {total} checks")
    return [_entry_state(index, node) for index, node in enumerate(nodes)]


def classify_reply(document: Any) -> tuple[CiState, str, int]:
    """(state, reason, checks counted) for one parsed reply. Pure.

    Failed wins over running, running over passed, and passed needs at
    least one entry: a commit with no checks is unknown.
    """
    try:
        states = _entry_states(document)
    except _Unreadable as exc:
        return CiState.UNKNOWN, _clip(str(exc)), 0
    if not states:
        return CiState.UNKNOWN, "GitHub reports no checks for this commit", 0
    failed = [name for state, name in states if state is CiState.FAILED]
    if failed:
        return CiState.FAILED, _clip("failed: " + ", ".join(failed)), len(states)
    running = [name for state, name in states if state is CiState.RUNNING]
    if running:
        return CiState.RUNNING, _clip("running: " + ", ".join(running)), len(states)
    return CiState.PASSED, f"{len(states)} checks passed", len(states)


# --- the transport ---------------------------------------------------------


def read_ci_state(sha: str, cwd: Path) -> CiReading:
    """Ask GitHub, through ``gh``, for the checks on ``sha``. Never raises
    for anything ``gh`` or GitHub does: every failure is an ``unknown``
    reading with the reason."""

    def reading(state: CiState, reason: str, checks: int = 0) -> CiReading:
        observed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return CiReading(sha, state, _clip(reason), observed_at, checks)

    if not _SHA.fullmatch(sha):
        return reading(CiState.UNKNOWN, "not a full commit sha")
    result = run_gh(
        [
            "api",
            "graphql",
            "-F",
            "owner={owner}",
            "-F",
            "repo={repo}",
            "-f",
            f"oid={sha}",
            "-f",
            f"query={CI_QUERY}",
        ],
        timeout=GH_POLL_TIMEOUT,
        cwd=cwd,
    )
    if not result.ok:
        return reading(CiState.UNKNOWN, result.error)
    try:
        document = read_json(result.stdout)
    except json.JSONDecodeError as exc:
        return reading(CiState.UNKNOWN, f"gh answered with text that is not JSON: {exc}")
    state, reason, checks = classify_reply(document)
    return reading(state, reason, checks)


# --- the ledger ------------------------------------------------------------


def read_ci_ledger(root_dir: Path) -> CiLedger:
    """The whole ledger for ``root_dir``, oldest first. No network.

    A missing file is the one legitimate empty read. ``OSError`` from the
    read and ``UnicodeDecodeError`` from the decode reach the caller:
    both calls sit outside any ``try``, so an unreadable ledger is a
    refusal, never an empty answer. A torn line, and valid JSON this
    reader cannot use, are counted in ``dropped``.
    """
    path = control_file(root_dir, CONTROL_CI_CHECKS)
    if not path.exists():
        return CiLedger()
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    readings: list[CiReading] = []
    dropped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = read_json(line)
        except json.JSONDecodeError:
            dropped += 1
            continue
        reading = CiReading.from_dict(record) if isinstance(record, dict) else None
        if reading is None:
            dropped += 1
            continue
        readings.append(reading)
    return CiLedger(readings=tuple(readings), dropped=dropped)


def poll_ci(root_dir: Path, shas: Sequence[str]) -> tuple[CiReading, ...]:
    """Read every sha, append every reading, return them in order.

    ``lock=True``: a scheduled ``ks ci poll`` and an operator's can
    interleave, as the signal ledger's two writers can. ``OSError`` from
    the append reaches the caller: a reading that was not recorded must
    not look recorded.
    """
    readings = tuple(read_ci_state(sha, root_dir) for sha in shas)
    ensure_control_state(root_dir)
    payload = "".join(json.dumps(reading.to_dict()) + "\n" for reading in readings)
    append_records(control_file(root_dir, CONTROL_CI_CHECKS), payload, repair="", lock=True)
    return readings
