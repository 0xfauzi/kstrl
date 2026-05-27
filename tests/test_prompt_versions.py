"""H3: Snapshot tests for the adversarial prompts.

These tests are the enforcement mechanism for the prompt-versioning policy
described in CLAUDE.md and docs/adversarial-design.md. The flow is:

    1. Developer edits one of DECOMPOSE_PROMPT, REVIEWER_PROMPT,
       SECURITY_PROMPT, DISTILL_PROMPT.
    2. test_<prompt>_snapshot_unchanged fails because the SHA-256 of the
       prompt text no longer matches _EXPECTED_HASHES.
    3. Developer must:
       a. Bump the corresponding *_PROMPT_VERSION constant (semver).
       b. Re-run the calibration suite:
            RALPH_RUN_CALIBRATION=1 RALPH_CALIBRATION_MODEL=haiku \\
                uv run pytest tests/test_calibration.py -v
       c. If detection rate held or improved, update _EXPECTED_HASHES below
          to the new SHA. The git diff in the PR shows both the prompt
          change and the hash change side by side, making prompt drift
          impossible to land unreviewed.

The hash snapshot is intentionally an exact-match check. The cost of a
spurious failure (a developer reformats a prompt and has to update the
hash) is one minute. The cost of a silent prompt drift that drops
detection rate is unbounded.
"""

from __future__ import annotations

import hashlib
import re

from ralph_py.decompose import DECOMPOSE_PROMPT, DECOMPOSE_PROMPT_VERSION
from ralph_py.knowledge import DISTILL_PROMPT, DISTILL_PROMPT_VERSION
from ralph_py.review import REVIEWER_PROMPT, REVIEWER_PROMPT_VERSION
from ralph_py.security import SECURITY_PROMPT, SECURITY_PROMPT_VERSION


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_EXPECTED_HASHES = {
    "DECOMPOSE_PROMPT": "89c9d6a70512676aefffa643558fbb71a6937682c56a9fd3943b69c49eb2abcf",
    "REVIEWER_PROMPT": "9307260aee8aedeea22d7fcb8e28131421013a915ec697d71f3428996bc434ca",
    "SECURITY_PROMPT": "b70df9754eef21179f83dfbc772dd16490d89557fabc8faa6f31ae2646fae946",
    "DISTILL_PROMPT": "489219257322678f22dfdf22cedec2be0173e2685402314a081f9d31834205ed",
}

_PROMPTS = {
    "DECOMPOSE_PROMPT": DECOMPOSE_PROMPT,
    "REVIEWER_PROMPT": REVIEWER_PROMPT,
    "SECURITY_PROMPT": SECURITY_PROMPT,
    "DISTILL_PROMPT": DISTILL_PROMPT,
}

_VERSIONS = {
    "DECOMPOSE_PROMPT_VERSION": DECOMPOSE_PROMPT_VERSION,
    "REVIEWER_PROMPT_VERSION": REVIEWER_PROMPT_VERSION,
    "SECURITY_PROMPT_VERSION": SECURITY_PROMPT_VERSION,
    "DISTILL_PROMPT_VERSION": DISTILL_PROMPT_VERSION,
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _drift_message(name: str, actual: str) -> str:
    return (
        f"{name} content changed without updating the snapshot.\n"
        f"  Expected SHA-256: {_EXPECTED_HASHES[name]}\n"
        f"  Actual SHA-256:   {actual}\n\n"
        "To land this change:\n"
        f"  1. Re-run calibration: RALPH_RUN_CALIBRATION=1 "
        "RALPH_CALIBRATION_MODEL=haiku uv run pytest tests/test_calibration.py -v\n"
        f"  2. Bump {name}_VERSION in ralph_py/.\n"
        "  3. Update _EXPECTED_HASHES in tests/test_prompt_versions.py.\n"
        "The hash diff is the audit trail that the prompt change was reviewed."
    )


def test_decompose_prompt_snapshot_unchanged() -> None:
    actual = _sha256(DECOMPOSE_PROMPT)
    assert actual == _EXPECTED_HASHES["DECOMPOSE_PROMPT"], _drift_message(
        "DECOMPOSE_PROMPT", actual,
    )


def test_reviewer_prompt_snapshot_unchanged() -> None:
    actual = _sha256(REVIEWER_PROMPT)
    assert actual == _EXPECTED_HASHES["REVIEWER_PROMPT"], _drift_message(
        "REVIEWER_PROMPT", actual,
    )


def test_security_prompt_snapshot_unchanged() -> None:
    actual = _sha256(SECURITY_PROMPT)
    assert actual == _EXPECTED_HASHES["SECURITY_PROMPT"], _drift_message(
        "SECURITY_PROMPT", actual,
    )


def test_distill_prompt_snapshot_unchanged() -> None:
    actual = _sha256(DISTILL_PROMPT)
    assert actual == _EXPECTED_HASHES["DISTILL_PROMPT"], _drift_message(
        "DISTILL_PROMPT", actual,
    )


def test_all_prompt_versions_are_semver() -> None:
    for name, value in _VERSIONS.items():
        assert _SEMVER_RE.match(value), (
            f"{name}={value!r} must be semver (MAJOR.MINOR.PATCH)."
        )


def test_every_prompt_has_a_version() -> None:
    for prompt_name in _PROMPTS:
        version_name = f"{prompt_name}_VERSION"
        assert version_name in _VERSIONS, (
            f"{prompt_name} is missing a {version_name} constant. "
            "Every adversarial prompt must declare a semver version."
        )


def test_every_version_has_a_prompt() -> None:
    for version_name in _VERSIONS:
        prompt_name = version_name.removesuffix("_VERSION")
        assert prompt_name in _PROMPTS, (
            f"{version_name} declared but no matching {prompt_name} prompt. "
            "Dead version constants drift; remove them."
        )


def test_every_prompt_has_a_recorded_hash() -> None:
    for name in _PROMPTS:
        assert name in _EXPECTED_HASHES, (
            f"{name} is missing a recorded hash in _EXPECTED_HASHES. "
            "Every adversarial prompt must be snapshot-protected."
        )
