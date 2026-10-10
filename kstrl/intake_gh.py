"""Talk to GitHub through gh for one configured intake, and write each outcome back."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.config import _parse_paths
from kstrl.config_numbers import SIGNED, check_numbers
from kstrl.jsonread import read_json
from kstrl.workqueue_items import ItemSource, QueueItem


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
            encoding="utf-8",
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
    # A queue priority: a negative value runs after the default 0 (#571: SIGNED).
    default_priority: int = field(default=0, metadata=SIGNED)
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
            *(
                self.state_label(name)
                for name in (
                    "running",
                    "done",
                    "failed",
                    "poison",
                    "awaiting_approval",
                    "awaiting_answer",
                )
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
        return check_numbers(cls(**values))


def apply_state_label(
    config: GitHubIntakeConfig,
    repo: str,
    number: int,
    state: str,
    root_dir: Path,
) -> str:
    """Move an issue to exactly one kstrl state label.

    Reads the issue's labels, then adds the new one and removes the
    managed labels the issue carries, so an issue can never carry two
    contradictory states. Only carried labels are named because ``gh``
    (read at 2.73.0) fails a whole ``--remove-label`` list when one name
    is not a label of the repository, and the issue then kept its old
    state label on every writeback (#738). Returns an error string, or
    "" on success; a writeback failure is reported, never raised, because
    the queue transition it describes has already happened locally.
    """
    if config.dry_run:
        return ""
    view = run_gh(
        ["issue", "view", str(number), "--repo", repo, "--json", "labels"],
        timeout=config.timeout_seconds,
        cwd=root_dir,
    )
    if not view.ok:
        return view.error
    carried = _carried_labels(view.stdout)
    if carried is None:
        return f"could not read the labels of issue #{number}: {view.stdout[:200]!r}"
    target = config.state_label(state)
    remove = [
        name for name in config.managed_labels if name != target and name.casefold() in carried
    ]
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


def _carried_labels(stdout: str) -> frozenset[str] | None:
    """The casefolded label names in ``gh issue view --json labels`` output.

    None when the output is not that shape: an unreadable answer is a
    failed writeback, never an issue with no labels.
    """
    try:
        data = read_json(stdout)
    except json.JSONDecodeError:
        return None
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list):
        return None
    names = [entry.get("name") if isinstance(entry, dict) else None for entry in labels]
    if not all(isinstance(name, str) for name in names):
        return None
    return frozenset(str(name).casefold() for name in names)


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
                f"`ks queue show {item.item_id}` and, if it should run "
                "again, `ks queue retry --reset-attempts`.",
            ]
        )
    elif state == "awaiting_approval":
        lines.extend(
            [
                "",
                "Nothing was pushed: the merge gate waits for a human. On the "
                "machine running `ks serve`, `ks inbox ls` lists the merge_gate "
                "item; `ks inbox approve <id>` pushes the reviewed branch, opens "
                "the PR, merges it and continues the run, and `ks inbox reject "
                "<id> --comment ...` fails the component.",
            ]
        )
    elif state == "awaiting_answer":
        lines.extend(
            [
                "",
                "Nothing runs until the owner answers the architect's question. "
                "On the machine running `ks serve`, `ks inbox ls` lists the "
                "spec_escalation item with the question. Write the answered spec "
                f"to a file and run `ks queue answer {item.item_id} <answered spec "
                "file>`, which replaces the queued copy of the spec and requeues "
                "the item. Editing this issue does not change the queued item.",
            ]
        )
    return "\n".join(lines)


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
