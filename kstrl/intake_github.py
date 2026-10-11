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
authorize *money*. ``allowed_actors`` (#188) replaces it with a decision
this project owns: with the list non-empty, only the actor of the latest
TRIGGER-label event may authorize a run, and every uncertainty about who
that was is a refusal. Left empty it changes nothing, and the residual
risk is exactly the two bullets above, bounded by the adapter being off
by default.

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
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl import intake_gh
from kstrl.intake_gh import GitHubIntakeConfig, _utc_now_iso
from kstrl.intake_issue import (
    Authorization,
    ProcessedLedger,
    RemoteIssue,
    authorization_refusal,
    is_bug_report,
    spec_from_issue,
)
from kstrl.jsonread import read_json
from kstrl.workqueue import Queue
from kstrl.workqueue_items import ItemSource, MergeDisposition, QueueError

#: Poll paging. The window GROWS until the admission cap can be filled or
#: the inbox is exhausted, because skipped issues do not consume the cap
#: (review #187 F6).
POLL_PAGE_SIZE = 30
MAX_POLL_PAGES = 4
MAX_POLL_LIMIT = 200
#: Breadth to gather relative to the cap before stopping.
POLL_OVERSCAN = 4


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
        data = read_json(payload or "[]")
    except json.JSONDecodeError as exc:
        return [], f"could not parse `gh issue list` output: {exc}"
    if not isinstance(data, list):
        return [], (f"`gh issue list` returned {type(data).__name__}, expected a list")
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
        issues.append(
            RemoteIssue(
                number=number,
                title=str(entry.get("title") or f"issue #{number}"),
                body=str(entry.get("body") or ""),
                url=str(entry.get("url") or ""),
                labels=labels,
            )
        )
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
    result = intake_gh.run_gh(
        ["repo", "view", "--json", "nameWithOwner"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return "", f"could not resolve the repo from the checkout: {result.error}"
    try:
        data = read_json(result.stdout or "{}")
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
    result = intake_gh.run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--search",
            f'label:"{config.queued_label}" state:open sort:created-asc',
            "--limit",
            str(page),
            "--json",
            "number,title,body,url,labels",
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


def verify_authorization(
    config: GitHubIntakeConfig,
    repo: str,
    number: int,
    root_dir: Path,
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
    result = intake_gh.run_gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={_AUTH_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
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
        payload = read_json(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return Authorization(
            ok=False,
            reason=f"unparseable authorization timeline: {exc}",
        )
    issue = (
        payload.get("data", {}).get("repository", {}).get("issue")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(issue, dict):
        return Authorization(
            ok=False,
            reason="authorization timeline had no issue node",
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
        ok=True,
        actor=actor,
        labeled_at=latest_at,
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
            planned.append(
                PlannedIssue(
                    issue,
                    Decision.SKIP_PROCESSED,
                    "already processed",
                )
            )
            continue
        if queue.find_by_source_ref(ref) is not None:
            planned.append(
                PlannedIssue(
                    issue,
                    Decision.SKIP_IN_QUEUE,
                    "already in the queue",
                )
            )
            continue
        if not issue.body.strip():
            planned.append(
                PlannedIssue(
                    issue,
                    Decision.SKIP_EMPTY_BODY,
                    "issue body is empty; nothing to build",
                )
            )
            continue
        if admitted >= config.max_items_per_sync:
            planned.append(
                PlannedIssue(
                    issue,
                    Decision.SKIP_CAP,
                    f"per-sync cap of {config.max_items_per_sync} reached",
                )
            )
            continue
        auth = authorizer(issue) if authorizer is not None else None
        refusal = authorization_refusal(config, auth)
        if refusal:
            planned.append(
                PlannedIssue(
                    issue,
                    Decision.REFUSE_UNAUTHORIZED,
                    refusal,
                    auth,
                )
            )
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
    result = intake_gh.run_gh(
        ["repo", "view", "--json", "nameWithOwner"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return "", f"could not resolve the checkout's repo: {result.error}"
    try:
        data = read_json(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return "", f"could not parse the checkout's repo: {exc}"
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(name, str) or name.count("/") != 1:
        return "", "`gh repo view` returned no usable nameWithOwner"
    return name, ""


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


def _resolve_sync_repos(
    config: GitHubIntakeConfig,
    root_dir: Path,
    local_repo: str | None,
) -> tuple[str, str]:
    """The inbox repo (`[intake_github] repo`) and an error, or `("", "")`.

    #231 B2. Extracted out of `sync` so that accepting a caller-resolved
    `local_repo` adds no branching to `sync` ITSELF: `sync` is already
    over its complexity gate and the ratchet forbids raising it further,
    while this function is new and starts under it. `local_repo=None`
    (every direct caller, including `ks queue sync`) resolves the
    checkout's own repo here exactly as `sync` used to inline; a
    non-``None`` value is `serve._run_intake`'s own resolution, already
    run once for steering's benefit, and is what deletes the SECOND
    `gh repo view` on a cycle where steering is also on - about 1440
    duplicate spawns/day at the default poll interval.

    The inbox must be the repo the factory will actually run against:
    `target_repo` is metadata, and `serve` always executes in `root_dir`.
    """
    repo, error = resolve_repo(config, root_dir)
    if error:
        return "", error
    if local_repo is None:
        local_repo, local_error = checkout_repo(config, root_dir)
        if local_error:
            return (
                repo,
                f"refusing to admit work without confirming the execution "
                f"repository: {local_error}",
            )
    if local_repo.lower() != repo.lower():
        return (
            repo,
            f"refusing cross-repository intake: the inbox is {repo} but this "
            f"checkout is {local_repo}, and a run would execute against "
            f"{local_repo}. Point [intake_github] repo at {local_repo}, or "
            "run the daemon from a checkout of the inbox repository.",
        )
    return repo, ""


def sync(
    queue: Queue,
    config: GitHubIntakeConfig,
    root_dir: Path,
    *,
    now_iso: str = "",
    verify: bool = True,
    commit_guard: Callable[[], AbstractContextManager[Any]] | None = None,
    local_repo: str | None = None,
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

    ``local_repo``: see :func:`_resolve_sync_repos`.
    """
    result = SyncResult()
    if not config.enabled:
        result.errors = ("[intake_github] enabled is false",)
        return result

    repo, error = _resolve_sync_repos(config, root_dir, local_repo)
    if repo:
        result.repo = repo
    if error:
        result.errors = (error,)
        return result

    ledger = ProcessedLedger(root_dir).load()

    # Authorization costs one API call per candidate, and the widening
    # loop below re-plans, so memoize per sync.
    checked: dict[int, Authorization] = {}

    def _authorize(issue: RemoteIssue) -> Authorization:
        if issue.number not in checked:
            checked[issue.number] = verify_authorization(
                config,
                repo,
                issue.number,
                root_dir,
            )
        return checked[issue.number]

    authorizer = _authorize if verify else None
    planned: list[PlannedIssue] = []
    polled_issues: list[RemoteIssue] = []
    # ---- network phase: NO lock is held here (#189 N1) ----
    for page_limit in poll_ladder(config):
        issues, poll_error, exhausted = poll_queued(
            config,
            repo,
            root_dir,
            limit=page_limit,
        )
        if poll_error:
            result.errors = (poll_error,)
            return result
        result.polled = len(issues)
        polled_issues = issues
        planned = plan_sync(
            queue,
            config,
            repo,
            issues,
            ledger,
            authorizer=authorizer,
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
        # same planner, so the two cannot disagree. Unlike steering's
        # `_act_on_one`, a dry run here consumes no admission cap.
        result.would_enqueue = tuple(
            entry.issue.source_ref(repo) for entry in planned if entry.decision.admits
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
            queue,
            config,
            repo,
            polled_issues,
            ledger,
            authorizer=authorizer,
        )
        result.planned = tuple(planned)
        result.skipped = {
            entry.issue.source_ref(repo): entry.reason
            for entry in planned
            if not entry.decision.admits
        }
        enqueued, errors = _commit_admissions(
            queue,
            config,
            repo,
            planned,
            ledger,
            stamp,
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
                design_acceptance=is_bug_report(issue),
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
                undone = f"AND the rollback failed ({undo_exc}); remove {item.item_id} by hand"
            errors.append(f"{ref}: could not record the dedupe entry ({exc}); {undone}")
            continue

        enqueued.append(ref)

    return enqueued, errors
