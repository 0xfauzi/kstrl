"""A project's ``[stack]``: instructions for the models, and the commands kstrl runs (#696).

The owner charter (CLAUDE.md) rules that kstrl holds no language-specific
code or config. A stack is how a project says what it is built with. It is
instruction text the models read, plus commands kstrl runs and judges WITHOUT
knowing what they are: a check passes when it exits 0 and fails on any other
exit. kstrl parses none of their output to decide.

The table in kstrl.toml has four keys, and all four are required::

    [stack]
    instructions = "What the project is built with, and how to work in it."
    setup = "make deps"        # "" states that worktrees need no setup
    env = ["TOOL_HOME"]        # variables the commands may see
    [stack.checks]             # run in this order; each must exit 0
    tests = "make test"
    lint = "make lint"

Three more keys are optional (#700 slice 2, :data:`STACK_RUNG_KEYS`): the
paths the isolation rung lets the commands write and read beyond their
worktree, and whether the stack drives a browser. One more is optional
(#700 slice 3): ``up``, the command that starts the application and exits 0
once it is ready. kstrl never reads what it does; ``ks doctor --measure``
replays the recipe (``kstrl.replay``) and a stack whose replay failed is not
confirmed (:data:`REPLAY_STAGES`).

With no ``[stack]`` table nothing changes: kstrl reads ``[verify]`` as before.
With one, it is the only source of verification commands, and every other
source is refused by name (:data:`OTHER_COMMAND_SOURCES`).

A stack runs nothing until a person confirms it (#696 slice 3). The
confirmation is an APPROVED ``stack_confirmation`` inbox item bound to the
stack's digest, in the control directory outside every worktree, and only
the most recent approval counts: reverting to an older confirmed text needs
a new confirmation. :func:`confirmed_stack` is the one reader of a usable
stack. A :class:`Stack` that did not come from it carries the reason in
``unconfirmed``, and both runners (``verify.check_stack_command`` and
``worktree_setup.WorktreeSetup``) refuse to run a command from one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kstrl.config_numbers import check_numbers
from kstrl.config_toml import ConfigError, load_toml_document, section_table
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind, ItemStatus

#: The four keys of ``[stack]``. Every one is required.
STACK_KEYS: tuple[str, ...] = ("instructions", "setup", "checks", "env")

#: #700 slice 2: what the isolation rung grants a stack's commands beyond
#: their worktree. Optional; absent is the same stack as empty or false.
#: ``writable`` and ``readable`` are paths (``~`` is the home directory,
#: a relative path is under the project root); ``browser = true`` adds
#: the raw Seatbelt rules headless Chromium needs to the test zone.
STACK_RUNG_KEYS: tuple[str, ...] = ("writable", "readable", "browser")

#: #700 slice 3: the stage a clean replay of the recipe (``kstrl.replay``)
#: failed at, as the replay record's ``failed`` names it; "" is a replay
#: that passed. The two in :data:`REPLAY_STAGES_WITH_EXIT` carry what the
#: command gave after a colon: its exit status, ``timeout`` or
#: ``undecodable`` (``up_failed:1``). One vocabulary for the replay that
#: writes it and the confirmation that reads it.
REPLAY_BOUNDARY_REFUSED = "boundary_refused"
REPLAY_SETUP_FAILED = "setup_failed"
REPLAY_UP_FAILED = "up_failed"
REPLAY_UP_TIMEOUT = "up_timeout"
REPLAY_CHECK_NOT_RUNNABLE = "check_not_runnable"
REPLAY_BASE_CONTRADICTION = "base_contradiction"
REPLAY_STAGES: tuple[str, ...] = (
    REPLAY_BOUNDARY_REFUSED,
    REPLAY_UP_TIMEOUT,
    REPLAY_CHECK_NOT_RUNNABLE,
    REPLAY_BASE_CONTRADICTION,
)
REPLAY_STAGES_WITH_EXIT: tuple[str, ...] = (REPLAY_SETUP_FAILED, REPLAY_UP_FAILED)


def replay_failure_known(failed: object) -> bool:
    """Whether ``failed`` is a replay record's ``failed`` kstrl can read."""
    if not isinstance(failed, str):
        return False
    stage, colon, given = failed.partition(":")
    if colon:
        return stage in REPLAY_STAGES_WITH_EXIT and bool(given)
    return failed == "" or failed in REPLAY_STAGES


def replay_refuses(failed: str) -> bool:
    """Whether a replay that failed at ``failed`` keeps its stack from being
    confirmed: every failure but the boundary's, which says nothing about
    the recipe (the replay ran nothing)."""
    return failed not in ("", REPLAY_BOUNDARY_REFUSED)


def replay_decides(replay: dict[str, Any]) -> bool:
    """Whether a replay record says anything about the recipe, so that it
    replaces an earlier one: a failure at a stage that ran, or a pass kstrl
    set up and cleaned up without an error. A refused boundary ran nothing
    and an errored replay proved nothing, so neither lifts a failure."""
    failed = str(replay.get("failed", ""))
    return replay_refuses(failed) or not (failed or replay.get("error"))


#: A name that holds any of these is never passed to a command, whoever
#: declared it (#696 decision 11). ``verify.scrubbed_subprocess_env`` drops
#: the same names; one tuple, so the refusal and the scrub cannot disagree.
SECRET_NAME_FRAGMENTS: tuple[str, ...] = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)

#: kstrl's own variables: kstrl sets the ones a command needs itself, so a
#: stack may not pass one through (``KSTRL_REPORT`` among them).
KSTRL_ENV_PREFIX = "KSTRL_"

#: A POSIX environment variable name.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Every other place a verification command can come from. With ``[stack]``
#: present each one is refused by name: the stack is the one source.
#: ``(section, key)`` rows are kstrl.toml keys; ``(None, name)`` rows are
#: environment variables. The ``ks factory`` flags are refused in
#: ``cli.factory``, which is the only command that has them.
OTHER_COMMAND_SOURCES: tuple[tuple[str | None, str], ...] = (
    ("verify", "test_command"),
    ("verify", "typecheck_command"),
    ("verify", "lint_command"),
    ("verify", "test_tool"),
    ("verify", "typecheck_tool"),
    ("verify", "lint_tool"),
    ("factory", "worktree_setup_command"),
    ("contract", "test_command"),
    ("breaker", "test_command"),
    (None, "KSTRL_VERIFY_TEST_CMD"),
    (None, "KSTRL_VERIFY_TYPECHECK_CMD"),
    (None, "KSTRL_VERIFY_LINT_CMD"),
    (None, "KSTRL_VERIFY_TEST_TOOL"),
    (None, "KSTRL_VERIFY_TYPECHECK_TOOL"),
    (None, "KSTRL_VERIFY_LINT_TOOL"),
    (None, "KSTRL_FACTORY_WORKTREE_SETUP_COMMAND"),
    (None, "KSTRL_CONTRACT_TEST_CMD"),
    (None, "KSTRL_BREAKER_TEST_CMD"),
)

#: H3: engineer-facing CONTEXT, rendered into every engineer prompt of a run
#: whose project has a ``[stack]`` (``loop.build_project_context``). The
#: calibration suite scores no engineer-context fixture, so this carries the
#: H3 obligation and no H2 obligation the suite can discharge (CLAUDE.md, the
#: DECISIONS_CONTEXT_PROMPT position). Only the template is pinned: the
#: instructions and the checks are the operator's, interpolated at run time.
STACK_PROMPT_VERSION = "1.0.0"

STACK_PROMPT = """\
# Stack (from this project's kstrl.toml [stack])

The operator chose this project's stack. Build in it, and follow these
instructions:

{instructions}

## Checks

kstrl runs these commands on your work, in this order, from the root of the
tree. A check passes only when it exits 0. Run them yourself before you report
a story complete. They are the only verification commands: ignore any other
list, including one written in the project context above.

{checks}"""


#: The inbox dedupe-key prefix of a stack confirmation item: ``stack:<digest>``.
#: ``ks inbox approve`` recognises a stack item by it.
STACK_KEY = "stack:"

#: The ``kind`` of the checkpoint events a run records for its stack.
STACK_KIND = "stack"

#: The options the stack checkpoint offers. The default is the last one, so
#: an accidental Enter costs a wait, never a confirmation.
STACK_OPTIONS = ("Confirm this stack", "Reject this stack", "Decide later in the inbox")

#: ``Stack.unconfirmed`` of a stack nothing has checked: what ``load_stack``
#: returns. Only :func:`confirmed_stack` clears it.
NOT_CHECKED = "has not been checked for a confirmation"

#: ``Stack.confirmed_by`` of a stack an operator confirmed at the prompt with
#: ``[inbox] enabled = false``: it holds for this process only.
CONFIRMED_THIS_RUN = "operator"

#: ``Stack.confirmed_by`` of a stack confirmed by an APPROVED inbox item.
CONFIRMED_IN_INBOX = "inbox"

#: Digests confirmed at the prompt in this process while the inbox is off
#: (#696 decision 1(i)): the confirmation holds for this run only.
_CONFIRMED_THIS_RUN: set[str] = set()

#: An inbox digest is a SHA-256 in lowercase hex, as :attr:`Stack.digest` writes it.
_DIGEST = re.compile(r"[0-9a-f]{64}")


class StackError(ConfigError):
    """A ``[stack]`` table kstrl will not use, with every reason indexed.

    A :class:`ConfigError`, so every surface that renders one renders this.
    Its own class because three loaders read the stack (``VerifyConfig``,
    ``FactoryConfig`` and ``ContractConfig``) besides :class:`StackConfig`,
    and the entry check (``config_preflight.collect_config_problems``) says a
    stack's fault once, not once per loader.
    """


@dataclass(frozen=True)
class Stack:
    """One loaded ``[stack]``: never partial, see :func:`load_stack`."""

    instructions: str
    setup: str
    #: ``(name, command)`` in the order kstrl.toml lists them.
    checks: tuple[tuple[str, str], ...]
    env: tuple[str, ...]
    #: #700 slice 2, :data:`STACK_RUNG_KEYS`, as kstrl.toml spells them.
    writable: tuple[str, ...] = ()
    readable: tuple[str, ...] = ()
    browser: bool = False
    #: #700 slice 3: the command that starts the application and exits 0 once
    #: it is ready; "" (or absent) when the stack starts nothing.
    up: str = ""
    #: Why no command of this stack may run, or "" when it may. Set to ""
    #: only by :func:`confirmed_stack`; not part of the digest.
    unconfirmed: str = field(default=NOT_CHECKED, compare=False)
    #: How it was confirmed: :data:`CONFIRMED_IN_INBOX` or
    #: :data:`CONFIRMED_THIS_RUN`; "" while ``unconfirmed`` is set.
    confirmed_by: str = field(default="", compare=False)

    @property
    def check_names(self) -> tuple[str, ...]:
        return tuple(name for name, _command in self.checks)

    @property
    def digest(self) -> str:
        """SHA-256 of the table's canonical JSON: what the stack IS.

        Key order and check order are part of the text: checks run in the
        order they are listed, so reordering them is a different stack.
        """
        canonical = json.dumps(
            {
                "instructions": self.instructions,
                "setup": self.setup,
                "checks": [list(check) for check in self.checks],
                "env": list(self.env),
                "writable": list(self.writable),
                "readable": list(self.readable),
                "browser": self.browser,
                "up": self.up,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def format_for_prompt(self) -> str:
        """The block ``loop.build_project_context`` hands the engineer."""
        checks = "\n".join(f"- {name}: `{command}`" for name, command in self.checks)
        return STACK_PROMPT.format(instructions=self.instructions.strip(), checks=checks)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _env_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return [f"env must be a list of variable names, got {value!r}"]
    errors: list[str] = []
    for index, name in enumerate(value):
        where = f"env[{index}]"
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            errors.append(f"{where} must be a variable name, got {name!r}")
        elif any(fragment in name.upper() for fragment in SECRET_NAME_FRAGMENTS):
            errors.append(
                f"{where} {name} looks like a secret ({', '.join(SECRET_NAME_FRAGMENTS)}); "
                "a check never sees a secret"
            )
        elif name.startswith(KSTRL_ENV_PREFIX):
            errors.append(f"{where} {name} is kstrl's own; kstrl sets what a check needs")
    return errors


def _paths_errors(key: str, value: object) -> list[str]:
    if not isinstance(value, list):
        return [f"{key} must be a list of paths, got {value!r}"]
    return [
        f"{key}[{index}] must be a non-empty path, got {path!r}"
        for index, path in enumerate(value)
        if not _nonempty_text(path)
    ]


def stack_paths(root_dir: Path, entries: tuple[str, ...]) -> list[Path]:
    """``writable`` or ``readable`` as paths: ``~`` expanded, a relative
    entry under ``root_dir``, an absolute one as written."""
    return [root_dir / Path(entry).expanduser() for entry in entries]


def _checks_errors(value: object) -> list[str]:
    if not isinstance(value, dict):
        return [f"checks must be a table of name = command, got {value!r}"]
    if not value:
        return ["checks is empty; a stack runs at least one check"]
    return [
        f"checks.{name} must be a non-empty command string, got {command!r}"
        for name, command in value.items()
        if not _nonempty_text(command)
    ]


_ALL_KEYS = (*STACK_KEYS, *STACK_RUNG_KEYS, "up")


def stack_errors(raw: dict[str, Any]) -> list[str]:
    """Every reason ``raw`` is not a usable ``[stack]``, indexed; [] when it is.

    The one vocabulary of a valid stack. A bad entry is refused, never
    dropped: a dropped check is a check that silently stopped running.
    """
    errors = [
        f"{key} is not a [stack] key; the keys are {', '.join(_ALL_KEYS)}"
        for key in raw
        if key not in _ALL_KEYS
    ]
    errors += [f"{key} is required" for key in STACK_KEYS if key not in raw]
    if "instructions" in raw and not _nonempty_text(raw["instructions"]):
        errors.append(f"instructions must be non-empty text, got {raw['instructions']!r}")
    if "setup" in raw and not isinstance(raw["setup"], str):
        errors.append(f'setup must be a command string, or "" for none, got {raw["setup"]!r}')
    if "checks" in raw:
        errors += _checks_errors(raw["checks"])
    if "env" in raw:
        errors += _env_errors(raw["env"])
    for key in ("writable", "readable"):
        errors += _paths_errors(key, raw.get(key, []))
    if not isinstance(raw.get("up", ""), str):
        errors.append(f'up must be a command string, or "" for none, got {raw["up"]!r}')
    if not isinstance(raw.get("browser", False), bool):
        errors.append(f"browser must be true or false, got {raw['browser']!r}")
    return errors


def _second_sources(document: Mapping[str, Any]) -> list[str]:
    """Every other command source in play beside ``[stack]``."""
    found: list[str] = []
    for section, key in OTHER_COMMAND_SOURCES:
        if section is None:
            if key in os.environ:
                found.append(f"the environment sets {key}")
            continue
        table = document.get(section)
        if isinstance(table, Mapping) and key in table:
            found.append(f"[{section}] {key} is set")
    return [
        f"{source}; with [stack] present the stack is the only source of "
        "verification commands, so remove it"
        for source in found
    ]


def load_stack(root_dir: Path) -> Stack | None:
    """The project's ``[stack]``, or None when kstrl.toml has none.

    Raises :class:`StackError` (exit 2 at every surface) for a table with any
    error, and for any other command source set beside it. Never returns a
    partial stack. Read through ``config_toml.load_toml_document``, so a parse
    fault is the same ``ConfigError`` every loader raises.
    """
    from kstrl.config import resolve_config_file

    path = resolve_config_file(root_dir)
    if not path.exists():
        return None
    document = load_toml_document(path)
    if "stack" not in document:
        return None
    raw = section_table(document, "stack", path)
    errors = stack_errors(raw) + _second_sources(document)
    if errors:
        raise StackError(f"[stack] in {path} is refused:\n    " + "\n    ".join(errors))
    return Stack(
        instructions=str(raw["instructions"]),
        setup=str(raw["setup"]),
        checks=tuple((str(name), str(command)) for name, command in raw["checks"].items()),
        env=tuple(str(name) for name in raw["env"]),
        writable=tuple(str(path) for path in raw.get("writable", [])),
        readable=tuple(str(path) for path in raw.get("readable", [])),
        browser=bool(raw.get("browser", False)),
        up=str(raw.get("up", "")),
    )


class StackRefused(StackError):
    """:func:`confirmed_stack`'s refusal: ``stack.unconfirmed`` says why."""

    def __init__(self, stack: Stack) -> None:
        super().__init__(f"the [stack] in kstrl.toml {stack.unconfirmed}")
        self.stack = stack


def stack_dedupe_key(digest: str) -> str:
    """The dedupe key of the stack_confirmation item for the stack ``digest``."""
    return f"{STACK_KEY}{digest}"


def _record_errors(record: dict[str, Any]) -> list[str]:
    """Why an inbox line that claims to be a stack decision cannot be read.

    The same vocabulary the fold uses (``InboxItem.from_dict``), plus the
    two fields a stack decision binds by: the digest and the key built
    from it.
    """
    item = InboxItem.from_dict(record)
    if item is None:
        return ["it is not an inbox item kstrl can read"]
    errors: list[str] = []
    if item.kind is not ItemKind.STACK_CONFIRMATION:
        errors.append(f"its kind is {str(item.kind)!r}, not {str(ItemKind.STACK_CONFIRMATION)!r}")
    digest = item.evidence.get("stack_digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        errors.append(f"its evidence.stack_digest {digest!r} is not a sha256")
    elif item.dedupe_key != stack_dedupe_key(digest):
        errors.append(f"its dedupe_key {item.dedupe_key!r} does not name its digest")
    replay = item.evidence.get("replay")
    if replay is not None and not (
        isinstance(replay, dict) and replay_failure_known(replay.get("failed"))
    ):
        shown = replay.get("failed") if isinstance(replay, dict) else type(replay).__name__
        errors.append(f"its evidence.replay names no replay outcome kstrl can read ({shown!r})")
    return errors


def _is_stack_record(record: dict[str, Any]) -> bool:
    return record.get("kind") == str(ItemKind.STACK_CONFIRMATION) or str(
        record.get("dedupe_key", "")
    ).startswith(STACK_KEY)


def _latest_approval(
    root_dir: Path, inbox_config: InboxConfig
) -> tuple[tuple[InboxItem, bool] | None, dict[str, str]]:
    """The stack item the newest APPROVED line of the inbox names, and whether it is still
    approved, or None; and, per stack digest, what the newest line carrying a
    replay record says it failed at (#700 slice 3).

    The replay is read off the newest LINE that carries one, not off the
    item: a later filing without a replay (``ks factory`` refusing) replaces
    the item's evidence, and must not erase a failure.

    The newest approval line decides, not the newest item that is still
    approved: when a person withdraws that approval later (``ks inbox
    reject`` or ``snooze``), no older approval comes back into force, so
    the caller refuses until a stack is confirmed again (#696 decision 1(b)).

    Raises :class:`ValueError` naming the fault when the inbox cannot be
    read, or any line of it could be a stack decision kstrl cannot read:
    an unreadable inbox is a refusal, never an empty read.
    """
    box = Inbox(root_dir, inbox_config)
    scan = box.scan()
    if scan.unreadable:
        raise ValueError(f"{box.path} cannot be read")
    if scan.skipped_lines:
        raise ValueError(
            f"{scan.skipped_lines} line(s) of {box.path} are not JSON objects, and any of "
            "them could be a newer stack decision"
        )
    folded: dict[str, InboxItem] = {}
    replays: dict[str, str] = {}
    newest = ""
    for position, record in enumerate(scan.records):
        if not _is_stack_record(record):
            continue
        errors = _record_errors(record)
        if errors:
            raise ValueError(
                f"record {position + 1} of {box.path} is a stack decision kstrl cannot read: "
                + "; ".join(errors)
            )
        item = InboxItem.from_dict(record)
        assert item is not None  # _record_errors refused every None
        folded[item.id] = item
        replay = item.evidence.get("replay")
        if isinstance(replay, dict) and replay_decides(replay):
            replays[str(item.evidence["stack_digest"])] = str(replay["failed"])
        if item.status is ItemStatus.APPROVED:
            newest = item.id
    if not newest:
        return None, replays
    return (folded[newest], folded[newest].status is ItemStatus.APPROVED), replays


def _refusal(root_dir: Path, stack: Stack) -> tuple[str, str]:
    """``(reason, confirmed_by)``: why ``stack`` may not run, or ``("", how)``.

    ``except Exception`` on both reads: whatever reading ``[inbox]`` or the
    inbox log raises (a TOML date cast to int is a TypeError, a control
    directory under the repository a ControlStateError) means the
    confirmation cannot be checked, which is a refusal, never a traceback
    and never an empty read. Not ``config_preflight.SURFACE_REJECTIONS``:
    importing that module from here makes every importer of kstrl.stack
    reach the queue (tests/test_state_dir_scope.py).
    """
    try:
        inbox_config = InboxConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return f"cannot be checked: [inbox] cannot be read: {exc}", ""
    if not inbox_config.enabled:
        if stack.digest in _CONFIRMED_THIS_RUN:
            return "", CONFIRMED_THIS_RUN
        return (
            "is not confirmed, and [inbox] is disabled, so only an answer at the prompt of "
            "ks factory in a terminal can confirm it, for that run only",
            "",
        )
    try:
        found, replays = _latest_approval(root_dir, inbox_config)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return f"cannot be checked: the inbox is unreadable: {exc}", ""
    if found is None:
        return f"is not confirmed: no inbox approval names {stack.digest[:12]}", ""
    latest, still_approved = found
    approved = str(latest.evidence["stack_digest"])
    if not still_approved:
        return (
            f"is not confirmed: the newest confirmation, of {approved[:12]}, was "
            f"{latest.status} by {latest.decided_by} at {latest.decided_at}",
            "",
        )
    if approved == stack.digest:
        failed = replays.get(stack.digest, "")
        if replay_refuses(failed):
            return (
                f"failed its clean replay at {failed} (ks doctor --measure), so its "
                f"confirmation of {approved[:12]} does not hold: fix the recipe and run "
                "ks doctor --measure again",
                "",
            )
        return "", CONFIRMED_IN_INBOX
    return (
        f"has changed since {approved[:12]}, confirmed by {latest.decided_by} at "
        f"{latest.decided_at}: it now reads {stack.digest[:12]}, which is not confirmed",
        "",
    )


def stack_text_digest(root_dir: Path, *, warn: Callable[[str], None]) -> str:
    """The digest of kstrl.toml's ``[stack]`` as written, "" when it has none.

    What a plan pins (``Manifest.stack_digest``): the text it was made
    under, whether or not anyone has confirmed it yet. Never a usable stack.

    A kstrl.toml that cannot be read pins nothing, said through ``warn``:
    decompose calls this ahead of its halt path, so raising here would
    cost the spec-issues artifact (``KstrlConfig.load_or_anchored`` is the
    precedent, and states the taxonomy). ``ks decompose`` and ``ks
    factory`` have already refused such a file at entry
    (``config_preflight``); only the in-process call reaches this.
    """
    try:
        stack = load_stack(root_dir)
    except (ConfigError, OSError) as exc:
        warn(f"the [stack] in kstrl.toml cannot be read, so this plan pins none: {exc}")
        return ""
    return stack.digest if stack is not None else ""


def confirmed_stack(root_dir: Path) -> Stack | None:
    """The project's ``[stack]`` when a person confirmed this exact text, else a refusal.

    The one reader of a usable stack. None when kstrl.toml has no
    ``[stack]``. Raises :class:`StackRefused` naming the reason when the
    most recent APPROVED ``stack_confirmation`` item does not carry the
    current digest, when there is none, or when the inbox cannot be read;
    and :class:`StackError` for a malformed table, as :func:`load_stack`.
    """
    stack = load_stack(root_dir)
    if stack is None:
        return None
    reason, confirmed_by = _refusal(root_dir, stack)
    if reason:
        raise StackRefused(replace(stack, unconfirmed=reason))
    return replace(stack, unconfirmed="", confirmed_by=confirmed_by)


def stack_in_force(root_dir: Path) -> Stack | None:
    """What a config loader holds: the confirmed stack, or the stack with
    ``unconfirmed`` set to why it may not run. Never raises a refusal, so a
    command that runs no check (``ks status``, ``ks inbox approve``) still
    loads; the runners refuse an unconfirmed stack's commands."""
    try:
        return confirmed_stack(root_dir)
    except StackRefused as refused:
        return refused.stack


def stack_evidence(root_dir: Path, stack: Stack) -> dict[str, Any]:
    """What a stack item carries: the digest, the table and where it came from."""
    from kstrl.config import resolve_config_file

    return {
        "stack_digest": stack.digest,
        "stack": {
            "instructions": stack.instructions,
            "setup": stack.setup,
            "checks": [list(check) for check in stack.checks],
            "env": list(stack.env),
            "up": stack.up,
        },
        "source": str(resolve_config_file(root_dir)),
    }


def file_stack_item(
    root_dir: Path,
    stack: Stack,
    *,
    run_id: str = "",
    quiet: bool = False,
    replay: dict[str, Any] | None = None,
) -> InboxItem:
    """File (or bump) the open stack_confirmation item for ``stack``.

    One item per digest while it is open: ``Inbox.add`` collapses a repeat
    onto it. Raises :class:`ValueError` when ``[inbox]`` is disabled or the
    inbox cannot be read, so no line is ever appended to an inbox kstrl
    could not read back; otherwise raises what ``Inbox.add`` raises.
    ``quiet`` pages nobody: the person who answered the prompt is there.
    ``replay`` is the record of a clean replay of this text (#700 slice 3),
    carried in the evidence; one that failed keeps the stack from being
    confirmed (:func:`replay_refuses`).
    """
    from kstrl.observability import NotifyConfig, NotifyHooks

    config = InboxConfig.load(root_dir)
    if not config.enabled:
        raise ValueError("[inbox] is disabled")
    _latest_approval(root_dir, config)
    evidence = stack_evidence(root_dir, stack)
    replayed = ""
    if replay is not None:
        evidence["replay"] = replay
        failed = str(replay.get("failed", ""))
        replayed = f" Its clean replay {'failed at ' + failed if failed else 'passed'}."
    box = Inbox(root_dir, config)
    return box.add(
        ItemKind.STACK_CONFIRMATION,
        f"Confirm the [stack] {stack.digest[:12]} in kstrl.toml",
        detail=(
            "kstrl runs no command of a [stack] a person has not confirmed. Checks, in order: "
            + ", ".join(f"{name} `{command}`" for name, command in stack.checks)
            + (f"; setup `{stack.setup}`" if stack.setup else "; no setup")
            + (f"; up `{stack.up}`" if stack.up else "")
            + "."
            + replayed
            + " ks inbox approve <id> confirms this text; ks inbox reject <id> --comment ... "
            "refuses it."
        ),
        run_id=run_id,
        dedupe_key=stack_dedupe_key(stack.digest),
        evidence=evidence,
        notify=NotifyHooks(NotifyConfig(), run_id=run_id) if quiet else None,
    )


def unconfirmed_lines(root_dir: Path, stack: Stack, *, run_id: str = "") -> list[str]:
    """What a refusal of the unconfirmed ``stack`` says, after filing its item.

    Every surface that refuses one (``ks factory``, the run's pre-spend
    checks, ``ks serve``) says it the same way and files the same one item.
    """
    lines = [f"the [stack] in kstrl.toml {stack.unconfirmed}. Nothing was run."]
    try:
        item = file_stack_item(root_dir, stack, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - a filing that failed is said, never raised
        lines.append(
            f"No confirmation item was filed ({exc}). Run ks factory in a terminal and "
            "answer the stack checkpoint, or fix the inbox and run this again."
        )
        return lines
    lines.append(
        f"ks inbox approve {item.id[:8]} confirms {stack.digest[:12]}; ks inbox reject "
        f"{item.id[:8]} --comment ... refuses it; ks inbox show {item.id[:8]} shows the table."
    )
    return lines


def decide_at_prompt(root_dir: Path, stack: Stack, *, confirm: bool, actor: str) -> str:
    """Record an answer given at the stack checkpoint; returns what to tell the operator.

    With the inbox on, the answer is an inbox decision, so the next run finds
    it. With it off (#696 decision 1(i)), a confirmation holds for this run
    only and a rejection is not recorded anywhere.
    """
    config = InboxConfig.load(root_dir)
    if not config.enabled:
        if confirm:
            _CONFIRMED_THIS_RUN.add(stack.digest)
            return "[inbox] is disabled: this confirmation holds for this run only"
        return "[inbox] is disabled: this rejection holds for this run only"
    item = file_stack_item(root_dir, stack, quiet=True)
    box = Inbox(root_dir, config)
    comment = "decided at the ks factory stack checkpoint"
    if confirm:
        box.approve(item.id, actor=actor, comment=comment)
        return f"confirmed {stack.digest[:12]} in the inbox ({item.id[:8]})"
    box.reject(item.id, actor=actor, comment=comment)
    return f"rejected {stack.digest[:12]} in the inbox ({item.id[:8]})"


@dataclass
class StackConfig:
    """The entry check's handle on ``[stack]`` (``config_preflight``).

    It loads the stack only to report a bad one, once and under ``[stack]``,
    before the three loaders that also read it for their own sections
    (``FactoryConfig``, ``VerifyConfig``, ``ContractConfig``), which are the
    ones that hold it. No field, so ``ks config show`` renders no row for it.
    """

    @classmethod
    def load(cls, root_dir: Path | None = None) -> StackConfig:
        load_stack(Path.cwd() if root_dir is None else root_dir)
        return check_numbers(cls())
