"""The flags a factory run was launched with, recorded so a retry can replay them (#436).

`ks factory` records every option the operator passed on its command line,
minus the ones :data:`NOT_REPLAYED` names, in
``.kstrl/runs/<run_id>/launch.json`` before the run spends anything. `ks
retry` reads the record of the run its manifest names and re-enters `ks
factory` with the same options, so a retry of a run launched with
``--max-cost-usd 55 --max-parallel 2`` runs under the same ceiling and
parallelism rather than under whatever env and kstrl.toml happen to say.

The record carries the identity of the run and the manifest it belongs to,
and the reader refuses a record whose identity does not match. A record
that is present but cannot be read is a refusal, never an empty read:
reading it as empty would replay no flags, which is the defect itself.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click
from click.core import ParameterSource

from kstrl.atomicio import atomic_write_json
from kstrl.events import RunPaths
from kstrl.jsonread import read_json

if TYPE_CHECKING:
    from kstrl.manifest import Manifest

FlagValue = str | int | float | bool

#: The file name inside a run directory.
LAUNCH_RECORD_FILE = "launch.json"

#: `ks factory` options a retry never replays, and why. Every other option
#: the operator passed on the command line is recorded and replayed.
#: ``tests/test_retry_carries_flags.py`` pins this against the command.
NOT_REPLAYED: dict[str, str] = {
    "spec": "a retry resumes the manifest; it never decomposes a spec again",
    "manifest_path": "retry names its own manifest with --manifest",
    "root": "retry names its own root with --root",
    "project_name": "used only with --spec",
    "base_branch": "used only with --spec; the manifest carries its base branch",
    "single_pr": "the manifest is authoritative for single_pr",
    "progress_log": "a path of one invocation; retry takes its own --progress-log",
    "force_lock": "overrides another run's lock once; never carried to a later run",
    "yes": "retry asks its own confirmation and takes its own --yes",
    "tui": "retry runs on the terminal it was started from",
    "ui": "display only; retry takes its own --ui",
    "no_color": "display only; retry takes its own --no-color",
}


class LaunchRecordError(ValueError):
    """A launch record is present but cannot be used."""


@dataclass(frozen=True)
class LaunchRecord:
    run_id: str
    manifest: str
    flags: tuple[tuple[str, FlagValue], ...]
    max_cost_usd: float


def launch_record_path(root_dir: Path, run_id: str) -> Path:
    return RunPaths.for_run(root_dir, run_id).root / LAUNCH_RECORD_FILE


def replayable_flags(ctx: click.Context) -> tuple[tuple[str, FlagValue], ...]:
    """The options this invocation got from its command line, minus NOT_REPLAYED."""
    return tuple(
        (param.name, ctx.params[param.name])
        for param in ctx.command.params
        if param.name is not None
        and param.name not in NOT_REPLAYED
        and ctx.get_parameter_source(param.name) is ParameterSource.COMMANDLINE
    )


def write_launch_record(
    root_dir: Path,
    run_id: str,
    manifest_path: Path,
    flags: tuple[tuple[str, FlagValue], ...],
    max_cost_usd: float,
) -> list[str]:
    """Write the record; return why it could not be written, or [] on success."""
    path = launch_record_path(root_dir, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "runId": run_id,
                "manifest": str(manifest_path.resolve()),
                "flags": dict(flags),
                "maxCostUsd": max_cost_usd,
            },
        )
    except OSError as exc:
        return [f"{path}: {exc}"]
    return []


def _is_flag_value(value: object) -> bool:
    return isinstance(value, str | int | float | bool)


def _record_problem(payload: object, run_id: str, manifest_file: Path) -> str | None:
    """Why the raw payload is not this run's record, or None when it is."""
    if not isinstance(payload, dict):
        return "the record is not a JSON object"
    if payload.get("runId") != run_id:
        return f"runId is {payload.get('runId')!r}, but the manifest names run {run_id!r}"
    expected = str(manifest_file.resolve())
    if payload.get("manifest") != expected:
        return f"manifest is {payload.get('manifest')!r}, not {expected!r}"
    flags = payload.get("flags")
    if not isinstance(flags, dict):
        return "flags is not a JSON object"
    for name, value in flags.items():
        if not _is_flag_value(value):
            return f"flags[{name!r}] is {value!r}, not a string, number or boolean"
    ceiling = payload.get("maxCostUsd")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int | float):
        return f"maxCostUsd is {ceiling!r}, not a number"
    if not math.isfinite(ceiling) or ceiling < 0:
        return f"maxCostUsd is {ceiling!r}, not a finite number >= 0"
    return None


def read_launch_record(
    root_dir: Path,
    manifest: Manifest,
    manifest_file: Path,
) -> LaunchRecord | None:
    """The record of the run ``manifest`` names, or None when there is none.

    None means no record exists: the manifest names no run, or that run
    left no file (it predates #436, or died before writing one). A file
    that exists and cannot be read, or belongs to another run or another
    manifest, raises :class:`LaunchRecordError`.
    """
    if not manifest.run_id:
        return None
    path = launch_record_path(root_dir, manifest.run_id)
    # The I/O outside the parse guard, the `baseline.py` shape: no
    # handler below can be widened into catching an OSError.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LaunchRecordError(f"{path} cannot be read: {exc}") from exc
    try:
        payload = read_json(raw)
    except ValueError as exc:
        raise LaunchRecordError(f"{path} cannot be read: {exc}") from exc
    problem = _record_problem(payload, manifest.run_id, manifest_file)
    if problem is not None:
        raise LaunchRecordError(f"{path} is not the record of this run: {problem}")
    return LaunchRecord(
        run_id=payload["runId"],
        manifest=payload["manifest"],
        flags=tuple(payload["flags"].items()),
        max_cost_usd=float(payload["maxCostUsd"]),
    )


def _long_opt(param: click.Option) -> str:
    return next(opt for opt in param.opts if opt.startswith("--"))


def option_argv(command: click.Command, values: Mapping[str, object]) -> list[str]:
    """Spell ``values`` as ``command``'s own command line.

    None is "not passed". A boolean flag with an off switch
    (``--create-prs/--no-prs``) spells either side; a plain flag
    (``--no-verify``) is passed only when true. Raises LaunchRecordError
    for a name the command has no option for, or a value its option
    rejects, so a bad value is refused before anything is changed.
    """
    options = {p.name: p for p in command.params if isinstance(p, click.Option)}
    argv: list[str] = []
    for name, value in values.items():
        param = options.get(name)
        if param is None:
            raise LaunchRecordError(f"`ks {command.name}` has no option for {name!r}")
        if value is not None:
            argv.extend(_spell(param, value))
    return argv


def _spell(param: click.Option, value: object) -> list[str]:
    """One option and its value as command-line tokens, checked by its own type."""
    if isinstance(value, bool) != bool(param.is_flag):
        raise LaunchRecordError(f"{_long_opt(param)} {value!r}: wrong kind of value")
    try:
        param.type(value, param, None)
    except click.BadParameter as exc:
        raise LaunchRecordError(f"{_long_opt(param)} {value!r}: {exc.message}") from exc
    if param.is_flag and param.secondary_opts:
        return [_long_opt(param) if value else param.secondary_opts[0]]
    if param.is_flag:
        return [_long_opt(param)] if value else []
    return [_long_opt(param), str(value)]


def flags_argv(command: click.Command, flags: Mapping[str, FlagValue]) -> list[str]:
    """``option_argv`` for recorded flags, refusing any NOT_REPLAYED name."""
    for name in flags:
        if name in NOT_REPLAYED:
            raise LaunchRecordError(f"{name!r} is never replayed ({NOT_REPLAYED[name]})")
    return option_argv(command, flags)
