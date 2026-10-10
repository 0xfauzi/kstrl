"""Steering commands that an allowed actor writes as PR comments.

This module holds the parse of those comments and the effect of each command
on the queue.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import intake_gh
from kstrl.intake_gh import GitHubIntakeConfig, _gh_comment, _utc_now_iso
from kstrl.intake_issue import ProcessedLedger, _actor_allowed
from kstrl.jsonread import read_json
from kstrl.workqueue import Queue
from kstrl.workqueue_items import ItemSource, MergeDisposition, QueueError, QueueItem

if TYPE_CHECKING:
    from kstrl.operator_context import OperatorFile

#: R10.10. The two steering commands, spelled as the first whitespace
#: token of a comment body. A token match rather than the `"/memory "`
#: prefix the issue names, so `/iterate` with no text is a command (the
#: issue allows it) and `/memorywipe` is not. `_STEER_COMMANDS` itself is
#: derived from `_STEER_HANDLERS`, near the handlers, so the dispatch
#: table is the one place a third command is registered (#231 A3).
STEER_MEMORY = "/memory"

STEER_ITERATE = "/iterate"

#: Who may steer when `allowed_actors` is empty. GitHub's own
#: author_association vocabulary, upper case as the API returns it.
_STEER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# ---------------------------------------------------------------------------
# R10.10: the polled steering channel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteerCommand:
    """One `/memory` or `/iterate` comment on an open kstrl PR."""

    pr_number: int
    pr_url: str
    comment_id: int
    login: str
    association: str
    command: str
    text: str
    #: R10.10 C1. GitHub's own last-edit timestamp on the comment, ISO
    #: 8601 UTC (`"2026-09-02T16:11:25Z"`). The watermark this module
    #: persists is drawn only from values seen here - never from a clock
    #: reading - and an edit that changes it is exactly what re-surfaces
    #: an already-resolved comment.
    updated_at: str

    def ledger_key(self, repo: str) -> str:
        """The `ProcessedLedger` key. Prefixed so it cannot collide with
        an issue's `owner/name#123`, which is the other thing in that
        file."""
        return f"pr-comment:{repo}#{self.pr_number}:{self.comment_id}"


@dataclass(frozen=True)
class SteerOutcome:
    """What one command did, and whether the comment id may be recorded.

    `error` non-empty means a TRANSIENT failure - the file could not be
    written, the queue could not be added to. Nothing is recorded, so the
    next cycle retries. Everything else is terminal, including a refusal:
    a refusal re-posted every sixty seconds is worse than a refusal
    posted once.
    """

    comment: str = ""
    item_id: str = ""
    error: str = ""


@dataclass
class SteerResult:
    """What one steering poll did.

    `repo`, `applied`, `enqueued` and `errors` are read by
    `serve._run_intake`'s caller or `serve._run_steering`'s narration.
    `repo` is the exception: it is read only inside this module (by
    `_act_on_commands`'s ledger keys), kept on the result because it is
    also useful to a caller inspecting `poll_steering`'s return directly,
    the way tests do.
    """

    repo: str = ""
    applied: tuple[str, ...] = ()
    enqueued: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SteerContext:
    """Everything one steering poll's handlers need, built once.

    #231 B4. `_act_on_commands` and `_apply_one` were each an
    8-parameter signature, six of the parameters pure pass-through
    between them, and `commit_guard` was threaded five hops to be read
    only by `_requeue`. `memory_spec` is resolved ONCE here rather than
    once per command, which is also what fixes `_memory_spec` running a
    full `KstrlConfig.load(root_dir)` inside a leaf on the daemon poll
    path.
    """

    config: GitHubIntakeConfig
    root_dir: Path
    queue: Queue
    ledger: ProcessedLedger
    repo: str
    stamp: str
    commit_guard: Callable[[], AbstractContextManager[Any]] | None
    memory_spec: OperatorFile


def _pr_url(repo: str, number: int) -> str:
    """The PR's URL, constructed rather than fetched.

    Byte-identical to what `gh pr create` prints on stdout (which is what
    `kstrl/pr.py` records as `Component.pr_url`) and to
    `gh pr list --json url`; both measured. Requesting a third `--json`
    field would make `url` a required field of every PR row and would
    break ten fixtures in `tests/test_open_pr_counter.py` to obtain a
    string already in hand.
    """
    return f"https://github.com/{repo}/pull/{number}"


def _same_pr(recorded: str, url: str) -> bool:
    """Whether a recorded `pr_urls` entry is this PR.

    Casefolded and stripped of a trailing slash: GitHub owner and repo
    names are case-insensitive and `[intake_github] repo` may be typed in
    a case other than the one `gh pr create` printed. The comparison is
    on the WHOLE url, never on the number, so a PR #7 of another
    repository is not this one.
    """
    return recorded.strip().rstrip("/").casefold() == url.strip().rstrip("/").casefold()


def _is_comment_record(row: Any) -> bool:
    """Whether one raw row is a comment this module can act on.

    Validated entry by entry BEFORE anything is parsed, and the TYPE of
    each field is checked rather than its presence: `str(row["id"])` on a
    list would produce a ledger key that looks fine and dedupes nothing.
    `user` may be null - GitHub returns that for a deleted account - and
    a comment with no login cannot be authorised, which
    `_steer_refusal` refuses by name. `updated_at` (#231 C1) is required
    for the same reason `id` is: the watermark is built only from values
    validated here, never assumed.
    """
    return (
        isinstance(row, dict)
        and isinstance(row.get("id"), int)
        and not isinstance(row.get("id"), bool)
        and isinstance(row.get("body"), str)
        and isinstance(row.get("author_association"), str)
        and isinstance(row.get("updated_at"), str)
        and isinstance(row.get("user"), dict | None)
    )


def _parse_command(row: Any, number: int, url: str) -> SteerCommand | None:
    """One validated row as a command, or None when it is ordinary prose."""
    parts = str(row["body"]).strip().split(None, 1)
    if not parts or parts[0] not in _STEER_COMMANDS:
        return None
    user = row.get("user")
    login = user.get("login", "") if isinstance(user, dict) else ""
    return SteerCommand(
        pr_number=number,
        pr_url=url,
        comment_id=int(row["id"]),
        login=login if isinstance(login, str) else "",
        association=str(row["author_association"]).strip().upper(),
        command=parts[0],
        text=parts[1].strip() if len(parts) > 1 else "",
        updated_at=str(row["updated_at"]),
    )


def _pr_steering_commands(
    config: GitHubIntakeConfig,
    repo: str,
    number: int,
    root_dir: Path,
    since: str,
) -> tuple[list[SteerCommand], tuple[str, ...], str]:
    """Every steering command on one PR, plus every validated comment's
    `updated_at`, or an error string.

    PR comments ARE issue comments, so this is the issues endpoint.
    `--paginate` merges the pages into one JSON array (measured against
    gh 2.73.0 on a three-page response). "Oldest first" is no longer
    load-bearing for the watermark (#231 C1-fix-a: the fetch is ordered
    by `created_at` while `since` filters on `updated_at`, so a caller
    that trusted fetch order to mean `updated_at` order was wrong), but
    it costs no sort either way, so it is left as GitHub returns it.

    `since` (#231 C1): appended as a query parameter when non-empty. It
    is the persisted watermark, and GitHub filters the endpoint to
    comments whose `updated_at` is at or after it, which is safe
    precisely because it keys on `updated_at` and not `created_at`: a
    `/memory` edited after the cut is fetched again. Measured on this
    repository: 130477 bytes for one PR's three comments with no
    `since`, 2 bytes with a `since` after all three.

    The second return value is `updated_at` for EVERY validated row,
    commands and ordinary prose alike (#231 C1-fix-b: a row that never
    parses as a command still counts as "seen" and, being trivially
    resolved, can still advance the watermark - the earlier version fed
    the watermark only from parsed commands, so a PR carrying nothing
    but review prose was refetched in full every cycle, which is
    exactly the case the 130477-byte measurement came from).

    A payload this cannot read is an ERROR, never an empty list: a gate
    that counts what it could not parse as zero is the fail-open shape
    this repository keeps finding. `except Exception` and not a tuple of
    names, because `json.loads` raises `RecursionError` on deeply nested
    input and that is a `RuntimeError`, not a `ValueError` (#318).
    """
    endpoint = f"repos/{repo}/issues/{number}/comments?per_page=100"
    if since:
        endpoint = f"{endpoint}&since={since}"
    result = intake_gh.run_gh(
        ["api", endpoint, "--paginate"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return [], (), f"could not read comments on PR #{number}: {result.error}"
    try:
        rows = read_json(result.stdout or "[]")
    except Exception as exc:  # noqa: BLE001 - the parser's taxonomy is the parser's
        return [], (), f"comments on PR #{number} were unparseable: {exc}"
    if not isinstance(rows, list):
        return [], (), f"comments on PR #{number} returned {type(rows).__name__}, expected a list"
    # #231 B8: loop-invariant (repo and number are both fixed for this
    # call), hoisted out of the loop below.
    url = _pr_url(repo, number)
    commands: list[SteerCommand] = []
    seen: list[str] = []
    for index, row in enumerate(rows):
        if not _is_comment_record(row):
            return (
                [],
                (),
                f"comment row {index} on PR #{number} is not a comment record: {row!r:.120}",
            )
        seen.append(str(row["updated_at"]))
        parsed = _parse_command(row, number, url)
        if parsed is not None:
            commands.append(parsed)
    return commands, tuple(seen), ""


def _steer_refusal(config: GitHubIntakeConfig, cmd: SteerCommand) -> str:
    """Why this comment may not steer, or "" when it may.

    Reuses #188's `allowed_actors` rather than declaring a second list:
    two definitions of who may make this daemon act means the weaker one
    is the one the gate consults. Empty falls back to GitHub's
    `author_association`, which is the inherited-permission behaviour
    #188's module docstring bounds.
    """
    if not cmd.login:
        return f"comment {cmd.comment_id} has no author login, so it cannot be authorised"
    if config.allowed_actors:
        if not _actor_allowed(config, cmd.login):
            return (
                f"@{cmd.login} is not in [intake_github] allowed_actors "
                f"({', '.join(config.allowed_actors)})"
            )
        return ""
    if cmd.association not in _STEER_ASSOCIATIONS:
        return (
            f"@{cmd.login} has author_association {cmd.association or 'NONE'}, "
            f"not one of {', '.join(sorted(_STEER_ASSOCIATIONS))}"
        )
    return ""


def _memory_spec(root_dir: Path) -> OperatorFile:
    """The memory file, resolved the ONE way every other reader resolves it.

    `operator_context.operator_file_spec` is where that resolution lives,
    and its docstring records what a second answer cost: the parent and
    the worker read different files. This writer uses the same one.
    Called once per poll, by `poll_steering`, into `SteerContext.memory_spec`
    - not once per command, which is what it cost before #231's simplify
    pass (B4).
    """
    from kstrl.config import KstrlConfig
    from kstrl.operator_context import MEMORY, operator_file_spec

    return operator_file_spec(MEMORY, root_dir, KstrlConfig.load(root_dir).memory_file)


def _guidance_record(cmd: SteerCommand) -> str:
    """The one line `/memory` appends: the text, its provenance, its date."""
    return f"- {cmd.text} (from PR #{cmd.pr_number} by @{cmd.login}, {_utc_now_iso()[:10]})"


def _recorded_comment(spec: OperatorFile, cmd: SteerCommand) -> str:
    """The acknowledgement. Says where it went and that it is not committed."""
    return f'Recorded to {spec.display}: "{cmd.text}". Commit it to keep it.'


def _record_memory(
    ctx: SteerContext,
    cmd: SteerCommand,
    *,
    refusal_prefix: str,
) -> tuple[SteerOutcome, bool]:
    """Validate and append `cmd.text` under `## Guidance`. `(outcome, ok)`.

    #231 B5. `_memory_outcome` and `_iterate_outcome` used to re-run the
    same four steps - refusal check, resolve the spec, append, build the
    acknowledgement - differing only in the refusal's prefix, which is
    the one thing each caller still supplies. `ok` is True only when the
    append succeeded, the branch `_iterate_outcome` continues past to
    requeue; `_memory_outcome` returns `outcome` either way.
    """
    from kstrl.operator_context import append_guidance_record, memory_text_refusal

    refusal = memory_text_refusal(cmd.text)
    if refusal:
        return SteerOutcome(comment=f"{refusal_prefix}{refusal}"), False
    error = append_guidance_record(ctx.memory_spec, _guidance_record(cmd))
    if error:
        return SteerOutcome(error=error), False
    return SteerOutcome(comment=_recorded_comment(ctx.memory_spec, cmd)), True


def _memory_outcome(ctx: SteerContext, cmd: SteerCommand) -> SteerOutcome:
    outcome, _ok = _record_memory(ctx, cmd, refusal_prefix="Not recorded: ")
    return outcome


def _item_for_pr(queue: Queue, url: str) -> QueueItem | None:
    """The queue item whose run produced this PR, in any state.

    A linear scan because the queue is small and `pr_urls` is not
    indexed. Matched on the WHOLE URL: matching on the number alone would
    re-run another repository's work.
    """
    for item in queue.items():
        for recorded in item.pr_urls:
            if _same_pr(recorded, url):
                return item
    return None


def _requeue(
    queue: Queue,
    original: QueueItem,
    cmd: SteerCommand,
    commit_guard: Callable[[], AbstractContextManager[Any]] | None,
) -> QueueItem:
    """Enqueue a re-run of `original`. The new item is an ORDINARY item.

    `ItemSource.LOCAL` and not GITHUB: `report_outcome` maps a source_ref
    to an issue by partitioning on the FIRST `#`, so the derived ref
    `owner/name#123#iterate-111` gives it `123#iterate-111`, `int()`
    fails, and every terminal transition would warn `cannot map ... to a
    GitHub issue`. The origin is in `source_ref` and in the
    acknowledgement, so nothing is lost.

    `STOP_AT_PR` unconditionally rather than copied from the original:
    the trigger was a remote comment, and R8.6's rule is that remotely
    triggered work never deletes the human merge gate.

    The guard covers the local write only, never the network work above
    it (#189 N1).
    """
    spec_text = queue.read_spec(original)
    guard = commit_guard() if commit_guard is not None else nullcontext()
    with guard:
        return queue.add(
            spec_text,
            title=original.title,
            priority=original.priority,
            merge_disposition=MergeDisposition.STOP_AT_PR,
            source=ItemSource.LOCAL,
            source_ref=f"{original.source_ref or original.item_id}#iterate-{cmd.comment_id}",
            target_repo=original.target_repo,
            project_name=original.project_name,
            max_attempts=original.max_attempts,
            spec_filename=original.spec_filename,
            design_acceptance=original.design_acceptance,
            actor="steer",
        )


def _iterate_outcome(ctx: SteerContext, cmd: SteerCommand) -> SteerOutcome:
    """`/memory` first, then re-queue. An empty text skips the memory step."""
    lines: list[str] = []
    if cmd.text:
        outcome, ok = _record_memory(ctx, cmd, refusal_prefix="Not recorded and not queued: ")
        if not ok:
            return outcome
        lines.append(outcome.comment)
    original = _item_for_pr(ctx.queue, cmd.pr_url)
    if original is None:
        lines.append("Cannot iterate: no queue item recorded this PR")
        return SteerOutcome(comment="\n".join(lines))
    try:
        item = _requeue(ctx.queue, original, cmd, ctx.commit_guard)
    except (QueueError, OSError, ValueError) as exc:
        return SteerOutcome(error=f"could not re-queue {original.item_id}: {exc}")
    lines.append(
        f"Queued a re-run as {item.item_id}; it starts once this PR is "
        "merged or closed (open-PR bound)."
    )
    return SteerOutcome(comment="\n".join(lines), item_id=item.item_id)


#: R10.10 A3. Closed by construction: `_STEER_COMMANDS` (what
#: `_parse_command` treats as a command at all) is DERIVED from this
#: mapping's keys rather than declared separately, so a command
#: registered here is automatically a member of the set `_parse_command`
#: recognises, and a command `_parse_command` could recognise but this
#: mapping does not is a `KeyError` in `_run_command`, not a silent fall
#: through to `/iterate` - the most expensive thing this subsystem can
#: do, since it re-queues a run. There is no `else` branch left to fall
#: into.
_STEER_HANDLERS: dict[str, Callable[[SteerContext, SteerCommand], SteerOutcome]] = {
    STEER_MEMORY: _memory_outcome,
    STEER_ITERATE: _iterate_outcome,
}

_STEER_COMMANDS = frozenset(_STEER_HANDLERS)


def _run_command(ctx: SteerContext, cmd: SteerCommand) -> SteerOutcome:
    try:
        handler = _STEER_HANDLERS[cmd.command]
    except KeyError:
        raise RuntimeError(
            f"no steering handler registered for {cmd.command!r}; refusing rather "
            "than silently falling through to /iterate, which would re-queue work"
        ) from None
    return handler(ctx, cmd)


def _post_pr_comment(ctx: SteerContext, number: int, body: str) -> str:
    """Acknowledge on the PR. Returns an error string, or "".

    NOT through `post_comment`: that one is gated on
    `comment_on_result`, which governs the issue-verdict writeback. An
    acknowledgement is the only feedback the commenter gets, and
    suppressing it would make a command that ran look ignored.
    """
    if not body:
        return ""
    return _gh_comment("pr", "PR", ctx.config, ctx.repo, number, body, ctx.root_dir)


def _apply_one(ctx: SteerContext, cmd: SteerCommand, result: SteerResult) -> bool:
    """Run one command, record it, acknowledge it. In that order.

    Returns whether it RESOLVED - was recorded in the ledger - which
    `_act_on_commands` needs to decide whether this comment may advance
    its PR's watermark (#231 C1).

    The ledger is written AFTER the action succeeded and BEFORE the
    acknowledgement, and the order is the whole retry story: a failed
    write leaves the id unrecorded so the next cycle tries again, while a
    failed acknowledgement does not cause the memory line to be written
    twice. The durable artifact is the file; the comment is best effort.
    """
    outcome = _run_command(ctx, cmd)
    key = cmd.ledger_key(result.repo)
    if outcome.error:
        result.errors += (outcome.error,)
        return False
    ctx.ledger.record(key, item_id=outcome.item_id, when=ctx.stamp)
    result.applied += (key,)
    if outcome.item_id:
        result.enqueued += (outcome.item_id,)
    post_error = _post_pr_comment(ctx, cmd.pr_number, outcome.comment)
    if post_error:
        result.errors += (post_error,)
    return True


def _compute_watermarks(
    seen_by_pr: Mapping[int, Sequence[str]],
    unresolved_by_pr: Mapping[int, Sequence[str]],
) -> dict[int, str]:
    """The per-PR steering watermark this cycle earned (#231 C1, corrected
    2026-09-19).

    Ordered by `updated_at` VALUE, never by position in the fetched
    list (C1-fix-a): the fetch is ascending by `created_at` while
    `since` filters on `updated_at`, so an older comment edited after a
    newer one was created sits earlier in fetch order and later in
    `updated_at` order. A rule that blocked "every later comment in
    fetch order" once an unresolved one was seen could therefore have
    already advanced the watermark past that same unresolved comment's
    `updated_at` from an earlier, resolved row - skipping it forever.

    `seen_by_pr` is every validated comment's `updated_at`, commands and
    ordinary prose alike (C1-fix-b: only parsed commands used to feed
    this and a prose-only PR never got a watermark at all).
    `unresolved_by_pr` is the `updated_at` of every command that did NOT
    resolve this cycle (capped, dry-run, or errored).

    For each PR: with nothing unresolved, the watermark is the greatest
    of everything seen. With something unresolved, it is the greatest
    SEEN value strictly less than the LEAST unresolved value - strict,
    because GitHub's `since` is inclusive, so a watermark equal to an
    unresolved comment's `updated_at` would still skip it. A PR with no
    such value (including one with nothing seen at all) is OMITTED
    entirely, leaving its persisted watermark unchanged.
    """
    watermarks: dict[int, str] = {}
    for number, seen in seen_by_pr.items():
        unresolved = unresolved_by_pr.get(number, ())
        if unresolved:
            floor = min(unresolved)
            eligible = [t for t in seen if t < floor]
        else:
            eligible = list(seen)
        if eligible:
            watermarks[number] = max(eligible)
    return watermarks


def _act_on_one(
    ctx: SteerContext,
    cmd: SteerCommand,
    result: SteerResult,
    acted: int,
) -> tuple[bool | None, bool]:
    """One command through the gate: seen, authorised, capped, dry run, act.

    Returns `(resolved, consumed_cap)`. `resolved` is what
    `_act_on_commands`'s docstring means for the watermark: `True`
    resolved, `False` did not, `None` means "already seen", which is
    resolved too but must not itself increment `acted` a second time.
    `consumed_cap` is whether THIS call should count against
    `max_items_per_sync` on the NEXT one - only a dry-run observation or
    a real attempt does, the same two branches the cap check itself
    guards, which is what keeps this and the cap check from disagreeing.
    """
    key = cmd.ledger_key(result.repo)
    if ctx.ledger.contains(key):
        return None, False
    reason = _steer_refusal(ctx.config, cmd)
    if reason:
        result.skipped[key] = reason
        return True, False
    if acted >= ctx.config.max_items_per_sync:
        result.skipped[key] = "the per-cycle cap is full; it waits for the next cycle"
        return False, False
    # A dry run CONSUMES the cap here, deliberately: the preview has to show
    # what a real cycle would do, cap included. `sync` differs, since its dry
    # run consumes no admission cap, and that divergence is recorded rather
    # than reconciled, because reconciling it means the shared gate
    # abstraction neither channel has yet.
    if ctx.config.dry_run:
        result.skipped[key] = f"dry run: would apply {cmd.command} from comment {cmd.comment_id}"
        return False, True
    return _apply_one(ctx, cmd, result), True


def _act_on_commands(
    ctx: SteerContext,
    commands: Sequence[SteerCommand],
    seen_by_pr: Mapping[int, Sequence[str]],
    result: SteerResult,
) -> dict[int, str]:
    """The per-comment gate order: seen, authorised, capped, dry run, act.

    An already-recorded comment and an unauthorised one do NOT consume
    the cap, for the same reason a skipped issue does not consume the
    admission cap (#187 F6): a hundred comments kstrl will never act on
    must not crowd out the one it will.

    Returns the per-PR watermark THIS CYCLE earned, via
    `_compute_watermarks` (#231 C1, corrected 2026-09-19): every command
    that did not resolve (capped, dry-run, or errored) is collected by
    PR number and handed to it alongside `seen_by_pr`, which carries
    every validated comment's `updated_at` regardless of whether it
    parsed as a command.
    """
    acted = 0
    unresolved_by_pr: dict[int, list[str]] = {}
    for cmd in commands:
        resolved, consumed_cap = _act_on_one(ctx, cmd, result, acted)
        if consumed_cap:
            acted += 1
        if resolved is False:
            unresolved_by_pr.setdefault(cmd.pr_number, []).append(cmd.updated_at)
    return _compute_watermarks(seen_by_pr, unresolved_by_pr)


def poll_steering(
    config: GitHubIntakeConfig,
    root_dir: Path,
    queue: Queue,
    *,
    repo: str,
    marked_numbers: tuple[int, ...],
    commit_guard: Callable[[], AbstractContextManager[Any]] | None = None,
) -> SteerResult:
    """Act on `/memory` and `/iterate` comments on open kstrl PRs (R10.10).

    Polling, not a webhook. Inbound HTTP is an explicit non-goal
    (`docs/dark-factory-roadmap.md`), and `ks serve` already polls
    GitHub every cycle; reading one more endpoint changes nothing about
    that decision.

    `repo` and `marked_numbers` are RESOLVED BY THE CALLER
    (`serve._run_intake`), not by this function: #231's simplify pass
    (B2) deleted this module's own `checkout_repo` call and its deferred
    `from kstrl.serve import count_open_kstrl_prs` (the inward import
    that made `serve` import `intake_github` import `serve`), because
    both were a SECOND `gh repo view` / `gh pr list` on any cycle where
    issue intake was also on - `_run_intake` now resolves each once and
    shares the answer with `_sync_remote_issues` too.

    Strictly additive, like the rest of this module: every failure
    returns as a value in `errors` and the cycle continues.

    `commit_guard` is entered ONLY around `Queue.add`, never around the
    network work, for the reason #189 N1 records: the daemon once held
    the queue mutex across a slow GitHub and blocked `ks queue pause`.

    The `steer_enabled` return is FIRST, above every I/O call and above
    the `ProcessedLedger` construction, because `ProcessedLedger.load`
    calls `ensure_control_state`, which CREATES the XDG control
    directory. A feature that is off must leave no trace. This is also
    the only copy of that check: `serve._run_steering` calls this
    unconditionally, so there is one gate rather than two a later edit
    could delete the wrong one of.

    The ledger is built here rather than taken as a parameter, the way
    `sync` builds its own. Passing it in would put
    `ProcessedLedger(root_dir).load()` in `kstrl/serve.py`, where
    `tests/test_serve_config_reads.py` sorts every `.load` call by
    receiver and asserts the undecided bucket is empty.
    """
    result = SteerResult()
    if not config.steer_enabled:
        return result
    result.repo = repo
    ledger = ProcessedLedger(root_dir).load()
    commands: list[SteerCommand] = []
    seen_by_pr: dict[int, tuple[str, ...]] = {}
    for number in marked_numbers:
        since = ledger.watermark(f"{repo}#{number}")
        found, seen, parse_error = _pr_steering_commands(config, repo, number, root_dir, since)
        if parse_error:
            result.errors += (parse_error,)
            continue
        commands.extend(found)
        if seen:
            seen_by_pr[number] = seen
    ctx = SteerContext(
        config=config,
        root_dir=root_dir,
        queue=queue,
        ledger=ledger,
        repo=repo,
        stamp=_utc_now_iso(),
        commit_guard=commit_guard,
        memory_spec=_memory_spec(root_dir),
    )
    watermarks = _act_on_commands(ctx, commands, seen_by_pr, result)
    for number, value in watermarks.items():
        ledger.set_watermark(f"{repo}#{number}", value)
    return result
