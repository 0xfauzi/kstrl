"""Whether the TUI can carry a retry, and the CLI command that can (#433 G1).

A retry re-enters the factory through ``FactoryLaunch``, which has a
field for two recorded flags, ``max_parallel`` and ``review_mode``
(:data:`CARRIED_FLAGS`), and none for any other flag or for a run limit
(#436, #526). So a refusal of another flag or of a limit is a refusal of
THIS surface, not of the retry: ``ks retry`` can run it with the options
the refusal names. A launch record ``plan_resume`` cannot use is refused
by ``ks retry`` too, so that refusal names no command (#433 H4).

The failure queue asks once per read, off the event loop, and the row,
the detail and the ``r`` binding all answer from the same ``Carry``. The
options come from ``plan_resume``'s ``UnkeptLimit`` data, never from its
wording, so a refusal the TUI cannot word still names the right options.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.config_preflight import SURFACE_REJECTIONS, raise_if_defect
from kstrl.launch import FactoryLaunch
from kstrl.launch_record import (
    FlagValue,
    LaunchRecordError,
    launch_record_path,
    option_argv,
    read_launch_record,
)
from kstrl.retry_plan import UnkeptLimit, limits_line, not_replayed, plan_resume
from kstrl.tui.theme import short_run_id

if TYPE_CHECKING:
    from kstrl.manifest import Manifest

#: The placeholder a command shows for a value no record holds.
VALUE = "N"
VALUE_NOTE = f"{VALUE}: a value keeps that limit; 0 runs without it"

#: The recorded flags ``FactoryLaunch`` has a field for; ``session._prepare_factory``
#: applies each the way ``ks factory`` applies the option (#433 H5).
CARRIED_FLAGS = ("max_parallel", "review_mode")


@dataclass(frozen=True)
class Carry:
    #: What the relaunch runs under, "" when refused.
    runs_under: str = ""
    #: Why the TUI cannot carry the retry, "" when it can.
    refusal: str = ""
    #: The run limits the refusal names; () for any other refusal.
    unkept: tuple[UnkeptLimit, ...] = ()
    #: ``--option is no longer an option. Why.`` for each recorded flag
    #: the retry leaves out (#539).
    not_replayed: tuple[str, ...] = ()
    #: The recorded flags the relaunch passes on, and their spelling (#433 H5).
    carried: tuple[tuple[str, FlagValue], ...] = ()
    replays: str = ""
    #: The paths a launch record refusal is about, by what each is (#433 H4).
    details: tuple[tuple[str, str], ...] = ()

    def launch(self, manifest_file: Path) -> FactoryLaunch:
        """The relaunch, carrying every recorded flag ``FactoryLaunch`` has a field for."""
        flags = dict(self.carried)
        parallel, mode = flags.get("max_parallel"), flags.get("review_mode")
        return FactoryLaunch(
            manifest_path=manifest_file,
            max_parallel=None if parallel is None else int(parallel),
            review_mode=None if mode is None else str(mode),
        )

    def command(self, component_id: str) -> str:
        """The ``ks retry`` command that carries what this surface cannot."""
        options = [f"{limit.option} {_value(limit)}" for limit in self.unkept]
        return " ".join(["ks retry", component_id, *options])

    @property
    def needs_value(self) -> bool:
        return any(limit.ran_under is None for limit in self.unkept)


def _value(limit: UnkeptLimit) -> str:
    """The recorded value, spelled as the option takes it, or ``N``."""
    if limit.ran_under is None:
        return VALUE
    value = limit.ran_under
    return str(int(value)) if float(value).is_integer() else str(value)


def _limit_refusal(run_id: str, unkept: tuple[UnkeptLimit, ...]) -> str:
    run = f"run {short_run_id(run_id)}" if run_id else "the run"
    unrecorded = sum(1 for limit in unkept if limit.ran_under is None)
    parts = []
    if unrecorded:
        parts.append(
            f"{run} left no launch record of {unrecorded} run limit(s), "
            "and the environment and kstrl.toml set none"
        )
    if len(unkept) > unrecorded:
        parts.append(
            f"{run} ran under {len(unkept) - unrecorded} run limit(s) "
            "that the environment and kstrl.toml no longer set"
        )
    return "; ".join(parts)


def read_carry(root: Path, manifest: Manifest, manifest_file: Path) -> Carry:
    """``plan_resume`` for the TUI's own relaunch (worker thread).

    ``plan_resume`` loads ``FactoryConfig`` and lets a config rejection
    propagate, so the same guard as ``session._prepare_factory`` is here
    (#289's defect class).
    """
    from kstrl.cli import factory as factory_command

    try:
        # The read plan_resume makes first, taken here for the refusal's
        # cause and paths: plan_resume keeps only its wording (#433 H4).
        read_launch_record(root, manifest, manifest_file)
    except LaunchRecordError as exc:
        return Carry(refusal=exc.cause or str(exc), details=exc.paths)
    try:
        plan, problems, unkept = plan_resume(
            root,
            manifest,
            manifest_file,
            factory_command,
            max_cost_usd=None,
            max_parallel=None,
            keep_worktrees_on_failure=False,
        )
    except SURFACE_REJECTIONS as exc:
        raise_if_defect(exc)
        return Carry(refusal=f"failed to load configuration: {exc}")
    if unkept:
        return Carry(refusal=_limit_refusal(manifest.run_id, unkept), unkept=unkept)
    if plan is None:
        # plan_resume refuses only a launch record here (a recorded option
        # `ks factory` lacks), and `ks retry` makes the same call (#433 H4).
        record = launch_record_path(root, manifest.run_id)
        return Carry(
            refusal=problems[0] if problems else "no resume plan",
            details=(("launch record", str(record)),),
        )
    dropped = not_replayed(plan)
    # A plain flag recorded off spells as nothing, so there is nothing to drop.
    uncarried = [
        shlex.join(spelled)
        for name, value in plan.flags
        if name not in CARRIED_FLAGS and (spelled := option_argv(factory_command, {name: value}))
    ]
    if uncarried:
        # FactoryLaunch has no field for these; `ks retry` replays them (#436).
        run = f"run {short_run_id(plan.run_id)}" if plan.run_id else "the run"
        return Carry(
            refusal=f"{run} was launched with {', '.join(uncarried)}, "
            "which this screen cannot pass on",
            not_replayed=dropped,
        )
    runs_under = f"{limits_line(plan)}, {plan.max_parallel} in parallel"
    return Carry(
        runs_under=runs_under,
        not_replayed=dropped,
        carried=plan.flags,
        replays=shlex.join(plan.argv),
    )
