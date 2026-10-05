"""The record of an agent process group, kept while kstrl owns it (#642).

The leash (``kstrl/agents/leash.py``) ends an agent's group when the
kstrl process that owns it dies. If the leash is killed in the same
moment, nothing is left to end the group, and the agent keeps running
with no kstrl process that knows about it. This record is how the next
kstrl command in the project finds such a group.

``DeadlineStreamer`` writes one record per spawn, once the leash has
started the agent, and removes it in ``_settle``, the tail of every
orderly disposal. A record still on disk therefore means its owner never
reached ``_settle``: it was killed, or it is still running.

WHERE. Under the XDG control directory of the project
(``statedir.control_dir``), outside every tree an agent can write, so an
agent cannot forge or delete a record (owner decision 3 (a) on #642).
Clones that share an ``origin`` share that directory, so they see each
other's records; a record whose owner is alive is never reported.

WHAT IT HOLDS. The group id, the nonce passed to the leash as an argv
(so the leader's command line carries it), the owner's pid, the agent's
working directory and its command. A reader that cannot parse a record
REFUSES rather than skipping it: a skipped record is an agent nobody
reports.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_json
from kstrl.jsonread import read_json
from kstrl.statedir import control_dir

#: The directory under the control directory that holds the records.
SPAWN_RECORD_DIRNAME = "agents"

#: What a nonce looks like: ``secrets.token_hex(16)``. Checked on read
#: because an empty or short nonce is a substring of almost any command
#: line, and the identity check would then pass for any process.
_NONCE = re.compile(r"[0-9a-f]{32}")


class SpawnRecordError(ValueError):
    """A record on disk that this reader cannot believe."""


@dataclass(frozen=True)
class SpawnRecord:
    """One agent process group kstrl started and has not yet disposed of."""

    #: The leash's pid, which is the group's id: the leash leads it.
    pgid: int
    #: Passed to the leash as an argv, so it is in the leader's command.
    nonce: str
    #: The kstrl process that started the agent and holds its lifeline.
    owner_pid: int
    cwd: str
    command: tuple[str, ...]


def new_nonce() -> str:
    """A nonce for one spawn."""
    return secrets.token_hex(16)


def spawn_record_path(root_dir: Path | None, nonce: str) -> Path | None:
    """Where one spawn's record goes, with its directory created.

    None when the caller has no project root (a probe in a scratch
    directory, a test driving the streamer directly): there is no
    project whose next command would read it. Called BEFORE the spawn,
    so a directory that cannot be created stops the agent from starting
    rather than leaving it running unrecorded. The error is a plain
    ``OSError`` carrying the cause, never a ``FileNotFoundError``, which
    the claude adapter reports as a missing CLI.
    """
    if root_dir is None:
        return None
    directory = control_dir(root_dir) / SPAWN_RECORD_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create the agent record directory {directory}: {exc}") from exc
    return directory / f"{nonce}.json"


def write_spawn_record(path: Path, record: SpawnRecord) -> None:
    """Write ``record`` at ``path``. Raises ``OSError`` as a plain ``OSError``."""
    try:
        atomic_write_json(
            path,
            {
                "pgid": record.pgid,
                "nonce": record.nonce,
                "owner_pid": record.owner_pid,
                "cwd": record.cwd,
                "command": list(record.command),
            },
        )
    except OSError as exc:
        raise OSError(f"cannot write the agent record {path}: {exc}") from exc


def forget_spawn_record(path: Path | None) -> None:
    """Remove a record. A removal that fails leaves a record whose group
    the next reader finds empty, and that reader removes it then."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_spawn_records(root_dir: Path) -> list[tuple[Path, SpawnRecord]]:
    """Every record for ``root_dir``, with its path.

    No directory is no records. A directory that cannot be listed or a
    file that cannot be read raises ``OSError``, and a file that does not
    parse as a record raises :class:`SpawnRecordError`: both are refusals
    for the caller, never an empty answer. Only ``*.json`` names are
    records; ``atomicio`` writes through ``*.tmp`` names.
    """
    directory = control_dir(root_dir) / SPAWN_RECORD_DIRNAME
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    paths = [directory / name for name in names if name.endswith(".json")]
    return [(path, _read_one(path)) for path in paths]


def _read_one(path: Path) -> SpawnRecord:
    raw = path.read_bytes()
    try:
        payload: Any = read_json(raw)
    except ValueError as exc:
        raise SpawnRecordError(f"{path}: not a JSON document: {exc}") from exc
    if not isinstance(payload, dict):
        raise SpawnRecordError(f"{path}: expected an object, got {type(payload).__name__}")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise SpawnRecordError(f"{path}: nonce must be 32 lowercase hex digits, got {nonce!r}")
    command = payload.get("command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise SpawnRecordError(f"{path}: command must be a list of strings, got {command!r}")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        raise SpawnRecordError(f"{path}: cwd must be a string, got {cwd!r}")
    return SpawnRecord(
        pgid=_int_field(path, payload, "pgid", minimum=2),
        nonce=nonce,
        owner_pid=_int_field(path, payload, "owner_pid", minimum=1),
        cwd=cwd,
        command=tuple(command),
    )


def _int_field(path: Path, payload: dict[str, Any], name: str, *, minimum: int) -> int:
    """An integer field at or above ``minimum``. A bool is refused: ``isinstance(True, int)``
    holds, and a bool is never a pid. A group id below 2 is refused because
    ``killpg(1, ...)`` is every process this user owns."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SpawnRecordError(f"{path}: {name} must be an integer >= {minimum}, got {value!r}")
    return value
