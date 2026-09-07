"""The config envelope a factory run resolves once and enforces throughout (#192).

``manifest.policy_hash`` exists so "what policy was this run held to?"
has one answer. Before this module it could have two: the hash was
computed once at run start while Phase 1 re-read ``[policy]``,
``[adequacy]`` and ``[autonomy]`` from ``kstrl.toml`` for every
component. Measured on a two-component run with the ladder off (the
default), an edit to ``kstrl.toml`` between the two components had the
second held to ``max_files_changed=500, deps_allow_new=true`` while the
manifest recorded the hash of ``max_files_changed=5,
deps_allow_new=false``. The adequacy posture flipped with it and nothing
recorded that at all.

This is #269 applied to config. ``RunScope`` already carries the run's
plan-time path snapshot for the same reason, and states it in
``pipeline.py``: the pipeline READS the snapshot and never resolves one
of its own, which is what stops Phase 1 and the in-loop guard drifting
apart. The envelope is the same defect with a different file.

Consequence for an operator: the resolution taken at run start wins for
the whole run, and an edit to ``kstrl.toml`` while a run is in flight
takes effect at the next run. There is deliberately no mid-run change
detector: after this module there is no per-component read left to
detect a change against.

WHY SEVEN SECTIONS AND NOT THREE
--------------------------------
Round 1 of this change put ``[policy]``, ``[adequacy]`` and
``[autonomy]`` here and left ``[sandbox]``, ``[fixtures]``, ``[inbox]``
and ``[divergence]`` being loaded by ``ComponentPipeline.__init__``. The
review measured what that cost: a malformed ``[inbox]`` reached the
constructor with no handler above it, so it left ``run_factory`` as a
raw ``ValueError``, and it did so ABOVE
``pipeline.record_architect_usage``, which is the line #257 exists to
reach - the architect's spend went unrecorded and ``serve`` charged $0
for a launch that had spent real money. Resolving all seven HERE, at a
point the run can still refuse from, is what closes that: see
:meth:`RunEnvelope.resolve` and its one caller in ``kstrl/factory.py``.
It also leaves ``[sandbox]`` with one resolution per run rather than
two (``ComponentPipeline.__init__`` and ``_run_factory_locked`` each
resolved it, which was the last "one section, two answers in one run"
this lane's own framing is about).

``AutonomyState`` reads ``.kstrl/autonomy.json`` rather than
``kstrl.toml``. It is in the envelope because it is the other half of
the same per-component read: the level Phase 1 hands the adequacy gate
was ``AutonomyState.load(root).level``, unclamped, while the factory had
already resolved a CLAMPED level three lines above the hash. The STATE
itself is carried, not just the level, because the factory's ladder
needs it and loading it twice costs a measured 17.2 ms and ensures the
control state twice. It is carried only when ``[autonomy] enabled``:
with the ladder off, which is the default, nothing reads the state and
the field is ``None``, so a project that never opted in reads no
``.kstrl/autonomy.json`` and runs no control-directory migration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from kstrl.adequacy import AdequacyConfig
from kstrl.autonomy import AutonomyConfig, AutonomyState
from kstrl.config import ConfigError, toml_parse_scope
from kstrl.config_preflight import resolve_or_report
from kstrl.divergence import DivergenceConfig
from kstrl.fixtures import FixturesConfig
from kstrl.inbox import InboxConfig
from kstrl.policy import PolicyConfig
from kstrl.sandbox import SandboxConfig

T = TypeVar("T")


@dataclass(frozen=True)
class RunEnvelope:
    """One run's resolved configuration, for every section a run enforces.

    Frozen: the factory clamps it with :func:`dataclasses.replace` at the
    ladder resolution and then hands the result to the pipeline, so
    every later reader holds an object nobody can edit underneath it.
    """

    policy: PolicyConfig
    adequacy: AdequacyConfig
    #: The ``[autonomy]`` section itself rather than a derived
    #: ``enabled`` bool: ``run_factory`` needs the object for
    #: ``resolve_runtime_level`` and the #262 probe gate, and reading it
    #: off the envelope is what stops it parsing ``kstrl.toml`` a second
    #: time nine lines later.
    autonomy: AutonomyConfig
    #: The stored ladder state, read once per run, and only when the
    #: ladder is on. The factory's ladder resolution reads it off here;
    #: before this field it loaded it a second time nine lines later,
    #: discarding the first result and paying 17.2 ms and a second
    #: ``ensure_control_state`` for it. ``None`` when ``[autonomy]
    #: enabled`` is false, which is the DEFAULT: nothing on that path
    #: consumes the state, and round 2 of #192 loaded it anyway, which
    #: bought the rare path 17.2 ms by charging the common one the same
    #: amount plus a control-directory migration and a warning about a
    #: ladder the run does not use.
    autonomy_state: AutonomyState | None
    #: The level the run OPERATES at, after ``resolve_runtime_level``
    #: clamps the stored level by ``[autonomy] max_level``, by the
    #: policy envelope ceiling and by control-state location. 0 when the
    #: ladder is off.
    autonomy_level: int
    #: The four the pipeline used to resolve for itself. Carried here so
    #: that a malformed one is a refusal the run can report rather than
    #: an exception out of a constructor (see the module docstring).
    sandbox: SandboxConfig
    fixtures: FixturesConfig
    inbox: InboxConfig
    divergence: DivergenceConfig

    @classmethod
    def resolve(
        cls,
        root_dir: Path,
        *,
        policy_override: PolicyConfig | None = None,
        fixtures_override: FixturesConfig | None = None,
    ) -> EnvelopeResolution:
        """The envelope, or the lines saying which section to fix.

        One document parse for all seven sections: the whole group is
        inside a single :func:`toml_parse_scope`, and each section goes
        through ``config_preflight.resolve_or_report`` rather than
        ``load_or_report`` because a NESTED scope replaces the outer
        cache instead of inheriting it, so the per-call variant would
        cost one parse per section.

        ``blame_env`` is False, and the reason is specific rather than
        conservative. Naming an environment variable means clearing
        ``os.environ`` process-wide, and by the time a run reaches here
        the caller may be a TUI worker thread. It also cannot lose
        anything: the entry preflight already resolved every one of
        these sections with ``blame_env=True`` before the command body
        ran, and the environment of a running process does not change
        underneath it, so the only input that can have gone bad since is
        the file - which ``_blamed_toml_value`` still names.

        ``policy_override`` and ``fixtures_override`` are
        ``FactoryConfig.policy_config`` and ``.fixtures_config``, the
        injection seams callers already use to hand the factory config
        it did not read.
        """
        problems: list[str] = []
        with toml_parse_scope():
            policy = policy_override or _resolved(PolicyConfig.load, root_dir, problems)
            adequacy = _resolved(AdequacyConfig.load, root_dir, problems)
            autonomy = _resolved(AutonomyConfig.load, root_dir, problems)
            sandbox = _resolved(SandboxConfig.load, root_dir, problems)
            fixtures = fixtures_override or _resolved(FixturesConfig.load, root_dir, problems)
            inbox = _resolved(InboxConfig.load, root_dir, problems)
            divergence = _resolved(DivergenceConfig.load, root_dir, problems)
        if (
            policy is None
            or adequacy is None
            or autonomy is None
            or sandbox is None
            or fixtures is None
            or inbox is None
            or divergence is None
        ):
            # ``problems`` is never empty here: ``_resolved`` writes a
            # line for every section that came back None, including the
            # one that did so without raising. A refusal with nothing to
            # print is an exit code 2 an operator cannot act on.
            return EnvelopeResolution(None, tuple(problems))
        # OUTSIDE the scope deliberately: this reads JSON, which
        # ``toml_parse_scope`` does not cache, and it costs a measured
        # 17.2 ms (most of it ``ensure_control_state``) against the
        # sub-millisecond window that scope's docstring says makes a
        # stale document safe.
        #
        # CONDITIONAL, because 17.2 ms on the default path buys nothing:
        # ``_resolve_ladder`` returns immediately when ``[autonomy]``
        # is disabled, so no consumer of this field exists there, and
        # ``autonomy_level`` is 0 either way. Round 2 of #192 read it
        # unconditionally so the ladder could take the object off the
        # envelope, and the measurement against ``origin/main`` was 0
        # ``AutonomyState.load`` and 0 ``ensure_control_state`` on a
        # default run before, 1 and 1 after, plus a ``RuntimeWarning``
        # about a ladder that run does not use when the stored state is
        # corrupt.
        state = AutonomyState.load(root_dir) if autonomy.enabled else None
        return EnvelopeResolution(
            cls(
                policy=policy,
                adequacy=adequacy,
                autonomy=autonomy,
                autonomy_state=state,
                autonomy_level=state.level if state is not None else 0,
                sandbox=sandbox,
                fixtures=fixtures,
                inbox=inbox,
                divergence=divergence,
            ),
            (),
        )

    @classmethod
    def load(
        cls,
        root_dir: Path,
        *,
        policy_override: PolicyConfig | None = None,
        fixtures_override: FixturesConfig | None = None,
    ) -> RunEnvelope:
        """:meth:`resolve`, raising instead of reporting.

        For a caller with no surface to report on - a test, an embedded
        pipeline. ``run_factory`` uses :meth:`resolve`, because a raise
        there is the defect this module's docstring records.

        The level here is the RAW stored level when the ladder is on,
        and 0 when it is off. The factory replaces it with the clamped
        one at the ladder resolution; a caller that never runs the
        ladder gets the stored level, which is what it got before this
        module existed.
        """
        resolved = cls.resolve(
            root_dir,
            policy_override=policy_override,
            fixtures_override=fixtures_override,
        )
        if resolved.envelope is None:
            raise ConfigError(
                "the run configuration cannot be resolved:\n  " + "\n  ".join(resolved.problems)
            )
        return resolved.envelope

    def policy_hash(self) -> str:
        """The hash ``manifest.policy_hash`` records, and the only site
        that computes it after #192, which is what makes "the hash
        covers what was enforced" true by construction."""
        return self.policy.envelope_hash()


@dataclass(frozen=True)
class EnvelopeResolution:
    """A resolved :class:`RunEnvelope`, or why there is not one.

    Exactly one side is populated: an envelope with no problems, or no
    envelope and at least one line naming a section and the input to
    fix. Returned as a pair rather than raised so ``run_factory`` can
    refuse the way its sibling pre-spend checks refuse - a sentence and
    exit 2 through ``_report_preflight`` - instead of handing the
    operator a traceback (#260 round 3 drew the same line for the
    architect decision register).
    """

    envelope: RunEnvelope | None
    problems: tuple[str, ...]


def _resolved(loader: Callable[[Path], T], root_dir: Path, problems: list[str]) -> T | None:
    """One section, appending its rejection line rather than raising.

    The line is GUARANTEED, and that is the point of doing it here
    rather than at each call. ``resolve_or_report`` is documented to
    return a value or a line and never neither, but nothing enforced it,
    and a loader that returned ``None`` without raising would have given
    :meth:`RunEnvelope.resolve` a ``None`` section with an empty
    ``problems``, which ``run_factory`` turns into exit code 2 with
    nothing printed. No loader in ``kstrl/`` does that today; this is
    what keeps the silence impossible rather than unlikely.
    """
    value, problem = resolve_or_report(loader, root_dir, blame_env=False)
    if problem is not None:
        problems.append(problem)
    elif value is None:
        name = getattr(loader, "__qualname__", repr(loader))
        problems.append(
            f"{name} resolved to no configuration and reported no problem. "
            "That is a defect in kstrl rather than in kstrl.toml."
        )
    return value
