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
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.config import _parse_paths
from kstrl.statedir import (
    CONTROL_GITHUB_PROCESSED,
    control_file,
    control_lock,
    ensure_control_state,
)
from kstrl.workqueue import (
    ItemSource,
    MergeDisposition,
    Queue,
    QueueError,
    QueueItem,
)

if TYPE_CHECKING:
    from kstrl.operator_context import OperatorFile

#: Cap on the spec text built from an issue. Generous for a real spec,
#: bounded enough that a pathological body cannot become a pathological
#: prompt. Truncation is announced in the spec itself, never silent.
MAX_SPEC_CHARS = 60_000

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
    args: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
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


def _validate_allowed_actors(value: Any) -> None:
    """Reject an ``allowed_actors`` value the adapter cannot act on.

    ``Any`` rather than ``list[str]``: a toml array member can be any type.
    """
    if not isinstance(value, list):
        raise IntakeError(
            "intake_github.allowed_actors must be a list of GitHub logins, got "
            f"{type(value).__name__}"
        )
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise IntakeError(
                f"intake_github.allowed_actors[{index}] must be a non-empty string, got {entry!r}"
            )


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
    #: The label that authorizes work. Who may apply it is ``allowed_actors``.
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
    #: GitHub logins allowed to apply the trigger label, and so to
    #: authorize spend. EMPTY (the default, and a SET-BUT-EMPTY env var)
    #: keeps the inherited-permission behaviour: anyone who can label the
    #: issue can spend. Non-empty, the LATEST trigger-label event's actor
    #: must be on this list. Compared case-insensitively.
    allowed_actors: list[str] = field(default_factory=list)
    #: R10.10. Poll comments on open kstrl-authored PRs for `/memory` and
    #: `/iterate`. OFF by default, and the default is the whole argument:
    #: on, this makes the daemon a WRITER of the operator's checkout
    #: (`[paths] memory`), which no other part of `ks serve` is. Who may
    #: steer is `allowed_actors`, the same list #188 already uses; there
    #: is deliberately no second allowlist.
    steer_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.queued_label.strip():
            raise IntakeError("intake_github.queued_label must not be empty")
        if self.max_items_per_sync < 1:
            raise IntakeError(
                f"intake_github.max_items_per_sync must be >= 1, got {self.max_items_per_sync}"
            )
        if self.timeout_seconds <= 0:
            raise IntakeError(
                f"intake_github.timeout_seconds must be > 0, got {self.timeout_seconds}"
            )
        if self.repo and self.repo.count("/") != 1:
            raise IntakeError(f"intake_github.repo must be 'owner/name', got {self.repo!r}")
        _validate_allowed_actors(self.allowed_actors)

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
            *(self.state_label(name) for name in ("running", "done", "failed", "poison")),
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
        actors = os.environ.get("KSTRL_INTAKE_GITHUB_ALLOWED_ACTORS")
        steer = os.environ.get("KSTRL_INTAKE_GITHUB_STEER_ENABLED")
        return cls(
            enabled=defaults.enabled if enabled is None else enabled == "1",
            repo=defaults.repo if repo is None else repo,
            queued_label=defaults.queued_label if label is None else label,
            label_prefix=defaults.label_prefix if prefix is None else prefix,
            max_items_per_sync=(defaults.max_items_per_sync if cap is None else int(cap)),
            default_priority=(defaults.default_priority if priority is None else int(priority)),
            comment_on_result=(defaults.comment_on_result if comment is None else comment == "1"),
            dry_run=defaults.dry_run if dry is None else dry == "1",
            timeout_seconds=(defaults.timeout_seconds if timeout is None else float(timeout)),
            allowed_actors=(defaults.allowed_actors if actors is None else _parse_paths(actors)),
            steer_enabled=defaults.steer_enabled if steer is None else steer == "1",
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> GitHubIntakeConfig:
        """Precedence: env > toml > defaults; reads ``[intake_github]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(
            resolve_config_file(root_dir),
            "intake_github",
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
                "max_items_per_sync",
                defaults.max_items_per_sync,
            ),
            "default_priority": _int(
                "default_priority",
                defaults.default_priority,
            ),
            "comment_on_result": _bool(
                "comment_on_result",
                defaults.comment_on_result,
            ),
            "dry_run": _bool("dry_run", defaults.dry_run),
            "timeout_seconds": float(
                section["timeout_seconds"]
                if "timeout_seconds" in section
                else defaults.timeout_seconds
            ),
            "allowed_actors": section.get("allowed_actors", defaults.allowed_actors),
            "steer_enabled": _bool("steer_enabled", defaults.steer_enabled),
        }
        env_map: dict[str, tuple[str, Callable[[str], Any]]] = {
            "KSTRL_INTAKE_GITHUB_ENABLED": ("enabled", lambda v: v == "1"),
            "KSTRL_INTAKE_GITHUB_REPO": ("repo", str),
            "KSTRL_INTAKE_GITHUB_QUEUED_LABEL": ("queued_label", str),
            "KSTRL_INTAKE_GITHUB_LABEL_PREFIX": ("label_prefix", str),
            "KSTRL_INTAKE_GITHUB_MAX_ITEMS": ("max_items_per_sync", int),
            "KSTRL_INTAKE_GITHUB_PRIORITY": ("default_priority", int),
            "KSTRL_INTAKE_GITHUB_COMMENT": (
                "comment_on_result",
                lambda v: v == "1",
            ),
            "KSTRL_INTAKE_GITHUB_DRY_RUN": ("dry_run", lambda v: v == "1"),
            "KSTRL_INTAKE_GITHUB_TIMEOUT": ("timeout_seconds", float),
            "KSTRL_INTAKE_GITHUB_ALLOWED_ACTORS": ("allowed_actors", _parse_paths),
            "KSTRL_INTAKE_GITHUB_STEER_ENABLED": ("steer_enabled", lambda v: v == "1"),
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
            data = json.loads(raw)
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
        from kstrl.workqueue import atomic_write

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
    #: Who applied the trigger label. Surfaced in skip reasons, and the
    #: value :func:`authorization_refusal` matches against
    #: ``allowed_actors`` (#188).
    actor: str = ""
    labeled_at: str = ""
    last_edited_at: str = ""


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
    result = run_gh(
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
        payload = json.loads(result.stdout or "{}")
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
            header + body[: max(0, keep)] + "\n\n[truncated by kstrl: issue body exceeded "
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
        "issue",
        "edit",
        str(number),
        "--repo",
        repo,
        "--add-label",
        target,
    ]
    for name in remove:
        args.extend(["--remove-label", name])
    result = run_gh(args, timeout=config.timeout_seconds, cwd=root_dir)
    return "" if result.ok else result.error


def _gh_comment(
    subcommand: str,
    label: str,
    config: GitHubIntakeConfig,
    repo: str,
    number: int,
    body: str,
    root_dir: Path,
) -> str:
    """Run ``gh <subcommand> comment``. Returns "" or an error naming ``label #N``.

    #231 B6. Shared by :func:`post_comment` (the issue verdict
    writeback, gated on ``comment_on_result``) and the steering
    acknowledgement (gated on nothing - it is the only feedback a
    commenter gets). The gate differs; the call and the error mapping do
    not, so only ``post_comment`` still carries a gate of its own.
    """
    result = run_gh(
        [subcommand, "comment", str(number), "--repo", repo, "--body", body],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    return "" if result.ok else f"could not comment on {label} #{number}: {result.error}"


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
    return _gh_comment("issue", "issue", config, repo, number, body, root_dir)


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
    lines.append(f"Queue item `{item.item_id}` - attempt {item.attempts} of {item.max_attempts}.")
    if state == "poison":
        lines.extend(
            [
                "",
                "This will NOT be retried automatically. Inspect it with "
                f"`ks queue show {item.item_id[:12]}` and, if it should run "
                "again, `ks queue retry --reset-attempts`.",
            ]
        )
    elif state == "done":
        lines.extend(
            [
                "",
                "The PR waits for a human merge decision: remote-sourced items "
                "always stop at the PR.",
            ]
        )
    return "\n".join(lines)


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
    result = run_gh(
        ["api", endpoint, "--paginate"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return [], (), f"could not read comments on PR #{number}: {result.error}"
    try:
        rows = json.loads(result.stdout or "[]")
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


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
