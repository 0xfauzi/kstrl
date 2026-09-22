"""The [release] section and the gate in front of a release that does
not exist yet (R8.7 slice 1, #154).

This module starts no process and reads no environment variable. Both
are held by guards rather than by prose:
``tests/test_release_gate.py`` runs the process-primitive walk over this
file and asserts it finds nothing, and asserts this module imports no
``os``. ``kstrl/release.py`` is also deliberately ABSENT from
``tests/test_process_lifecycle.py::EXPECTED_PROCESS_MODULES``, whose own
docstring pins an absent module to zero on all four columns, so a driver
added here fails that census in its own diff.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from kstrl.manifest import Component

RELEASE_SECTION = "release"

REASON_RELEASE_DISABLED = "release_disabled"
REASON_ENVIRONMENT_UNSET = "environment_unset"
REASON_RUN_NOT_CLEAN = "run_not_clean"
REASON_REF_UNRECORDED = "release_ref_unrecorded"
REASON_POLICY_DISABLED = "policy_disabled"
REASON_POLICY_DEPLOY_FALSE = "policy_deploy_false"
REASON_LADDER_WITHHELD = "ladder_withheld"
REASON_NO_DRIVER = "no_driver"

RELEASE_WITHHELD_REASONS: tuple[str, ...] = (
    REASON_RELEASE_DISABLED,
    REASON_ENVIRONMENT_UNSET,
    REASON_RUN_NOT_CLEAN,
    REASON_REF_UNRECORDED,
    REASON_POLICY_DISABLED,
    REASON_POLICY_DEPLOY_FALSE,
    REASON_LADDER_WITHHELD,
    REASON_NO_DRIVER,
)

#: One constant, referenced by ``factory.py`` and by the tests; never a
#: literal at two sites.
RELEASE_REF_RULE = "last_merge_by_completed_at"


@dataclass(frozen=True)
class ReleaseConfig:
    """``[release]`` config (R8.7 slice 1). Nothing here deploys.

    No ``from_env()`` on purpose, departing from the CLAUDE.md
    convention that every config dataclass has one: an environment door
    that may only tighten can only ever return the already-off default,
    which is a mechanism with no reachable effect. See
    ``tests/test_release_gate.py::test_the_release_module_reads_no_environment_variable``.
    """

    enabled: bool = False
    #: No default on purpose. tests/test_config_toml.py:400 pins that an
    #: unknown TOML key and an unknown section are both dropped in
    # codespell:ignore-next-line
    #: silence, so a misspelled key parses clean and falls back rather
    #: than raising. An empty environment is a refusal
    #: (REASON_ENVIRONMENT_UNSET), so the typo surfaces instead of
    #: releasing to a default nobody chose.
    environment: str = ""

    @classmethod
    def load(cls, root_dir: Path | None = None) -> ReleaseConfig:
        """Reads ``[release]``. TOML only: see the module docstring."""
        from kstrl.config import load_toml_section, resolve_config_file

        base = root_dir if root_dir is not None else Path.cwd()
        section = load_toml_section(resolve_config_file(base), RELEASE_SECTION)
        defaults = cls()
        enabled = bool(section["enabled"]) if "enabled" in section else defaults.enabled
        environment = (
            str(section["environment"]) if "environment" in section else defaults.environment
        )
        return cls(enabled=enabled, environment=environment)


@dataclass(frozen=True)
class ReleaseInputs:
    """Exactly what the gate reads. Pure data, no methods."""

    release_enabled: bool
    environment: str
    run_clean: bool
    release_ref: str
    policy_enabled: bool
    policy_deploy: bool
    ladder_deploy_permitted: bool | None


_WITHHELD_RULES: tuple[tuple[str, Callable[[ReleaseInputs], bool]], ...] = (
    (REASON_RELEASE_DISABLED, lambda i: not i.release_enabled),
    (REASON_ENVIRONMENT_UNSET, lambda i: not i.environment.strip()),
    (REASON_RUN_NOT_CLEAN, lambda i: not i.run_clean),
    (REASON_REF_UNRECORDED, lambda i: not i.release_ref),
    (REASON_POLICY_DISABLED, lambda i: not i.policy_enabled),
    (REASON_POLICY_DEPLOY_FALSE, lambda i: not i.policy_deploy),
    (REASON_LADDER_WITHHELD, lambda i: i.ladder_deploy_permitted is False),
)


def release_withheld(inputs: ReleaseInputs) -> str:
    """The FIRST reason this run does not release. Never "".

    Order. The two configuration rungs come first because they are the
    reason on every default run. The two RUN-STATE rungs come next,
    ahead of the policy rungs, for two reasons that agree: a run that
    did not finish clean, or produced no ref, has nothing to release
    whatever the permissions say; and putting them there is what lets
    an end-to-end test reach them by setting two inert TOML keys,
    instead of switching the policy envelope on inside a factory run.

    ``no_driver`` is the terminal rung and it is the whole containment
    mechanism of slice 1: every other gate can be opened by
    configuration, and this one cannot, because there is no code past
    it. The driver slice deletes this row and adds its own.
    """
    for reason, holds in _WITHHELD_RULES:
        if holds(inputs):
            return reason
    return REASON_NO_DRIVER


def release_ref_from(components: Sequence[Component]) -> str:
    """The ref this run would release, or "" when there is none.

    A CHOICE, not a derivation. The roadmap says "merge SHA of the
    final tier", and in ``create_prs`` per-component mode there is no
    tier merge, so this takes the merge the factory saw last:
    the greatest ``completed_at`` among components that recorded a
    ``merge_sha``. ``completed_at`` has one-second resolution
    (``kstrl/pipeline.py::_iso_now``), so ties are ordinary rather
    than exotic: the key carries the manifest index, so a tie goes to
    the LATER component in manifest order, which is the more
    downstream one. ``RELEASE_REF_RULE`` is stamped beside the value
    so a later reader can tell which rule wrote an old one.
    """
    merged = [(i, c) for i, c in enumerate(components) if c.merge_sha]
    if not merged:
        return ""
    return max(merged, key=lambda pair: (pair[1].completed_at, pair[0]))[1].merge_sha
