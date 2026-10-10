"""An issue as kstrl holds it, and the rules that judge it.

This module holds the parsed record, the authorization rule, the spec that
an issue becomes, and the ledger of processed issues.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.intake_gh import GitHubIntakeConfig
from kstrl.jsonread import read_json
from kstrl.statedir import (
    CONTROL_GITHUB_PROCESSED,
    control_file,
    control_lock,
    ensure_control_state,
)

#: Cap on the spec text built from an issue. Generous for a real spec,
#: bounded enough that a pathological body cannot become a pathological
#: prompt. Truncation is announced in the spec itself, never silent.
MAX_SPEC_CHARS = 60_000

#: The human label that marks an issue as a bug report (#654, owner
#: decision 8), compared without regard to case as GitHub compares label
#: names. A bug report takes the same `ks factory` path as other work with
#: two changes: its run gets `--design-acceptance`, and its spec gets
#: BUG_REPORT_PROMPT.
BUG_LABEL = "bug"

#: H3 only: the line `spec_from_issue` adds to the spec of a bug report.
#: No calibration fixture carries a bug report, so no H2 capture applies.
BUG_REPORT_PROMPT_VERSION = "1.0.0"

BUG_REPORT_PROMPT = """\
> This item is a bug report. At least one acceptance check must reproduce
> the reported failure: that check has "onBase": "fails".

"""


@dataclass(frozen=True)
class RemoteIssue:
    """One GitHub issue as the adapter sees it."""

    number: int
    title: str
    body: str
    url: str
    labels: tuple[str, ...] = ()

    def source_ref(self, repo: str) -> str:
        """The stable identity used for dedupe: ``owner/name#123``."""
        return f"{repo}#{self.number}"


@dataclass
class ProcessedLedger:
    """Issues this repo has already admitted, so a re-poll is a no-op.

    Covers the half ``Queue.find_by_source_ref`` cannot: once an item is
    done and removed from the queue, only this record stops the issue
    being enqueued again on the very next poll.

    An unreadable ledger is treated as EMPTY, and that direction is
    deliberate but bounded: failing closed would stall intake entirely,
    while failing open can at worst re-enqueue an issue whose label is
    still applied - and the queue's own ``find_by_source_ref``, the
    ``max_items_per_sync`` cap, and the daily budget all still apply. The
    file is written atomically, so a torn read is not a normal event.
    """

    root_dir: Path
    _entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: R10.10 (#231 C1). The steering `since` cursor, one per pull
    #: request (`"<repo>#<number>"`), persisted BESIDE the processed-ids
    #: entries above - same file, same lock, same atomic write - rather
    #: than a second control file with its own load and write path. See
    #: :meth:`watermark` and :meth:`set_watermark`.
    _watermarks: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return control_file(self.root_dir, CONTROL_GITHUB_PROCESSED)

    def load(self) -> ProcessedLedger:
        ensure_control_state(self.root_dir)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # One clause: the class docstring above already argues that
            # an unreadable ledger is treated as EMPTY and why that is
            # bounded, and a decode failure is the same unreadable
            # ledger. Nothing here reports the cause, so #320's
            # separate-remedy rule has no message to separate.
            self._entries = {}
            self._watermarks = {}
            return self
        try:
            data = read_json(raw)
        except json.JSONDecodeError:
            self._entries = {}
            self._watermarks = {}
            return self
        if isinstance(data, dict) and isinstance(data.get("processed"), dict):
            self._entries = {str(k): v for k, v in data["processed"].items() if isinstance(v, dict)}
        else:
            self._entries = {}
        watermarks = data.get("watermarks") if isinstance(data, dict) else None
        if isinstance(watermarks, dict):
            self._watermarks = {str(k): v for k, v in watermarks.items() if isinstance(v, str)}
        else:
            self._watermarks = {}
        return self

    def contains(self, source_ref: str) -> bool:
        return source_ref in self._entries

    def record(self, source_ref: str, *, item_id: str, when: str) -> None:
        self._entries[source_ref] = {"item_id": item_id, "first_seen": when}
        self._write()

    def forget(self, source_ref: str) -> bool:
        """Drop an entry so the issue can be admitted again."""
        if source_ref not in self._entries:
            return False
        del self._entries[source_ref]
        self._write()
        return True

    def entries(self) -> dict[str, dict[str, Any]]:
        return dict(self._entries)

    def watermark(self, key: str) -> str:
        """The persisted steering ``since`` cursor for ``key``, or ``""``."""
        return self._watermarks.get(key, "")

    def set_watermark(self, key: str, value: str) -> None:
        """Advance ``key``'s steering watermark to ``value``, and persist it.

        A no-op when ``value`` does not advance the stored one: the
        watermark must be monotonic (#231 C1's rule is a maximum, not a
        replacement), and skipping the write also skips taking the lock
        on a cycle that resolved nothing new for this pull request.
        """
        if value <= self._watermarks.get(key, ""):
            return
        self._watermarks[key] = value
        self._write()

    def _write(self) -> None:
        from kstrl.workqueue_store import atomic_write

        ensure_control_state(self.root_dir)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with control_lock(self.root_dir):
            atomic_write(
                path,
                json.dumps(
                    {"version": 1, "processed": self._entries, "watermarks": self._watermarks},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )


@dataclass(frozen=True)
class Authorization:
    """Whether an issue's current bytes are the ones that were authorized.

    Review #187 F1: GitHub lets an issue AUTHOR edit the body after the
    fact. So a public contributor can submit something benign, wait for a
    maintainer to apply the trigger label, then rewrite the body - and
    those new bytes become factory input under an authorization granted
    for different ones. The label is a point-in-time act; the body is
    mutable; binding them is the only way the label means anything.
    """

    ok: bool
    reason: str = ""
    #: Who applied the trigger label. Surfaced in skip reasons, and the
    #: value :func:`authorization_refusal` matches against
    #: ``allowed_actors`` (#188).
    actor: str = ""
    labeled_at: str = ""
    last_edited_at: str = ""


def _actor_allowed(config: GitHubIntakeConfig, actor: str) -> bool:
    """Whether ``actor`` is on ``allowed_actors``. The ONE membership test.

    #188 owns the list; R10.10 asks the same question about a comment
    author. Two spellings of it means the weaker one is the one some
    gate consults, so both callers come through here. Compared
    casefolded and stripped, which is what ``allowed_actors``'s own
    docstring promises.
    """
    return actor.strip().casefold() in {name.strip().casefold() for name in config.allowed_actors}


def authorization_refusal(
    config: GitHubIntakeConfig,
    auth: Authorization | None,
) -> str:
    """Why this issue is not authorized, or "" when it is.

    Two questions, one answer, because to an operator they are the same
    refusal: were the authorized bytes the bytes that will run (#187 F1,
    :func:`verify_authorization`), and was the actor who applied the
    trigger label one this project chose to trust (#188)?

    An unreadable authorization falls through to "the authorization check
    refused without saying why" rather than "", which would read as an
    admission.

    ``allowed_actors`` empty means no allowlist: this returns "" and keeps
    the inherited-permission behaviour, anyone who can apply the label can
    spend.
    """
    if auth is not None and not auth.ok:
        return auth.reason or "the authorization check refused without saying why"
    if not config.allowed_actors:
        return ""
    actor = auth.actor if auth is not None else ""
    if not _actor_allowed(config, actor):
        return (
            f"{config.queued_label} was applied by {actor or 'an unknown actor'}, "
            f"who is not in [intake_github] allowed_actors "
            f"({', '.join(config.allowed_actors)})"
        )
    return ""


def is_bug_report(issue: RemoteIssue) -> bool:
    """Whether the issue carries the human ``bug`` label (:data:`BUG_LABEL`)."""
    return any(label.casefold() == BUG_LABEL for label in issue.labels)


def spec_from_issue(issue: RemoteIssue, repo: str) -> str:
    """Build the spec text the factory will decompose.

    The provenance header is not decoration: a spec that reaches the
    architect without saying where it came from produces a PR nobody can
    trace back to a request.
    """
    body = issue.body.strip()
    header = (
        f"# {issue.title}\n\n"
        f"> Sourced from {issue.source_ref(repo)}"
        + (f" ({issue.url})" if issue.url else "")
        + "\n\n"
        + (BUG_REPORT_PROMPT if is_bug_report(issue) else "")
    )
    spec = header + body
    if len(spec) > MAX_SPEC_CHARS:
        keep = MAX_SPEC_CHARS - len(header) - 80
        spec = (
            header + body[: max(0, keep)] + "\n\n[truncated by kstrl: issue body exceeded "
            f"{MAX_SPEC_CHARS} characters]\n"
        )
    return spec
