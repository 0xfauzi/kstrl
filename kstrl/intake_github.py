"""R8.6 continuous intake: GitHub Issues as the remote inbox.

An issue labelled ``kstrl:queued`` becomes a queue item; the queue's
verdict comes back as a state label and a comment. Polling only - no
webhooks, no public endpoint, nothing to keep reachable.

**What authorizes work, stated accurately.** The trigger is the LABEL,
not the issue: a stranger can open an issue but cannot label it. What
that boundary actually is, however, is narrower than it first appears,
and the first version of this module overclaimed it:

- Applying a label needs the **Triage** role or above, NOT write/push
  access. On an organization repository a triager who cannot push a line
  of code can still authorize factory spend (review #187 F2).
- Any GitHub Action in the repo with ``issues: write`` can apply the
  label, so a workflow can trigger spend with no human involved.

So this is a permission designed for *managing issues*, borrowed to
authorize *money*. Issue #188 replaces it with an explicit actor
allowlist, which is what makes the authorization this project's own
decision rather than an inherited one. Until then the residual risk is
exactly the two bullets above, bounded by the adapter being off by
default.

What IS enforced here is that the authorized bytes are the bytes that
run: an issue edited after it was labelled is refused, because GitHub
lets an issue author rewrite the body after a maintainer labelled it
(review #187 F1).

**Strictly additive.** A front-end outage must never block the local
queue (R8.6). Every ``gh`` call therefore returns a result object instead
of raising, and every failure path leaves the queue exactly as it was.
The local queue is the system of record; GitHub is a view onto it that
happens to also be an input.

**Idempotency has two halves.** ``Queue.find_by_source_ref`` covers items
still in the queue; the processed-ids ledger here covers items that have
already left it (done, removed). Without the ledger, an issue whose item
finished would be re-enqueued on the next poll forever.

**Remote items never auto-merge.** ``MergeDisposition.STOP_AT_PR`` is
forced regardless of labels or config: continuous intake must not
silently delete the human merge gate, and an issue label is the last
place that decision should be settable from.

**Prompt injection.** An issue body becomes a spec that reaches the
architect. The defense is already in ``decompose``, which wraps the spec
between per-run random delimiters as untrusted data (R5.3). This module
deliberately does NOT pattern-match issue bodies for injection strings:
a spec legitimately discussing prompts would be rejected, and rejecting
work is not the same as containing it. What this module does add is a
size cap, so a pathological body cannot become a pathological prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl.workqueue import (
    ItemSource,
    MergeDisposition,
    Queue,
    QueueError,
    QueueItem,
    queue_root,
)

GITHUB_LEDGER_FILENAME = "github_processed.json"

#: Cap on the spec text built from an issue. Generous for a real spec,
#: bounded enough that a pathological body cannot become a pathological
#: prompt. Truncation is announced in the spec itself, never silent.
MAX_SPEC_CHARS = 60_000

#: Poll paging. The window GROWS until the admission cap can be filled or
#: the inbox is exhausted, because skipped issues do not consume the cap
#: (review #187 F6).
POLL_PAGE_SIZE = 30
MAX_POLL_PAGES = 4
MAX_POLL_LIMIT = 200
#: Breadth to gather relative to the cap before stopping.
POLL_OVERSCAN = 4


class IntakeError(RuntimeError):
    """A configuration problem the operator must fix.

    Deliberately NOT raised for transport failures: those return a result
    object so a GitHub outage cannot propagate into the queue path.
    """


@dataclass(frozen=True)
class GhResult:
    """Outcome of one ``gh`` invocation. Never an exception."""

    ok: bool
    stdout: str = ""
    error: str = ""


def run_gh(
    args: list[str], *, timeout: float, cwd: Path | None = None,
) -> GhResult:
    """Invoke ``gh``, converting every failure into a value.

    The adapter is additive by contract, so a missing binary, a timeout,
    an auth failure, and a rate limit all have to be survivable. Callers
    branch on ``ok``; nothing here escapes as an exception.
    """
    import shutil

    if shutil.which("gh") is None:
        return GhResult(ok=False, error="gh CLI is not installed")
    try:
        completed = subprocess.run(
            ["gh", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GhResult(ok=False, error=f"gh {args[0]} timed out after {timeout}s")
    except OSError as exc:
        return GhResult(ok=False, error=f"gh {args[0]} could not run: {exc}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return GhResult(
            ok=False,
            stdout=completed.stdout,
            error=f"gh {args[0]} failed ({completed.returncode}): {detail[:500]}",
        )
    return GhResult(ok=True, stdout=completed.stdout)


@dataclass(frozen=True)
class GitHubIntakeConfig:
    """``[intake_github]`` config. Off by default.

    Opt-in because enabling it makes an outbound poller and a writer of
    public comments out of a local tool; that should never happen because
    a default changed. ``dry_run`` records writebacks instead of sending
    them, mirroring ``LinearConfig.dry_run``.
    """

    enabled: bool = False
    #: ``owner/name``; empty resolves from the checkout's origin remote.
    repo: str = ""
    #: The label that authorizes work. Applying it needs write access.
    queued_label: str = "kstrl:queued"
    #: Prefix for the state labels written back.
    label_prefix: str = "kstrl:"
    #: Upper bound on items admitted per sync, so a label applied to
    #: fifty issues at once cannot enqueue fifty runs.
    max_items_per_sync: int = 5
    default_priority: int = 0
    comment_on_result: bool = True
    dry_run: bool = False
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.queued_label.strip():
            raise IntakeError("intake_github.queued_label must not be empty")
        if self.max_items_per_sync < 1:
            raise IntakeError(
                "intake_github.max_items_per_sync must be >= 1, got "
                f"{self.max_items_per_sync}"
            )
        if self.timeout_seconds <= 0:
            raise IntakeError(
                "intake_github.timeout_seconds must be > 0, got "
                f"{self.timeout_seconds}"
            )
        if self.repo and self.repo.count("/") != 1:
            raise IntakeError(
                f"intake_github.repo must be 'owner/name', got {self.repo!r}"
            )

    def state_label(self, state: str) -> str:
        return f"{self.label_prefix}{state}"

    @property
    def managed_labels(self) -> tuple[str, ...]:
        """Every label this adapter owns, including the trigger.

        Used to strip stale state before applying a new one, so an issue
        cannot end up carrying ``kstrl:running`` and ``kstrl:done`` at
        once.
        """
        return (
            self.queued_label,
            *(
                self.state_label(name)
                for name in ("running", "done", "failed", "poison")
            ),
        )

    @classmethod
    def from_env(cls) -> GitHubIntakeConfig:
        defaults = cls()
        enabled = os.environ.get("KSTRL_INTAKE_GITHUB_ENABLED")
        repo = os.environ.get("KSTRL_INTAKE_GITHUB_REPO")
        label = os.environ.get("KSTRL_INTAKE_GITHUB_QUEUED_LABEL")
        prefix = os.environ.get("KSTRL_INTAKE_GITHUB_LABEL_PREFIX")
        cap = os.environ.get("KSTRL_INTAKE_GITHUB_MAX_ITEMS")
        priority = os.environ.get("KSTRL_INTAKE_GITHUB_PRIORITY")
        comment = os.environ.get("KSTRL_INTAKE_GITHUB_COMMENT")
        dry = os.environ.get("KSTRL_INTAKE_GITHUB_DRY_RUN")
        timeout = os.environ.get("KSTRL_INTAKE_GITHUB_TIMEOUT")
        return cls(
            enabled=defaults.enabled if enabled is None else enabled == "1",
            repo=defaults.repo if repo is None else repo,
            queued_label=defaults.queued_label if label is None else label,
            label_prefix=defaults.label_prefix if prefix is None else prefix,
            max_items_per_sync=(
                defaults.max_items_per_sync if cap is None else int(cap)
            ),
            default_priority=(
                defaults.default_priority if priority is None else int(priority)
            ),
            comment_on_result=(
                defaults.comment_on_result if comment is None else comment == "1"
            ),
            dry_run=defaults.dry_run if dry is None else dry == "1",
            timeout_seconds=(
                defaults.timeout_seconds if timeout is None else float(timeout)
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> GitHubIntakeConfig:
        """Precedence: env > toml > defaults; reads ``[intake_github]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(
            resolve_config_file(root_dir), "intake_github",
        )
        defaults = cls()

        def _str(key: str, fallback: str) -> str:
            return str(section[key]) if key in section else fallback

        def _int(key: str, fallback: int) -> int:
            return int(section[key]) if key in section else fallback

        def _bool(key: str, fallback: bool) -> bool:
            return bool(section[key]) if key in section else fallback

        values: dict[str, Any] = {
            "enabled": _bool("enabled", defaults.enabled),
            "repo": _str("repo", defaults.repo),
            "queued_label": _str("queued_label", defaults.queued_label),
            "label_prefix": _str("label_prefix", defaults.label_prefix),
            "max_items_per_sync": _int(
                "max_items_per_sync", defaults.max_items_per_sync,
            ),
            "default_priority": _int(
                "default_priority", defaults.default_priority,
            ),
            "comment_on_result": _bool(
                "comment_on_result", defaults.comment_on_result,
            ),
            "dry_run": _bool("dry_run", defaults.dry_run),
            "timeout_seconds": float(
                section["timeout_seconds"]
                if "timeout_seconds" in section else defaults.timeout_seconds
            ),
        }
        env_map: dict[str, tuple[str, Callable[[str], Any]]] = {
            "KSTRL_INTAKE_GITHUB_ENABLED": ("enabled", lambda v: v == "1"),
            "KSTRL_INTAKE_GITHUB_REPO": ("repo", str),
            "KSTRL_INTAKE_GITHUB_QUEUED_LABEL": ("queued_label", str),
            "KSTRL_INTAKE_GITHUB_LABEL_PREFIX": ("label_prefix", str),
            "KSTRL_INTAKE_GITHUB_MAX_ITEMS": ("max_items_per_sync", int),
            "KSTRL_INTAKE_GITHUB_PRIORITY": ("default_priority", int),
            "KSTRL_INTAKE_GITHUB_COMMENT": (
                "comment_on_result", lambda v: v == "1",
            ),
            "KSTRL_INTAKE_GITHUB_DRY_RUN": ("dry_run", lambda v: v == "1"),
            "KSTRL_INTAKE_GITHUB_TIMEOUT": ("timeout_seconds", float),
        }
        for var, (name, cast) in env_map.items():
            if var in os.environ:
                values[name] = cast(os.environ[var])
        return cls(**values)


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


def parse_issue_list(payload: str) -> tuple[list[RemoteIssue], str]:
    """Decode ``gh issue list --json``; returns ``(issues, error)``.

    Tolerant PER ENTRY - one unparseable issue must not discard the whole
    poll, because the queue would then stall on a single bad issue - but
    STRICT about the top level. Review #187 F7: a malformed payload used
    to collapse to the same empty list as a healthy ``[]``, so a `gh`
    output-shape change or a truncated response looked like a successful
    empty poll and no cron or launchd wrapper could alert on it.
    """
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError as exc:
        return [], f"could not parse `gh issue list` output: {exc}"
    if not isinstance(data, list):
        return [], (
            f"`gh issue list` returned {type(data).__name__}, expected a list"
        )
    issues: list[RemoteIssue] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        raw_labels = entry.get("labels")
        labels: tuple[str, ...] = ()
        if isinstance(raw_labels, list):
            labels = tuple(
                str(item["name"])
                for item in raw_labels
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
        issues.append(RemoteIssue(
            number=number,
            title=str(entry.get("title") or f"issue #{number}"),
            body=str(entry.get("body") or ""),
            url=str(entry.get("url") or ""),
            labels=labels,
        ))
    # Oldest first: FIFO across the remote inbox, matching the queue's own
    # ordering within a priority band. The poll ALSO asks GitHub to sort
    # ascending - sorting a truncated page cannot establish FIFO on its
    # own (review #187 F6).
    issues.sort(key=lambda issue: issue.number)
    return issues, ""


def resolve_repo(config: GitHubIntakeConfig, root_dir: Path) -> tuple[str, str]:
    """The target repo, or an error string. Never raises."""
    if config.repo:
        return config.repo, ""
    result = run_gh(
        ["repo", "view", "--json", "nameWithOwner"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return "", f"could not resolve the repo from the checkout: {result.error}"
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return "", f"could not parse `gh repo view` output: {exc}"
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(name, str) or name.count("/") != 1:
        return "", "`gh repo view` returned no usable nameWithOwner"
    return name, ""


def poll_ladder(config: GitHubIntakeConfig) -> list[int]:
    """Widening page sizes to try, smallest first.

    A ladder rather than a single window because skipped issues do not
    consume the admission cap, so how far to look depends on how many of
    what we found is eligible - which only the planner knows (#187 F6).
    """
    ladder: list[int] = []
    limit = max(POLL_PAGE_SIZE, config.max_items_per_sync)
    while limit < MAX_POLL_LIMIT and len(ladder) < MAX_POLL_PAGES - 1:
        ladder.append(limit)
        limit = min(limit * 2, MAX_POLL_LIMIT)
    ladder.append(MAX_POLL_LIMIT)
    return ladder


def poll_queued(
    config: GitHubIntakeConfig,
    repo: str,
    root_dir: Path,
    *,
    limit: int = 0,
) -> tuple[list[RemoteIssue], str, bool]:
    """Open issues carrying the trigger label; ``(issues, error, exhausted)``.

    ``exhausted`` is True when the page came back shorter than the limit,
    which is how the caller knows there is nothing further to find. The
    caller drives the widening, because whether a wider window is needed
    depends on how many of these issues are ELIGIBLE - and only the
    planner can say that (#187 F6).

    ``sort:created-asc`` asks GitHub for ascending order rather than
    relying on sorting whatever page came back.

    Note for the record (H4): this does NOT use conditional requests.
    ``gh issue list`` exposes no ETag, so the saving the R8.6 plan
    attributed to ETags is not realised here. It is not needed at this
    cadence: one call per poll interval is ~60/hour against 5,000/hour.
    """
    page = limit if limit > 0 else max(POLL_PAGE_SIZE, config.max_items_per_sync)
    result = run_gh(
        [
            "issue", "list",
            "--repo", repo,
            "--search",
            f'label:"{config.queued_label}" state:open sort:created-asc',
            "--limit", str(page),
            "--json", "number,title,body,url,labels",
        ],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return [], result.error, False
    issues, parse_error = parse_issue_list(result.stdout)
    if parse_error:
        return [], parse_error, False
    return issues, "", len(issues) < page


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

    @property
    def path(self) -> Path:
        return queue_root(self.root_dir) / GITHUB_LEDGER_FILENAME

    def load(self) -> ProcessedLedger:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            self._entries = {}
            return self
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._entries = {}
            return self
        if isinstance(data, dict) and isinstance(data.get("processed"), dict):
            self._entries = {
                str(k): v for k, v in data["processed"].items()
                if isinstance(v, dict)
            }
        else:
            self._entries = {}
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

    def _write(self) -> None:
        from kstrl.workqueue import atomic_write

        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            json.dumps(
                {"version": 1, "processed": self._entries},
                indent=2, ensure_ascii=False,
            ) + "\n",
        )


# ---------------------------------------------------------------------------
# Authorization: bind the label to the bytes it authorized
# ---------------------------------------------------------------------------


#: One GraphQL call gets the body's last-edit time AND the labelling
#: events with their actors, so the authorization check costs one request
#: per candidate rather than two.
_AUTH_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    issue(number:$number) {
      lastEditedAt
      timelineItems(itemTypes:[LABELED_EVENT], last:50) {
        nodes { ... on LabeledEvent {
          createdAt
          label { name }
          actor { login }
        } }
      }
    }
  }
}
"""


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
    #: Who applied the trigger label. Captured and surfaced in skip
    #: reasons now; issue #188 turns it into an allowlist decision.
    actor: str = ""
    labeled_at: str = ""
    last_edited_at: str = ""


def verify_authorization(
    config: GitHubIntakeConfig, repo: str, number: int, root_dir: Path,
) -> Authorization:
    """Confirm the issue has not been edited since it was labelled.

    Fails CLOSED on every uncertainty - unreadable response, missing
    label event, unparseable timestamps - because "we could not check"
    is not evidence that the bytes are the authorized ones. This mirrors
    every other R8.6 admission gate.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return Authorization(ok=False, reason=f"unusable repo {repo!r}")
    result = run_gh(
        [
            "api", "graphql",
            "-f", f"query={_AUTH_QUERY}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={number}",
        ],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return Authorization(
            ok=False,
            reason=f"could not read the authorization timeline: {result.error}",
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return Authorization(
            ok=False, reason=f"unparseable authorization timeline: {exc}",
        )
    issue = (
        payload.get("data", {}).get("repository", {}).get("issue")
        if isinstance(payload, dict) else None
    )
    if not isinstance(issue, dict):
        return Authorization(
            ok=False, reason="authorization timeline had no issue node",
        )

    nodes = issue.get("timelineItems", {})
    raw_nodes = nodes.get("nodes") if isinstance(nodes, dict) else None
    latest_at = ""
    actor = ""
    for node in raw_nodes or []:
        if not isinstance(node, dict):
            continue
        label = node.get("label")
        if not isinstance(label, dict) or label.get("name") != config.queued_label:
            continue
        created = node.get("createdAt")
        if not isinstance(created, str):
            continue
        if created >= latest_at:
            latest_at = created
            who = node.get("actor")
            actor = who.get("login", "") if isinstance(who, dict) else ""
    if not latest_at:
        return Authorization(
            ok=False,
            reason=(
                f"no {config.queued_label!r} labelling event found; refusing "
                "to treat a label of unknown provenance as authorization"
            ),
        )

    edited_at = issue.get("lastEditedAt")
    if isinstance(edited_at, str) and edited_at:
        # ISO-8601 UTC from GitHub, so lexicographic order is chronological.
        if edited_at > latest_at:
            return Authorization(
                ok=False,
                actor=actor,
                labeled_at=latest_at,
                last_edited_at=edited_at,
                reason=(
                    f"the issue body was edited at {edited_at}, after it was "
                    f"labelled at {latest_at} by {actor or 'an unknown actor'}; "
                    "re-apply the label to authorize the current text"
                ),
            )
    return Authorization(
        ok=True, actor=actor, labeled_at=latest_at,
        last_edited_at=edited_at if isinstance(edited_at, str) else "",
    )


# ---------------------------------------------------------------------------
# Planning: one side-effect-free decision tree
# ---------------------------------------------------------------------------


class Decision(StrEnum):
    """What a sync would do with one polled issue."""

    ADMIT = "admit"
    SKIP_PROCESSED = "skip_processed"
    SKIP_IN_QUEUE = "skip_in_queue"
    SKIP_EMPTY_BODY = "skip_empty_body"
    SKIP_CAP = "skip_cap"
    REFUSE_UNAUTHORIZED = "refuse_unauthorized"

    @property
    def admits(self) -> bool:
        return self is Decision.ADMIT


@dataclass(frozen=True)
class PlannedIssue:
    """One issue plus the decision and the reason for it."""

    issue: RemoteIssue
    decision: Decision
    reason: str = ""
    authorization: Authorization | None = None

    @property
    def source_ref_for(self) -> str:
        return ""


def plan_sync(
    queue: Queue,
    config: GitHubIntakeConfig,
    repo: str,
    issues: Sequence[RemoteIssue],
    ledger: ProcessedLedger,
    *,
    authorizer: Callable[[RemoteIssue], Authorization] | None = None,
) -> list[PlannedIssue]:
    """Decide what to do with each polled issue, mutating NOTHING.

    ONE decision tree, shared by :func:`sync` and by ``ks queue sync
    --dry-run``. Review #187 F4/F11: dry-run had its own copy that
    reported every eligible issue as ``ENQUEUE`` while ignoring the
    admission cap, and the config-level ``dry_run`` flag suppressed only
    the remote writes while still enqueueing locally - so a "dry run"
    could launch paid work. A dry run that disagrees with the real thing
    is worse than no dry run, and the only way to guarantee agreement is
    for there to be one implementation.
    """
    planned: list[PlannedIssue] = []
    admitted = 0
    for issue in issues:
        ref = issue.source_ref(repo)
        if ledger.contains(ref):
            planned.append(PlannedIssue(
                issue, Decision.SKIP_PROCESSED, "already processed",
            ))
            continue
        if queue.find_by_source_ref(ref) is not None:
            planned.append(PlannedIssue(
                issue, Decision.SKIP_IN_QUEUE, "already in the queue",
            ))
            continue
        if not issue.body.strip():
            planned.append(PlannedIssue(
                issue, Decision.SKIP_EMPTY_BODY,
                "issue body is empty; nothing to build",
            ))
            continue
        if admitted >= config.max_items_per_sync:
            planned.append(PlannedIssue(
                issue, Decision.SKIP_CAP,
                f"per-sync cap of {config.max_items_per_sync} reached",
            ))
            continue
        auth = authorizer(issue) if authorizer is not None else None
        if auth is not None and not auth.ok:
            planned.append(PlannedIssue(
                issue, Decision.REFUSE_UNAUTHORIZED, auth.reason, auth,
            ))
            continue
        planned.append(PlannedIssue(issue, Decision.ADMIT, "", auth))
        admitted += 1
    return planned


def checkout_repo(config: GitHubIntakeConfig, root_dir: Path) -> tuple[str, str]:
    """The repo the CHECKOUT points at, independent of config.

    Needed because ``target_repo`` is only metadata: ``serve_cycle``
    always runs the factory against its own ``root_dir``. Review #187 F3:
    an explicit ``[intake_github] repo = "B"`` inside checkout A admitted
    B's issues and executed them against A, which would open a PR in the
    wrong repository.
    """
    result = run_gh(
        ["repo", "view", "--json", "nameWithOwner"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return "", f"could not resolve the checkout's repo: {result.error}"
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return "", f"could not parse the checkout's repo: {exc}"
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(name, str) or name.count("/") != 1:
        return "", "`gh repo view` returned no usable nameWithOwner"
    return name, ""


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
    )
    spec = header + body
    if len(spec) > MAX_SPEC_CHARS:
        keep = MAX_SPEC_CHARS - len(header) - 80
        spec = (
            header
            + body[: max(0, keep)]
            + "\n\n[truncated by kstrl: issue body exceeded "
            f"{MAX_SPEC_CHARS} characters]\n"
        )
    return spec


@dataclass
class SyncResult:
    """What one sync did. Every field is something a test or the CLI reads."""

    repo: str = ""
    polled: int = 0
    enqueued: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    #: The full decision list, so the CLI can render exactly what the
    #: production planner decided rather than re-deriving it.
    planned: tuple[PlannedIssue, ...] = ()
    #: Set on a dry run: refs that WOULD have been admitted.
    would_enqueue: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def apply_state_label(
    config: GitHubIntakeConfig,
    repo: str,
    number: int,
    state: str,
    root_dir: Path,
) -> str:
    """Move an issue to exactly one kstrl state label.

    Removes every managed label the adapter owns before adding the new
    one, so an issue can never carry two contradictory states. Returns an
    error string, or "" on success; a writeback failure is reported, never
    raised, because the queue transition it describes has already
    happened locally.
    """
    if config.dry_run:
        return ""
    target = config.state_label(state)
    remove = [name for name in config.managed_labels if name != target]
    args = [
        "issue", "edit", str(number),
        "--repo", repo,
        "--add-label", target,
    ]
    for name in remove:
        args.extend(["--remove-label", name])
    result = run_gh(args, timeout=config.timeout_seconds, cwd=root_dir)
    return "" if result.ok else result.error


def post_comment(
    config: GitHubIntakeConfig,
    repo: str,
    number: int,
    body: str,
    root_dir: Path,
) -> str:
    """Comment on the source issue. Returns an error string, or ""."""
    if config.dry_run or not config.comment_on_result:
        return ""
    result = run_gh(
        ["issue", "comment", str(number), "--repo", repo, "--body", body],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    return "" if result.ok else result.error


def issue_number_from_ref(source_ref: str) -> int:
    """``owner/name#123`` -> 123; 0 when the ref is not a GitHub issue."""
    _, sep, tail = source_ref.partition("#")
    if not sep:
        return 0
    try:
        return int(tail)
    except ValueError:
        return 0


def repo_from_ref(source_ref: str) -> str:
    """``owner/name#123`` -> ``owner/name``; "" when not a GitHub ref."""
    head, sep, _ = source_ref.partition("#")
    return head if sep and head.count("/") == 1 else ""


def sync(
    queue: Queue,
    config: GitHubIntakeConfig,
    root_dir: Path,
    *,
    now_iso: str = "",
    verify: bool = True,
    commit_guard: Callable[[], AbstractContextManager[Any]] | None = None,
) -> SyncResult:
    """Admit labelled issues into the local queue.

    Additive by construction: every failure path returns a result with
    errors recorded and the queue untouched. A GitHub outage produces an
    empty sync, not a stalled queue.

    ``config.dry_run`` makes this side-effect free - it plans and reports
    without touching the queue or the ledger (review #187 F4: it
    previously suppressed only the remote writes while still enqueueing,
    so a "dry run" could launch paid work).

    ``commit_guard`` is entered ONLY around the local commit, never around
    the network work. Review #189 N1: the daemon wrapped this whole
    function in the queue mutex, so a slow GitHub blocked
    ``ks queue pause`` and every other queue transition - reintroducing
    exactly the problem #187 F10 removed from writeback. Polling and
    per-issue authorization now happen unlocked; the plan is then RE-run
    under the guard against fresh queue state, which costs nothing
    because authorization is memoized.
    """
    result = SyncResult()
    if not config.enabled:
        result.errors = ("[intake_github] enabled is false",)
        return result

    repo, error = resolve_repo(config, root_dir)
    if error:
        result.errors = (error,)
        return result
    result.repo = repo

    # The inbox must be the repo the factory will actually run against:
    # target_repo is metadata, and serve always executes in root_dir.
    local_repo, local_error = checkout_repo(config, root_dir)
    if local_error:
        result.errors = (
            f"refusing to admit work without confirming the execution "
            f"repository: {local_error}",
        )
        return result
    if local_repo.lower() != repo.lower():
        result.errors = (
            f"refusing cross-repository intake: the inbox is {repo} but this "
            f"checkout is {local_repo}, and a run would execute against "
            f"{local_repo}. Point [intake_github] repo at {local_repo}, or "
            "run the daemon from a checkout of the inbox repository.",
        )
        return result

    ledger = ProcessedLedger(root_dir).load()

    # Authorization costs one API call per candidate, and the widening
    # loop below re-plans, so memoize per sync.
    checked: dict[int, Authorization] = {}

    def _authorize(issue: RemoteIssue) -> Authorization:
        if issue.number not in checked:
            checked[issue.number] = verify_authorization(
                config, repo, issue.number, root_dir,
            )
        return checked[issue.number]

    authorizer = _authorize if verify else None
    planned: list[PlannedIssue] = []
    polled_issues: list[RemoteIssue] = []
    # ---- network phase: NO lock is held here (#189 N1) ----
    for page_limit in poll_ladder(config):
        issues, poll_error, exhausted = poll_queued(
            config, repo, root_dir, limit=page_limit,
        )
        if poll_error:
            result.errors = (poll_error,)
            return result
        result.polled = len(issues)
        polled_issues = issues
        planned = plan_sync(
            queue, config, repo, issues, ledger, authorizer=authorizer,
        )
        admitted = sum(1 for entry in planned if entry.decision.admits)
        # Stop when the cap is filled or there is nothing more to look at.
        if admitted >= config.max_items_per_sync or exhausted:
            break
    result.planned = tuple(planned)
    for entry in planned:
        if not entry.decision.admits:
            result.skipped[entry.issue.source_ref(repo)] = entry.reason

    if config.dry_run:
        # Planned, reported, nothing written. The CLI's --dry-run uses the
        # same planner, so the two cannot disagree.
        result.would_enqueue = tuple(
            entry.issue.source_ref(repo) for entry in planned
            if entry.decision.admits
        )
        return result

    stamp = now_iso or _utc_now_iso()
    enqueued: list[str] = []
    errors: list[str] = []

    # ---- commit phase: the guard covers ONLY local writes ----
    guard = commit_guard() if commit_guard is not None else nullcontext()
    with guard:
        # Re-plan against fresh queue state: another process may have
        # enqueued the same ref while we were on the network. Authorization
        # is memoized, so this makes no new requests.
        ledger = ProcessedLedger(root_dir).load()
        planned = plan_sync(
            queue, config, repo, polled_issues, ledger, authorizer=authorizer,
        )
        result.planned = tuple(planned)
        result.skipped = {
            entry.issue.source_ref(repo): entry.reason
            for entry in planned if not entry.decision.admits
        }
        enqueued, errors = _commit_admissions(
            queue, config, repo, planned, ledger, stamp,
        )

    result.enqueued = tuple(enqueued)
    result.errors = tuple(errors)
    return result


def _commit_admissions(
    queue: Queue,
    config: GitHubIntakeConfig,
    repo: str,
    planned: Sequence[PlannedIssue],
    ledger: ProcessedLedger,
    stamp: str,
) -> tuple[list[str], list[str]]:
    """Enqueue the admitted issues. Caller holds the commit guard."""
    enqueued: list[str] = []
    errors: list[str] = []
    for entry in planned:
        if not entry.decision.admits:
            continue
        issue = entry.issue
        ref = issue.source_ref(repo)
        try:
            item = queue.add(
                spec_from_issue(issue, repo),
                title=issue.title,
                priority=config.default_priority,
                # FORCED, never configurable from the remote side.
                merge_disposition=MergeDisposition.STOP_AT_PR,
                source=ItemSource.GITHUB,
                source_ref=ref,
                target_repo=repo,
                spec_filename="spec.md",
                actor="intake-github",
            )
        except (QueueError, OSError) as exc:
            errors.append(f"{ref}: could not enqueue ({exc})")
            continue

        # Admission and dedupe must commit together. Review #187 F5:
        # queue.add publishes atomically, so a failing ledger.record left
        # a live queued item with no durable processed entry AND escaped
        # the whole batch - after which the issue could be admitted twice.
        try:
            ledger.record(ref, item_id=item.item_id, when=stamp)
        except OSError as exc:
            try:
                queue.remove(item, actor="intake-github")
                undone = "the queued item was rolled back"
            except (QueueError, OSError) as undo_exc:
                undone = (
                    f"AND the rollback failed ({undo_exc}); remove "
                    f"{item.item_id} by hand"
                )
            errors.append(
                f"{ref}: could not record the dedupe entry ({exc}); {undone}"
            )
            continue

        enqueued.append(ref)

    return enqueued, errors


def report_outcome(
    item: QueueItem,
    *,
    state: str,
    detail: str,
    config: GitHubIntakeConfig,
    root_dir: Path,
) -> str:
    """Write a queue verdict back to the source issue.

    Called by the daemon on terminal transitions. Returns an error
    string, or "" when there was nothing to do or it succeeded. Never
    raises: the local transition already happened, and a failed comment
    must not undo it.
    """
    if not config.enabled or item.source is not ItemSource.GITHUB:
        return ""
    repo = repo_from_ref(item.source_ref) or config.repo
    number = issue_number_from_ref(item.source_ref)
    if not repo or number <= 0:
        return f"cannot map {item.source_ref!r} to a GitHub issue"

    errors: list[str] = []
    label_error = apply_state_label(config, repo, number, state, root_dir)
    if label_error:
        errors.append(label_error)
    body = _outcome_comment(item, state, detail)
    comment_error = post_comment(config, repo, number, body, root_dir)
    if comment_error:
        errors.append(comment_error)
    return "; ".join(errors)


def _outcome_comment(item: QueueItem, state: str, detail: str) -> str:
    """The comment body. Says what happened and what a human should do."""
    lines = [f"**kstrl: {state}**", ""]
    if detail:
        lines.extend([detail, ""])
    lines.append(
        f"Queue item `{item.item_id}` - attempt {item.attempts} of "
        f"{item.max_attempts}."
    )
    if state == "poison":
        lines.extend([
            "",
            "This will NOT be retried automatically. Inspect it with "
            f"`ks queue show {item.item_id[:12]}` and, if it should run "
            "again, `ks queue retry --reset-attempts`.",
        ])
    elif state == "done":
        lines.extend([
            "",
            "The PR waits for a human merge decision: remote-sourced items "
            "always stop at the PR.",
        ])
    return "\n".join(lines)


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
