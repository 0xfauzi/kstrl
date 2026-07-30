"""R8.6 continuous intake: GitHub Issues as the remote inbox.

An issue labelled ``kstrl:queued`` becomes a queue item; the queue's
verdict comes back as a state label and a comment. Polling only - no
webhooks, no public endpoint, nothing to keep reachable.

**What authorizes work.** The trigger is the LABEL, not the issue. Adding
a label to a repository requires write access, so on a public repo a
stranger can open an issue but cannot queue a factory run against it.
That property is the whole access-control story for this adapter, and it
is the reason the adapter watches a label rather than, say, a title
prefix or a mention.

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
from collections.abc import Callable
from dataclasses import dataclass, field
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


def parse_issue_list(payload: str) -> list[RemoteIssue]:
    """Decode ``gh issue list --json``, skipping anything malformed.

    Tolerant by design: one unparseable entry must not discard the whole
    poll, because the queue would then stall on a single bad issue.
    """
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
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
    # ordering within a priority band.
    issues.sort(key=lambda issue: issue.number)
    return issues


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


def poll_queued(
    config: GitHubIntakeConfig, repo: str, root_dir: Path,
) -> tuple[list[RemoteIssue], str]:
    """Open issues carrying the trigger label, oldest first.

    One API call per sync. Note for the record (H4): this does NOT use
    conditional requests - ``gh issue list`` exposes no ETag - so the
    saving the R8.6 plan attributed to ETags is not realised here. It is
    not needed at this cadence: one call per poll interval is ~60/hour
    against a 5,000/hour budget.
    """
    result = run_gh(
        [
            "issue", "list",
            "--repo", repo,
            "--label", config.queued_label,
            "--state", "open",
            "--limit", str(max(config.max_items_per_sync * 2, 10)),
            "--json", "number,title,body,url,labels",
        ],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not result.ok:
        return [], result.error
    return parse_issue_list(result.stdout), ""


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
) -> SyncResult:
    """Admit labelled issues into the local queue.

    Additive by construction: every failure path returns a result with
    errors recorded and the queue untouched. A GitHub outage produces an
    empty sync, not a stalled queue.
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

    issues, poll_error = poll_queued(config, repo, root_dir)
    if poll_error:
        result.errors = (poll_error,)
        return result
    result.polled = len(issues)

    ledger = ProcessedLedger(root_dir).load()
    stamp = now_iso or _utc_now_iso()
    enqueued: list[str] = []
    errors: list[str] = []

    for issue in issues:
        if len(enqueued) >= config.max_items_per_sync:
            result.skipped[issue.source_ref(repo)] = (
                f"per-sync cap of {config.max_items_per_sync} reached"
            )
            continue
        ref = issue.source_ref(repo)
        if ledger.contains(ref):
            result.skipped[ref] = "already processed"
            continue
        if queue.find_by_source_ref(ref) is not None:
            result.skipped[ref] = "already in the queue"
            continue
        if not issue.body.strip():
            # An empty body would produce a spec with nothing but a
            # provenance header. Skip loudly rather than paying an
            # architect call to discover it says nothing.
            result.skipped[ref] = "issue body is empty; nothing to build"
            continue

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

        ledger.record(ref, item_id=item.item_id, when=stamp)
        enqueued.append(ref)
        label_error = apply_state_label(
            config, repo, issue.number, "running", root_dir,
        )
        if label_error:
            # The item IS queued; only the remote view is stale.
            errors.append(f"{ref}: enqueued but label writeback failed "
                          f"({label_error})")

    result.enqueued = tuple(enqueued)
    result.errors = tuple(errors)
    return result


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
