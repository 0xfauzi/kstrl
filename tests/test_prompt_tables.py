"""H3: the enrolled tables agree with each other (#530 split).

Split out of ``tests/test_prompt_versions.py`` when enrolling
``GEPA_REFLECTION_PROMPT`` would have taken that file past the repo's
800-line gate, the reason ``tests/test_prompt_enrollment_walk.py`` was
split from it before. That file holds the tables (``_PROMPTS``,
``_VERSIONS``, ``_EXPECTED_SNAPSHOTS``) and checks each prompt against its
snapshot; this one checks the tables against each other, so a row added
to one and not the others fails here.
"""

from __future__ import annotations

import re

from tests.test_prompt_versions import _EXPECTED_SNAPSHOTS, _PROMPTS, _VERSIONS

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_all_prompt_versions_are_semver() -> None:
    for name, value in _VERSIONS.items():
        assert _SEMVER_RE.match(value), (
            f"{name}_VERSION={value!r} must be semver (MAJOR.MINOR.PATCH)."
        )


def test_versions_and_snapshots_agree_on_version_string() -> None:
    """Catches the case where a developer updates ``_EXPECTED_SNAPSHOTS``
    but forgets to update the matching ``*_PROMPT_VERSION`` constant
    (or vice versa). Both stores of the version string must match."""
    for name in _PROMPTS:
        live_version = _VERSIONS[name]
        recorded_version = _EXPECTED_SNAPSHOTS[name][1]
        assert live_version == recorded_version, (
            f"Version drift for {name}: "
            f"live constant says {live_version!r}, "
            f"_EXPECTED_SNAPSHOTS says {recorded_version!r}. "
            "Either bump the constant to match the snapshot, or update "
            "the snapshot to match the constant. They must agree."
        )


def test_every_prompt_has_a_version() -> None:
    for prompt_name in _PROMPTS:
        assert prompt_name in _VERSIONS, (
            f"{prompt_name} is missing a {prompt_name}_VERSION constant. "
            "Every adversarial prompt must declare a semver version."
        )


def test_every_version_has_a_prompt() -> None:
    for prompt_name in _VERSIONS:
        assert prompt_name in _PROMPTS, (
            f"{prompt_name}_VERSION declared but no matching prompt body. "
            "Dead version constants drift; remove them."
        )


def test_every_snapshot_has_a_prompt() -> None:
    """The reverse of ``test_every_prompt_has_a_recorded_snapshot``.

    The hash check is now derived from ``_PROMPTS``, so a snapshot row
    whose prompt was deleted is parametrized over by nothing and silently
    stops meaning what it says. That is what happened to
    DEFAULT_PRD_PROMPT. Dead rows rot; remove them."""
    for name in _EXPECTED_SNAPSHOTS:
        assert name in _PROMPTS, (
            f"_EXPECTED_SNAPSHOTS has a row for {name!r}, which is not an "
            "enrolled prompt. If the prompt was deleted, delete its "
            "snapshot row too: nothing checks it any more."
        )


def test_every_prompt_has_a_recorded_snapshot() -> None:
    for name in _PROMPTS:
        assert name in _EXPECTED_SNAPSHOTS, (
            f"{name} is missing a recorded snapshot in _EXPECTED_SNAPSHOTS. "
            "Every adversarial prompt must be snapshot-protected."
        )
