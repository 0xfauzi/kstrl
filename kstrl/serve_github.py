"""The GitHub reads of a cycle: the open-PR bound and its count streak, issue intake, and
steering.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from kstrl.atomicio import atomic_write_json
from kstrl.jsonread import read_json
from kstrl.pr import GH_TIMEOUT
from kstrl.pr_body import PR_FOOTER_MARKER
from kstrl.serve_config import Admission, ServeConfig, ServeObserver
from kstrl.statedir import (
    CONTROL_PR_COUNT_STREAK,
    ControlStateError,
    control_file,
    control_lock,
    ensure_control_state,
)
from kstrl.workqueue import Queue
from kstrl.workqueue_store import queue_lock

if TYPE_CHECKING:
    from kstrl.intake_gh import GitHubIntakeConfig


@dataclass(frozen=True)
class OpenPrCount:
    """How many open kstrl PRs were seen, and whether the page was full.

    ``saturated`` travels with the count because a full page makes the
    count a LOWER BOUND rather than a count: ``gh pr list`` returns at
    most ``--limit`` rows, newest first, so an older unmerged kstrl PR
    can sit outside the window. A gate that saw only the number could
    not tell "zero open" from "zero within the newest hundred", and the
    second one is exactly the condition the bound exists for. Hence a
    pair, not an ``int``.

    ``count`` is a property over ``marked_numbers`` rather than a stored
    field, so the two cannot be constructed out of agreement.
    ``tests/test_flow_control.py`` once built a fake with a non-zero
    ``count`` and an empty ``marked_numbers``, which was uncatchable
    while both were independent fields.
    """

    saturated: bool
    #: The numbers of the PRs the marker matched, in payload order. R10.10
    #: reads comments on exactly these PRs: the list and the count come
    #: from ONE pass over ONE payload, so a second lister would be a
    #: second answer to "which PRs are kstrl's".
    marked_numbers: tuple[int, ...] = ()

    @property
    def count(self) -> int:
        return len(self.marked_numbers)


def count_open_kstrl_prs(cwd: Path, *, limit: int = 100) -> OpenPrCount:
    """Open PRs whose body ENDS with :data:`PR_FOOTER_MARKER` (R10.7).

    Runs ``gh pr list --state=open --limit <limit> --json number,body``
    under ``GH_TIMEOUT``. Every failure this function CONVERTS is raised
    as ``RuntimeError`` - gh failure, timeout, missing binary,
    unparseable output, unrecognised row - because the caller decides
    what a failed count means and an unknown number of open PRs is not
    zero. That is not a promise that nothing else escapes, and a caller
    must still catch ``Exception``: ``run_gh``'s decode raises
    ``UnicodeDecodeError`` and ``json.loads`` raises ``RecursionError``
    on deeply nested input, neither of which is converted here and
    neither of which is a ``RuntimeError`` (#318). ``check_open_pr_bound``
    catches ``Exception`` for exactly that reason.

    ``--state=open`` is deliberately one argv token. Spelled as two, the
    literal ``"open"`` lands in ``kstrl/serve.py`` and is counted by the
    encoding census in ``tests/test_encoding_readers.py``, which nets
    every expression folding to ``read_text`` or ``open``. That would let
    a later commit add a genuine read and drop this flag with the census
    count unmoved.

    Anchored at the END of the body rather than matched anywhere in it.
    Both writers in ``kstrl/pr.py`` append the marker as the final line
    (``tests/test_flow_control.py`` pins that through the real writer),
    so the anchor costs no true positive and rejects every PR that
    merely quotes the footer in prose - a body discussing the constant,
    a human pasting it. A substring match counted PR #354 of this
    repository, which no kstrl opened, as a kstrl PR.

    Two directions, both stated. FALSE NEGATIVE: a PR opened before the
    footer took its current wording is not counted (the literal was a
    different one until commit ``bd19f2b``), and neither is one whose
    body a human edited. FALSE POSITIVE: a PR whose body was written by
    hand to end with exactly this line.

    Rows are validated one by one before anything is counted. A payload
    whose rows are not PR records is a refusal with the row's index, not
    a count of zero: the ``isinstance`` filter this replaced read as
    defensive and was in fact the fail-open.

    The spawn and its four transport failures belong to
    :func:`~kstrl.intake_gh.run_gh`, which is the package's one gh
    invocation. Only the shapes of a bad PAYLOAD are decided here.

    ``number`` is now validated too, alongside ``body``, because R10.10's
    polled steering channel reads comments on exactly the PRs this
    returns as ``marked_numbers``: a row whose number is absent or the
    wrong type must refuse rather than be silently skipped.
    """
    from kstrl.intake_gh import run_gh

    args = ["pr", "list", "--state=open", "--limit", str(limit), "--json", "number,body"]
    result = run_gh(args, timeout=GH_TIMEOUT, cwd=cwd)
    if not result.ok:
        raise RuntimeError(result.error)
    try:
        rows = read_json(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"gh pr list returned unparseable JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise RuntimeError(f"gh pr list returned {type(rows).__name__}, expected a list")
    marked: list[int] = []
    for index, row in enumerate(rows):
        # The TYPE of ``body`` is checked, not just its presence. A
        # validator that admits a field the parser then coerces is the
        # fail-open one field over: ``str(row["body"] or "")`` turns
        # ``42`` into ``"42"`` and ``["x"]`` into ``"['x']"``, and both
        # would have been counted as unmarked PRs rather than refused.
        # ``gh pr list --json body`` returns a string or null today; the
        # only scenario this validation is for is the one where that
        # changes. ``bool`` is excluded from the ``number`` check because
        # ``isinstance(True, int)`` is true.
        if (
            not isinstance(row, dict)
            or "body" not in row
            or not isinstance(row["body"], str | None)
            or not isinstance(row.get("number"), int)
            or isinstance(row.get("number"), bool)
        ):
            raise RuntimeError(f"gh pr list row {index} is not a PR record: {row!r:.120}")
        if (row["body"] or "").rstrip().endswith(PR_FOOTER_MARKER):
            marked.append(row["number"])
    return OpenPrCount(
        saturated=len(rows) >= limit,
        marked_numbers=tuple(marked),
    )


#: Consecutive inconclusive counts before the daemon files an item.
STREAK_THRESHOLD: Final = 3


#: Shape of ``pr_count_streak.json``. Bumped when a field changes
#: meaning; a file carrying any other value is treated as damaged rather
#: than partially read.
STREAK_SCHEMA_VERSION: Final = 1


def _warn_streak_state(path: Path, reason: str) -> str:
    """Warn that the persisted streak was rejected; return the sentence.

    Warned here and returned, so ``serve`` can put the identical text on
    the observer an operator is actually watching. Same shape as
    ``autonomy._warn_rejected_state``.
    """
    message = (
        f"serve: rejected the open-PR count streak {path} ({reason}); "
        "treating the count as already at its alarm threshold so the next "
        "unusable count files an inbox item, and leaving the file alone"
    )
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    return message


@dataclass
class OpenPrCountStreak:
    """Consecutive inconclusive open-PR counts, persisted across processes.

    It exists to file ONE inbox item once the count has been unusable
    ``threshold`` polls running. A rate limit or a brief outage clears
    itself and never reaches the threshold; an expired ``gh`` token and a
    ``gh`` missing from launchd's PATH do not clear themselves, and
    before this the daemon waited on them forever with every operator
    surface reporting healthy.

    ON DISK because ``ks serve`` ships in two deployment shapes and only
    one of them is a long-lived process. ``--plist-mode keepalive`` runs
    one process that polls; ``--plist-mode interval`` runs
    ``ks serve --once`` on a ``StartCalendarInterval``, one process per
    firing. A streak that lived in the loop's frame started at 0 on every
    interval firing, so ``consecutive`` never exceeded 1, the threshold
    was unreachable, and the alarm did not exist in the mode most exposed
    to the failure it names - a ``gh`` missing from launchd's PATH.

    ``filed`` is set by :func:`_record_count_failure` AFTER the item is
    on disk, never by the poll that merely decided to file: the inbox
    write is deliberately failure-swallowing, so marking the streak first
    would throw the alarm away at the moment it is needed.
    :meth:`record_conclusive` clears both, because the next failure after
    a recovery is new information.

    ``reason`` and ``damaged`` are transient, not persisted. ``reason``
    is what the LAST inconclusive count said, and it is what the filed
    item quotes: the caller's own refusal string may have come from a
    different wait gate entirely, and an item that names the wrong cause
    is worse than none. It is also what makes filing conditional on this
    poll having actually failed to count.
    """

    threshold: int = STREAK_THRESHOLD
    consecutive: int = 0
    filed: bool = False
    reason: str = ""
    damaged: str | None = None

    def record_inconclusive(self, reason: str) -> None:
        self.consecutive += 1
        self.reason = reason

    def record_conclusive(self) -> None:
        self.consecutive = 0
        self.filed = False
        self.reason = ""

    def should_file(self) -> bool:
        """Whether THIS poll is the one that files. Asks; marks nothing.

        A predicate, not a transition. The version that set ``filed``
        here read as one atomic step and was not: the write it authorised
        happens afterwards and can fail, and the streak had already
        disarmed itself by then.

        ``reason`` non-empty is what makes this a question about the
        COUNT. Any wait gate's refusal reaches ``_record_count_failure``,
        and a streak restored at its threshold from a damaged file would
        otherwise file an open-PR-count item on the strength of a full
        inbox.
        """
        return bool(self.reason) and not self.filed and self.consecutive >= self.threshold

    @staticmethod
    def path_for(root_dir: Path) -> Path:
        return control_file(root_dir, CONTROL_PR_COUNT_STREAK)

    @classmethod
    def load(cls, root_dir: Path) -> OpenPrCountStreak:
        """Read the streak, failing toward the ALARM rather than silence.

        A missing file is first run and is streak 0: nothing has failed
        yet, and treating that as an alarm would file an item on every
        fresh install.

        Anything else unreadable - bad permissions, bytes that are not
        utf-8, JSON that will not parse, a shape or a schema version this
        code does not recognise - comes back AT the threshold with
        ``damaged`` set. The safe direction here is the opposite of
        autonomy's: this file's whole job is to make a silent failure
        loud, so a version of it we cannot read must not quietly restart
        the count from zero and re-earn three polls of silence. The next
        unusable count files immediately.

        The file is NOT rewritten while damaged; see :meth:`save`.
        """
        ensure_control_state(root_dir)
        path = cls.path_for(root_dir)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            # ``ValueError`` beside ``OSError`` because
            # ``UnicodeDecodeError`` is one and would otherwise escape a
            # handler written to fail closed (#291).
            return cls._damaged(path, f"unreadable: {exc}")
        try:
            data = read_json(raw)
        except ValueError as exc:
            return cls._damaged(path, f"malformed JSON: {exc}")
        return cls._from_payload(path, data)

    @classmethod
    def _from_payload(cls, path: Path, data: Any) -> OpenPrCountStreak:
        """Validate every field, or return the damaged streak saying which."""
        if not isinstance(data, dict):
            return cls._damaged(path, "top-level value is not an object")
        version = data.get("schema_version")
        if version != STREAK_SCHEMA_VERSION:
            return cls._damaged(
                path,
                f"schema_version is {version!r}, expected {STREAK_SCHEMA_VERSION}",
            )
        consecutive = data.get("consecutive")
        # ``bool`` is an ``int`` subclass, so ``True`` would otherwise
        # validate as a count of 1.
        if not isinstance(consecutive, int) or isinstance(consecutive, bool) or consecutive < 0:
            return cls._damaged(path, f"consecutive is not a count: {consecutive!r:.60}")
        filed = data.get("filed")
        if not isinstance(filed, bool):
            return cls._damaged(path, f"filed is not a boolean: {filed!r:.60}")
        return cls(consecutive=consecutive, filed=filed)

    @classmethod
    def _damaged(cls, path: Path, reason: str) -> OpenPrCountStreak:
        return cls(
            consecutive=STREAK_THRESHOLD,
            filed=False,
            damaged=_warn_streak_state(path, reason),
        )

    def save(self, root_dir: Path) -> str | None:
        """Persist the streak. Returns None on success, else the reason.

        REFUSES while ``damaged``. Writing a fresh count over bytes an
        operator could have inspected destroys the only evidence of the
        damage, and the next load would then find a clean file and report
        nothing. The refusal is re-reported on every save attempt, keyed
        by the file and the cause, so it does not need a record of its
        own.

        A failed write is reported and not raised. The daemon has no
        per-cycle handler, and dying over a bookkeeping file would be a
        worse outcome than the degraded alarm it causes: without the
        write, the streak is per-process again, which is the behaviour
        this file replaced. It is not the only mechanism that would
        notice - an unwritable control directory also fails
        ``SpendLedger`` on the money path, which raises.
        """
        if self.damaged is not None:
            return self.damaged
        path = self.path_for(root_dir)
        payload = {
            "schema_version": STREAK_SCHEMA_VERSION,
            "consecutive": self.consecutive,
            "filed": self.filed,
        }
        try:
            ensure_control_state(root_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            with control_lock(root_dir):
                atomic_write_json(path, payload)
        except (OSError, ControlStateError) as exc:
            return (
                f"serve: could not persist the open-PR count streak {path}: {exc}. "
                "The count restarts at zero in the next process, so launchd "
                "`interval` mode will not reach its alarm threshold."
            )
        return None


def check_open_pr_bound(
    config: ServeConfig,
    root_dir: Path,
    *,
    counter: Callable[[Path], OpenPrCount] | None = None,
    streak: OpenPrCountStreak | None = None,
) -> Admission:
    """Flow control: refuse admission while max_open_prs kstrl PRs are open.

    Skipped (allowed) when ``max_open_prs`` is 0, or when the factory
    will not open PRs at all (``[factory] create_prs = false``), because
    then there is nothing to bound. Anything that is not a usable count
    refuses admission with the error in the reason: an unknown number of
    open PRs is not zero.

    ``except Exception`` on purpose, and the config read is INSIDE it.
    Every outcome of this block that is not a number means one thing to
    this gate, and an enumeration of the exception types believed
    reachable is the defect rather than the precaution (#318). Two were
    measured escaping the previous ``except RuntimeError``, both fatal to
    a daemon that has no per-cycle handler: a ``ValueError`` from
    ``FactoryConfig.load`` on ``[factory] max_parallel = "two"``, which
    this gate re-reads every poll while only startup validates it, and a
    ``UnicodeDecodeError`` from ``run_gh``'s locale decode.

    A saturated page refuses in both directions and says which. ``count
    >= max_open_prs`` is conclusive even on a full page, so it keeps the
    ordinary reason; ``count < max_open_prs`` on a full page is not a
    fact about the repository and must not admit.

    All refusals are WAITS, not pauses. ``pause_reason`` stays empty so
    the daemon re-checks next cycle: an open PR lifts the refusal by
    being merged or closed, which needs no operator action on the queue.
    A count that stays unusable is the case a wait alone handles badly,
    which is what ``streak`` is for; see :class:`OpenPrCountStreak`.

    ``counter`` exists so tests inject a fake without putting a ``gh``
    on PATH; it is resolved inside the body rather than as a default
    argument so patching the module-level name reaches it.

    ``streak`` defaults to a throwaway rather than to ``None``. The four
    ``if streak is not None`` guards this replaced were four branches
    whose only job was to decide whether to record, and every one of
    them was a place a later edit could forget. A caller that does not
    pass one gets a streak nobody reads, which is what ``None`` meant.

    BOTH early-allow paths record a CONCLUSIVE result. A gate that is
    switched off, or that does not apply because the factory opens no
    pull requests, is not evidence that ``gh`` is broken; leaving a
    half-built streak standing across a week of ``max_open_prs = 0``
    would file on the first failure afterwards and contradict the
    "three consecutive polls" contract in the docs.
    """
    tally = streak if streak is not None else OpenPrCountStreak()
    if config.max_open_prs == 0:
        tally.record_conclusive()
        return Admission(allowed=True, reason="open-PR bound disabled")
    count_fn = counter if counter is not None else count_open_kstrl_prs
    try:
        from kstrl.factory_config import FactoryConfig

        if not FactoryConfig.load(root_dir).create_prs:
            tally.record_conclusive()
            return Admission(
                allowed=True,
                reason="open-PR bound not applicable (create_prs = false)",
            )
        counted = count_fn(root_dir)
    except Exception as exc:  # noqa: BLE001 - anything but a count is the same answer
        reason = f"cannot count open kstrl PRs: {exc}"
        tally.record_inconclusive(reason)
        return Admission(allowed=False, reason=reason)
    if counted.count >= config.max_open_prs:
        # Conclusive even on a full page: the rows already seen carry
        # the bound, and rows outside the window can only add to them.
        tally.record_conclusive()
        return Admission(
            allowed=False,
            reason=(
                f"{counted.count} kstrl PR(s) open (bound {config.max_open_prs}); "
                "waiting for review"
            ),
        )
    if counted.saturated:
        reason = (
            f"cannot count open kstrl PRs: gh returned a full page, so "
            f"{counted.count} is a lower bound and not a count"
        )
        tally.record_inconclusive(reason)
        return Admission(allowed=False, reason=reason)
    tally.record_conclusive()
    return Admission(
        allowed=True,
        reason=f"{counted.count} of {config.max_open_prs} kstrl PRs open",
    )


def _load_pr_count_streak(root_dir: Path, observer: ServeObserver) -> OpenPrCountStreak:
    """Restore the streak, reporting a damaged file where an operator looks.

    :meth:`OpenPrCountStreak.load` already emits a ``RuntimeWarning``,
    which reaches stderr and therefore ``serve.err.log``. Repeating it on
    the observer is what puts it in front of somebody running
    ``ks serve`` in a terminal, which is the surface the whole S5 finding
    was about.
    """
    streak = OpenPrCountStreak.load(root_dir)
    if streak.damaged is not None:
        observer.warn(streak.damaged)
    return streak


def _save_pr_count_streak(
    streak: OpenPrCountStreak,
    root_dir: Path,
    observer: ServeObserver,
) -> None:
    """Persist the streak after a cycle, reporting a refusal or a failure."""
    refusal = streak.save(root_dir)
    if refusal is not None:
        observer.warn(refusal)


def _run_intake(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull remote work in, then act on steering comments. Best effort.

    ONE config read, and - when either half is on - ONE resolution of
    the checkout's own repo, shared by both: `enabled` admits labelled
    issues and spends money, `steer_enabled` reads comments and writes
    the operator's checkout. Coupling them would mean an operator who
    wants only the steering channel has to switch on issue intake.

    #231's simplify pass (B2) moved the repo resolution here from `sync`
    and `poll_steering`, which each used to resolve it independently -
    a second `gh repo view` on any cycle where both are on - and moved
    the open-PR count steering needs here too, which is what deletes
    `poll_steering`'s deferred `from kstrl.serve import
    count_open_kstrl_prs`, the inward import that made this module
    import `intake_github` import this module back.

    Strictly additive, like the adapter itself: every failure is recorded
    and the cycle continues to drain whatever is already queued. A GitHub
    outage must not stop local work.
    """
    try:
        from kstrl.intake_gh import GitHubIntakeConfig

        config = GitHubIntakeConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub intake raised: {exc}")
        return (), (str(exc),)
    if not config.enabled and not config.steer_enabled:
        return (), ()

    from kstrl.intake_github import checkout_repo

    repo, repo_error = checkout_repo(config, root_dir)
    if repo_error:
        observer.warn(f"GitHub intake raised: {repo_error}")
        return (), (repo_error,)

    marked_numbers: tuple[int, ...] = ()
    if config.steer_enabled:
        try:
            marked_numbers = count_open_kstrl_prs(root_dir).marked_numbers
        except Exception as exc:  # noqa: BLE001 - an unknown PR list is not an empty one
            observer.warn(f"GitHub steering raised: could not list the open kstrl PRs: {exc}")

    enqueued, errors = _sync_remote_issues(root_dir, queue, observer, config, repo)
    _run_steering(root_dir, queue, observer, config, repo, marked_numbers)
    return enqueued, errors


def _sync_remote_issues(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
    config: GitHubIntakeConfig,
    repo: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull remote work into the queue, best effort.

    Review #189 F1: without this the daemon drained a queue nothing could
    fill. `ks queue sync` existed but only as a manual command, so an
    installed LaunchAgent could never admit a labelled issue - the
    adapter and the daemon were each correct and their composition did
    nothing.

    `repo` (#231 B2): the checkout's repo, already resolved once by
    `_run_intake` and passed through to `sync`'s `local_repo`, so `sync`
    does not resolve it a second time.

    Strictly additive, like the adapter itself: every failure is recorded
    and the cycle continues to drain whatever is already queued. A GitHub
    outage must not stop local work.
    """
    if not config.enabled:
        return (), ()
    try:
        from kstrl.intake_github import Decision
        from kstrl.intake_github import sync as intake_sync

        result = intake_sync(
            queue,
            config,
            root_dir,
            # Poll and authorize unlocked; take the queue mutex only for
            # the local commit, so a slow GitHub cannot block
            # `ks queue pause` or any other transition (#189 N1).
            commit_guard=lambda: queue_lock(root_dir, blocking=True),
            local_repo=repo,
        )
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub intake raised: {exc}")
        return (), (str(exc),)

    for ref in result.enqueued:
        observer.info(f"Intake queued {ref}")
    for error in result.errors:
        observer.warn(f"  intake: {error}")
    # #188 simplify pass item 2: a refusal only ``ks queue sync``/`--dry-run`
    # could show was invisible from the daemon's own narration.
    for entry in result.planned:
        if entry.decision is Decision.REFUSE_UNAUTHORIZED:
            observer.warn(f"  intake refused {entry.issue.source_ref(result.repo)}: {entry.reason}")
    return result.enqueued, result.errors


def _run_steering(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
    config: GitHubIntakeConfig,
    repo: str,
    marked_numbers: tuple[int, ...],
) -> None:
    """Act on `/memory` and `/iterate` comments on open kstrl PRs (#231).

    Narration only: the effects are the memory file, the queue and the
    acknowledgement comments. Every refusal is spoken, never silent,
    which is #188's rule applied to the same surface.

    `repo` and `marked_numbers` (#231 B2): resolved once by `_run_intake`
    and passed straight through, so `poll_steering` need not resolve
    either itself.

    No `steer_enabled` check here. `poll_steering` returns an empty
    result as its first statement when the switch is off, above the
    `ProcessedLedger` construction, and one gate with one home is the
    point (Decision 7).

    Returns None, so steering failures reach the observer and NOT
    `CycleResult.sync_errors`. Deliberate: `sync_errors` is the remote
    INTAKE's field, read by the CLI as "what the issue
    adapter could not admit", and folding a comment-poll failure into it
    would make a steering outage read as an intake outage. Adding a
    `CycleResult` field instead would mean editing `serve_cycle`'s body,
    which the ratchet forbids.
    """
    try:
        from kstrl.intake_steering import poll_steering

        result = poll_steering(
            config,
            root_dir,
            queue,
            repo=repo,
            marked_numbers=marked_numbers,
            commit_guard=lambda: queue_lock(root_dir, blocking=True),
        )
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub steering raised: {exc}")
        return
    for key in result.applied:
        observer.info(f"Steering applied {key}")
    for item_id in result.enqueued:
        observer.info(f"Steering queued a re-run as {item_id}")
    for key, reason in result.skipped.items():
        observer.warn(f"  steering skipped {key}: {reason}")
    for error in result.errors:
        observer.warn(f"  steering: {error}")
