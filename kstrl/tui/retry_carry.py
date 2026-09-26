"""Whether the TUI can carry a retry, and the CLI command that can (#433 G1).

A retry re-enters the factory through ``FactoryLaunch``, which has no
field for a recorded flag and no field for a run limit (#436, #526). So
``plan_resume``'s refusal is a refusal of THIS surface, not of the
retry: ``ks retry`` can run it with the options the refusal names.

The failure queue asks once per read, off the event loop, and the row,
the detail and the ``r`` binding all answer from the same ``Carry``. The
options come from ``plan_resume``'s ``UnkeptLimit`` data, never from its
wording, so a refusal the TUI cannot word still names the right options.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.config_preflight import SURFACE_REJECTIONS, raise_if_defect
from kstrl.retry_plan import UnkeptLimit, limits_line, not_replayed, plan_resume
from kstrl.tui.theme import short_run_id

if TYPE_CHECKING:
    from kstrl.manifest import Manifest

#: The placeholder a command shows for a value no record holds.
VALUE = "N"
VALUE_NOTE = f"{VALUE}: a value keeps that limit; 0 runs without it"


@dataclass(frozen=True)
class Carry:
    #: What the relaunch runs under, "" when refused.
    runs_under: str = ""
    #: Why the TUI cannot carry the retry, "" when it can.
    refusal: str = ""
    #: The run limits the refusal names; () for any other refusal.
    unkept: tuple[UnkeptLimit, ...] = ()
    #: ``--option, why`` for each recorded flag the retry leaves out (#539).
    not_replayed: tuple[str, ...] = ()

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
        return Carry(refusal="; ".join(problems) or "no resume plan")
    dropped = not_replayed(plan)
    if plan.argv:
        # FactoryLaunch has no field for a recorded flag; `ks retry`
        # replays them (#436).
        return Carry(
            refusal="the recorded run's flags cannot be carried through the TUI",
            not_replayed=dropped,
        )
    runs_under = f"{limits_line(plan)}, {plan.max_parallel} in parallel"
    return Carry(runs_under=runs_under, not_replayed=dropped)
