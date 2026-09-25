"""The global playbook: an append-only ledger of lesson operations (#509).

Slice 7 of #217, phase 2 of ``docs/continuous-learning-design.md``. A
store every project can append to, the four delta operations, and the
``[learning]`` opt-out. Nothing here writes a lesson on its own or puts
one into a prompt; slices 8 and 9 do that.

THE STORE IS A LEDGER, NOT A DOCUMENT. Every change is one JSON line
appended through ``appendio.append_records`` to
``$XDG_STATE_HOME/kstrl/global/playbook/ops.jsonl``, and the playbook is
what :func:`load_playbook` gets by folding those lines in order. The
design doc (section 9, M11) measured the alternative: six concurrent
writers rewriting one document kept between 50 and 80 of 150
contributions with no error raised, where the append kept 150 of 150 in
every run. Every project's runs write here, so concurrent writers are
the normal case, and ``lock=True`` is passed for the same reason
``signals.append_signals`` passes it.

A LINE THE FOLD CANNOT USE IS REFUSED, NEVER SKIPPED. A line that does
not parse, names an op outside :class:`OpKind`, adds an id twice, or
names a lesson id no earlier line added raises :class:`PlaybookError`
naming the line number (the #260 rule: an unreadable record is a
refusal, not an absence). The writer validates each op with the SAME
function the fold uses, so a write the fold would refuse never lands.
The cost is stated rather than hidden: a torn final line, left by a
writer killed mid-append, makes the fold refuse until someone removes
it by hand.

The record shape is ``ace-framework`` 0.13.0's ``Skill`` field names
(``id``, ``section``, ``keywords``, ``issue``, ``insight``, ``active``,
``used_count``, ``helpful_count``, ``harmful_count``, ``neutral_count``
and the two timestamps), plus kstrl's ``evidence``, ``scope``,
``target_signature`` and ``status``, so a later switch to that package
is a mapping rather than a redesign. kstrl does not depend on it: it
declares Python >=3.12 against kstrl's >=3.11, calls models outside
``kstrl.agents`` and the ``max_adversarial_calls`` cap, and saves its
store by rewriting the whole file. If it later supports Python 3.11, a
caller-supplied model callable and an append-only store, this module
should be replaced by it (#217 plan, section 3.3).

``status`` changes only through DEMOTE and RETIRE; UPDATE may not touch
it, ``active``, ``id`` or the timestamps. RETIRE also sets the ACE
``active`` flag to false, so the two fields cannot disagree about a
retired lesson.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from kstrl.appendio import append_records
from kstrl.config import ConfigError, _parse_bool, load_toml_section, resolve_config_file
from kstrl.decisions import enum_field_error, required_field_error
from kstrl.jsonread import read_json
from kstrl.statedir import CONTROL_APP_NAME, xdg_state_home

LEDGER_NAME = "ops.jsonl"


class PlaybookError(ValueError):
    """A ledger line, or an op about to be written, that the fold refuses."""


class OpKind(StrEnum):
    """The delta operations. The one vocabulary the fold and the writer check."""

    ADD = "ADD"
    UPDATE = "UPDATE"
    DEMOTE = "DEMOTE"
    RETIRE = "RETIRE"


class LessonStatus(StrEnum):
    ACTIVE = "active"
    DEMOTED = "demoted"
    RETIRED = "retired"


@dataclass(frozen=True)
class Lesson:
    """One playbook bullet. ACE's field names, plus kstrl's last four."""

    id: str
    section: str
    keywords: tuple[str, ...]
    issue: str
    insight: str
    active: bool
    used_count: int
    helpful_count: int
    harmful_count: int
    neutral_count: int
    created_at: str
    updated_at: str
    evidence: tuple[str, ...]
    scope: str
    target_signature: str
    status: LessonStatus

    def to_record(self) -> dict[str, Any]:
        record = {f.name: getattr(self, f.name) for f in fields(self)}
        record["keywords"] = list(self.keywords)
        record["evidence"] = list(self.evidence)
        record["status"] = str(self.status)
        return record


LESSON_FIELDS = tuple(f.name for f in fields(Lesson))

#: What an UPDATE may change. The status pair moves only through DEMOTE
#: and RETIRE, and identity and time only through the fold itself.
UPDATABLE_FIELDS = frozenset(LESSON_FIELDS) - {"id", "created_at", "updated_at", "status", "active"}

_STRING_FIELDS = frozenset(
    {"id", "section", "issue", "insight", "created_at", "updated_at", "scope", "target_signature"}
)
_COUNT_FIELDS = frozenset({"used_count", "helpful_count", "harmful_count", "neutral_count"})
_LIST_FIELDS = frozenset({"keywords", "evidence"})

#: A ``path:line`` citation: a name with a ``.`` or ``/`` in it followed
#: by ``:<digits>``. Matches ``kstrl/x.py:42`` and ``app.ts:7-19``; does
#: not match a failure signature (``review:prd_criterion``,
#: ``ruff:E501``) or a run id. Its miss, stated: a file with no
#: extension and no directory (``Makefile:3``).
_SOURCE_LINE_RE = re.compile(r"[\w-]*[./][\w./\\-]*:\d+")


def _evidence_item_error(prefix: str, item: str) -> str | None:
    if "\n" in item or "\r" in item:
        return f"{prefix}: contains a newline; evidence holds run ids and signatures, not code"
    if _SOURCE_LINE_RE.search(item):
        return (
            f"{prefix}: {item!r} has a path:line form; evidence holds run ids "
            "and signatures, not code"
        )
    return None


def _list_field_error(prefix: str, name: str, value: Any) -> str | None:
    if not isinstance(value, list):
        return f"{prefix}.{name}: must be a list of strings, got {type(value).__name__}"
    for index, item in enumerate(value):
        where = f"{prefix}.{name}[{index}]"
        if not isinstance(item, str) or not item.strip():
            return f"{where}: must be a non-empty string"
        if name == "evidence" and (error := _evidence_item_error(where, item)):
            return error
    return None


def _field_error(prefix: str, name: str, value: Any) -> str | None:
    """One lesson field, or the reason it is not one. The only definition."""
    if name in _STRING_FIELDS:
        return required_field_error(prefix, name, value)
    if name in _COUNT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"{prefix}.{name}: must be a non-negative integer, got {value!r}"
        return None
    if name in _LIST_FIELDS:
        return _list_field_error(prefix, name, value)
    if name == "active":
        return None if isinstance(value, bool) else f"{prefix}.active: must be true or false"
    if name == "status":
        return enum_field_error(prefix, name, value, [s.value for s in LessonStatus])
    return f"{prefix}.{name}: not a lesson field"


def _typed(name: str, value: Any) -> Any:
    if name in _LIST_FIELDS:
        return tuple(value)
    if name == "status":
        return LessonStatus(value)
    return value


def _keys_error(prefix: str, record: Mapping[str, Any], expected: frozenset[str]) -> str | None:
    missing, unexpected = expected - set(record), set(record) - expected
    if missing or unexpected:
        return f"{prefix}: missing {sorted(missing)}, unexpected {sorted(unexpected)}"
    return None


def _lesson_from_record(prefix: str, record: Any) -> Lesson:
    if not isinstance(record, dict):
        raise PlaybookError(f"{prefix}: must be a JSON object")
    if error := _keys_error(prefix, record, frozenset(LESSON_FIELDS)):
        raise PlaybookError(error)
    for name in LESSON_FIELDS:
        if error := _field_error(prefix, name, record[name]):
            raise PlaybookError(error)
    return Lesson(**{name: _typed(name, record[name]) for name in LESSON_FIELDS})


def _changes_from_record(prefix: str, changes: Any) -> dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise PlaybookError(f"{prefix}: must be a non-empty JSON object")
    refused = set(changes) - UPDATABLE_FIELDS
    if refused:
        raise PlaybookError(f"{prefix}: may not change {sorted(refused)}")
    for name, value in changes.items():
        if error := _field_error(prefix, name, value):
            raise PlaybookError(error)
    return {name: _typed(name, value) for name, value in changes.items()}


_OP_KEYS = {
    OpKind.ADD: frozenset({"op", "id", "at", "lesson"}),
    OpKind.UPDATE: frozenset({"op", "id", "at", "changes"}),
    OpKind.DEMOTE: frozenset({"op", "id", "at"}),
    OpKind.RETIRE: frozenset({"op", "id", "at"}),
}


@dataclass(frozen=True)
class Op:
    """One ledger line. ``lesson`` is for ADD, ``changes`` for UPDATE."""

    kind: OpKind
    lesson_id: str
    at: str
    lesson: Lesson | None = None
    changes: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"op": str(self.kind), "id": self.lesson_id, "at": self.at}
        if self.lesson is not None:
            record["lesson"] = self.lesson.to_record()
        if self.changes:
            record["changes"] = {
                name: list(value) if isinstance(value, tuple) else value
                for name, value in self.changes.items()
            }
        return record


def op_from_record(prefix: str, record: Any) -> Op:
    """Validate one raw ledger record and build its :class:`Op`.

    The writer and the fold both call this, so there is one definition
    of a valid line and no weaker second one for a gate to consult.
    """
    if not isinstance(record, dict):
        raise PlaybookError(f"{prefix}: must be a JSON object")
    if error := enum_field_error(prefix, "op", record.get("op"), [k.value for k in OpKind]):
        raise PlaybookError(error)
    kind = OpKind(record["op"])
    if error := _keys_error(prefix, record, _OP_KEYS[kind]):
        raise PlaybookError(error)
    for name in ("id", "at"):
        if error := required_field_error(prefix, name, record[name]):
            raise PlaybookError(error)
    if kind is OpKind.ADD:
        lesson = _lesson_from_record(f"{prefix}.lesson", record["lesson"])
        if lesson.id != record["id"]:
            raise PlaybookError(f"{prefix}: id {record['id']!r} but lesson.id {lesson.id!r}")
        return Op(kind, record["id"], record["at"], lesson=lesson)
    if kind is OpKind.UPDATE:
        changes = _changes_from_record(f"{prefix}.changes", record["changes"])
        return Op(kind, record["id"], record["at"], changes=changes)
    return Op(kind, record["id"], record["at"])


@dataclass(frozen=True)
class Playbook:
    """The folded ledger, and the exact bytes it was folded from."""

    path: Path
    lessons: tuple[Lesson, ...]
    line_count: int
    byte_count: int
    sha256: str


def playbook_dir() -> Path:
    """``$XDG_STATE_HOME/kstrl/global/playbook``, outside every repository."""
    return xdg_state_home() / CONTROL_APP_NAME / "global" / "playbook"


def ledger_path() -> Path:
    return playbook_dir() / LEDGER_NAME


def _apply(prefix: str, lessons: dict[str, Lesson], op: Op) -> None:
    if op.kind is OpKind.ADD:
        if op.lesson_id in lessons:
            raise PlaybookError(f"{prefix}: ADD of lesson {op.lesson_id!r}, which already exists")
        if op.lesson is None:
            raise PlaybookError(f"{prefix}: ADD carries no lesson")
        lessons[op.lesson_id] = op.lesson
        return
    current = lessons.get(op.lesson_id)
    if current is None:
        raise PlaybookError(f"{prefix}: {op.kind} names unknown lesson id {op.lesson_id!r}")
    if op.kind is OpKind.UPDATE:
        lessons[op.lesson_id] = replace(current, **op.changes, updated_at=op.at)
    elif op.kind is OpKind.DEMOTE:
        lessons[op.lesson_id] = replace(current, status=LessonStatus.DEMOTED, updated_at=op.at)
    else:
        lessons[op.lesson_id] = replace(
            current, status=LessonStatus.RETIRED, active=False, updated_at=op.at
        )


def _parse_line(prefix: str, line: bytes) -> Op:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlaybookError(f"{prefix}: not UTF-8: {exc}") from exc
    try:
        record = read_json(text)
    except json.JSONDecodeError as exc:
        raise PlaybookError(f"{prefix}: not a JSON record: {exc}") from exc
    return op_from_record(prefix, record)


def load_playbook(path: Path | None = None) -> Playbook:
    """Fold the ledger in order. A missing ledger is an empty playbook.

    Raises :class:`PlaybookError` naming the first line the fold cannot
    use, and lets ``OSError`` out for a ledger that exists and cannot be
    read. The digest is over exactly the bytes folded, read once.
    """
    path = ledger_path() if path is None else path
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    lessons: dict[str, Lesson] = {}
    for number, line in enumerate(lines, start=1):
        prefix = f"{path} line {number}"
        _apply(prefix, lessons, _parse_line(prefix, line))
    return Playbook(
        path=path,
        lessons=tuple(lessons.values()),
        line_count=len(lines),
        byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def append_ops(ops: Sequence[Op], path: Path | None = None) -> None:
    """Append ``ops`` as one write, after validating every one of them.

    Nothing is written unless every op passes :func:`op_from_record`,
    the fold's own validator. ``OSError`` is the caller's: see
    :func:`contribute` for the one that turns it into a warning.
    """
    path = ledger_path() if path is None else path
    lines: list[str] = []
    for index, op in enumerate(ops):
        record = op.to_record()
        op_from_record(f"op {index}", record)
        lines.append(json.dumps(record) + "\n")
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    append_records(path, "".join(lines), repair="", lock=True)


def contribute(
    ops: Sequence[Op],
    config: LearningConfig,
    *,
    warn: Callable[[str], None],
) -> int:
    """Append ``ops`` when this project contributes. Returns how many landed.

    ``contribute = false`` writes nothing and says nothing: it is the
    operator's choice, not a fault. An unreachable store is skipped with
    one warning and never replaced by a copy inside the repository
    (design doc section 6).
    """
    if not config.contribute:
        return 0
    try:
        append_ops(ops)
    except OSError as exc:
        warn(
            f"the global playbook at {playbook_dir()} is unreachable ({exc}); contribution skipped"
        )
        return 0
    return len(ops)


_CONTRIBUTE_ENV = "KSTRL_LEARNING_CONTRIBUTE"
_CONSUME_ENV = "KSTRL_LEARNING_CONSUME"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else _parse_bool(value)


def _toml_bool(section: Mapping[str, Any], key: str, default: bool) -> bool:
    """Strict: ``bool("false")`` is True, so a lenient cast would turn a
    quoted opt-out into a contribution."""
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(f"[learning] {key} must be true or false, got {value!r}")
    return value


@dataclass(frozen=True)
class LearningConfig:
    """``[learning]``: whether this project sends lessons to the global
    playbook and whether it reads them. Both default to true (design doc
    section 6, decided 2026-08-13: contribute by default, opt out per
    project)."""

    contribute: bool = True
    consume: bool = True

    @classmethod
    def from_env(cls) -> LearningConfig:
        defaults = cls()
        return cls(
            contribute=_env_bool(_CONTRIBUTE_ENV, defaults.contribute),
            consume=_env_bool(_CONSUME_ENV, defaults.consume),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> LearningConfig:
        """Precedence: env > toml > defaults, with one exception.

        A kstrl.toml that cannot be read or parsed makes ``contribute``
        false for this run whatever the environment says, because the
        opt-out may be in the file that could not be read (design doc
        section 6: a project must not leak by silence or by error).
        """
        if root_dir is None:
            root_dir = Path.cwd()
        try:
            section = load_toml_section(resolve_config_file(root_dir), "learning")
        except (OSError, ConfigError):
            return cls(contribute=False, consume=_env_bool(_CONSUME_ENV, cls().consume))
        return cls(
            contribute=_env_bool(_CONTRIBUTE_ENV, _toml_bool(section, "contribute", True)),
            consume=_env_bool(_CONSUME_ENV, _toml_bool(section, "consume", True)),
        )
