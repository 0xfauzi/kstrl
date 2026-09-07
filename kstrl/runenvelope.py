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

``AutonomyState`` reads ``.kstrl/autonomy.json`` rather than
``kstrl.toml``. It is in the envelope because it is the other half of
the same per-component read: the level Phase 1 hands the adequacy gate
was ``AutonomyState.load(root).level``, unclamped, while the factory had
already resolved a CLAMPED level three lines above the hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kstrl.adequacy import AdequacyConfig
from kstrl.autonomy import AutonomyConfig, AutonomyState
from kstrl.config import toml_parse_scope
from kstrl.policy import PolicyConfig


@dataclass(frozen=True)
class RunEnvelope:
    """One run's resolved policy envelope, adequacy posture and autonomy level.

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
    #: The level the run OPERATES at, after ``resolve_runtime_level``
    #: clamps the stored level by ``[autonomy] max_level``, by the
    #: policy envelope ceiling and by control-state location. 0 when the
    #: ladder is off.
    autonomy_level: int

    @classmethod
    def load(
        cls,
        root_dir: Path,
        *,
        policy_override: PolicyConfig | None = None,
    ) -> RunEnvelope:
        """Resolve the envelope from disk in one document parse.

        ``policy_override`` is ``FactoryConfig.policy_config``, the
        injection seam callers already use to hand the factory a policy
        it did not read.

        The level here is the RAW stored level. The factory replaces it
        with the clamped one at the ladder resolution; a caller that
        never runs the ladder (a test, an embedded pipeline) gets the
        stored level, which is what it got before this module existed.

        ``AutonomyState.load`` stays OUTSIDE the scope deliberately: it
        reads JSON, which ``toml_parse_scope`` does not cache, and it
        costs 18.3ms (14.6 of them ``ensure_control_state``) against the
        sub-millisecond window that scope's docstring says makes a
        stale document safe.
        """
        with toml_parse_scope():
            policy = policy_override or PolicyConfig.load(root_dir)
            adequacy = AdequacyConfig.load(root_dir)
            autonomy = AutonomyConfig.load(root_dir)
        level = AutonomyState.load(root_dir).level if autonomy.enabled else 0
        return cls(
            policy=policy,
            adequacy=adequacy,
            autonomy=autonomy,
            autonomy_level=level,
        )

    def policy_hash(self) -> str:
        """The hash ``manifest.policy_hash`` records, and the only site
        that computes it after #192, which is what makes "the hash
        covers what was enforced" true by construction."""
        return self.policy.envelope_hash()
