"""This repository's OWN committed baseline, read the way the workflow reads it.

Every other test about the dampener builds a baseline under ``tmp_path``. None
of them touches ``scripts/kstrl/sense-baseline.json``, which is this
repository's own committed baseline and the worked example
``docs/dampener.md`` points at. Round 2 of review on #357 measured the gap -
`grep -rn sense-baseline tests/` found only fixtures and a string constant.

What that costs. The baseline records a digest of the three verify commands and
the timeout, and ``ks sense --compare-baseline`` refuses a mismatch with exit 2.
So a `[verify]` command change in ``kstrl.toml``, a bump to
``BASELINE_SCHEMA_VERSION``, or moving ``[tool.mypy] files`` out of
``pyproject.toml`` (which flips ``_default_typecheck_command``) invalidates the
committed file with a green local suite, and the first signal is a red dampener
job on somebody else's next pull request. Here it is a red test on the commit
that did it.

The timeout is a constant here. It used to be read out of the dampener
workflow, which was this repository's only consumer of the baseline until #394
deleted that job; a baseline compared at a different timeout is not a
comparison, so the number the file was written at is pinned in one place and
the regeneration command in the failure message is built from it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kstrl import dampener
from kstrl.cli import SENSE_SCHEMA_VERSION
from kstrl.evolution import _CATEGORY_BY_CHECK
from kstrl.verify import VerifyConfig, resolve_verify_commands

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / dampener.DEFAULT_BASELINE_PATH

#: Every environment variable that can move a value the digest covers. Cleared
#: before the config is loaded: a developer with KSTRL_VERIFY_LINT_CMD exported
#: would otherwise get a red test that says nothing about this repository.
_DIGEST_ENV = (
    "KSTRL_VERIFY_TEST_CMD",
    "KSTRL_VERIFY_TYPECHECK_CMD",
    "KSTRL_VERIFY_LINT_CMD",
    "KSTRL_TIMEOUT_VERIFY",
)


#: The timeout the committed baseline was measured at, and the one the
#: regeneration command below names. One constant, so the two cannot drift.
BASELINE_TIMEOUT_SECONDS = 1800.0


@pytest.fixture
def repository_config(monkeypatch: pytest.MonkeyPatch) -> VerifyConfig:
    for name in _DIGEST_ENV:
        monkeypatch.delenv(name, raising=False)
    assert not any(name in os.environ for name in _DIGEST_ENV)
    return VerifyConfig.load(ROOT)


def test_the_committed_baseline_matches_this_repository(
    repository_config: VerifyConfig,
) -> None:
    """The digest, recomputed from this checkout at the workflow's timeout.

    This is the equality the dampener job refuses on. It is checkable in under
    a second and nothing was checking it.
    """
    baseline = dampener.read_baseline(BASELINE_PATH)
    commands = resolve_verify_commands(repository_config, ROOT)

    assert baseline.verify_digest == dampener.verify_digest(commands, BASELINE_TIMEOUT_SECONDS), (
        "the committed sense baseline was measured with different verify "
        "commands or a different timeout than this checkout resolves, so "
        "`ks sense --compare-baseline` exits 2 for anyone who runs it. "
        f"Regenerate it: KSTRL_TIMEOUT_VERIFY={BASELINE_TIMEOUT_SECONDS:.0f} uv run ks sense "
        "--write-baseline --force"
    )
    assert baseline.sense_schema_version == SENSE_SCHEMA_VERSION


def test_a_different_timeout_gives_a_different_digest(
    repository_config: VerifyConfig,
) -> None:
    """The control for the equality above.

    Without it, a ``verify_digest`` that ignored its inputs and returned a
    constant would pass, and the refusal the committed baseline relies on would
    be gone with nothing failing. The 300 is the default an operator gets by
    running the command without the workflow's env, which is the exact mistake
    the digest exists to catch.
    """
    commands = resolve_verify_commands(repository_config, ROOT)

    assert dampener.verify_digest(commands, 300.0) != dampener.verify_digest(commands, 1800.0)


def test_every_check_the_baseline_names_still_exists() -> None:
    """A renamed check is a hole the digest cannot see.

    The digest covers the three gate COMMANDS and the timeout, so renaming a
    check leaves it matching while every signature under the old name silently
    stops being comparable: the name vanishes from ``measured_checks``, and the
    dampener reports it as a sensor that stopped measuring on every pull
    request. ``_CATEGORY_BY_CHECK`` is the enrolled vocabulary of check names,
    kept honest by ``tests/test_check_name_enrolment.py``.
    """
    baseline = dampener.read_baseline(BASELINE_PATH)
    named = {*baseline.measured_checks, *baseline.unmeasured_checks}

    assert named, "the committed baseline names no checks at all"
    assert named <= set(_CATEGORY_BY_CHECK), sorted(named - set(_CATEGORY_BY_CHECK))


def test_the_baseline_reasons_are_the_ones_its_own_checks_give() -> None:
    """The unmeasured half of the file, checked against itself.

    ``read_baseline`` refuses a document whose ``unmeasured_reasons`` keys do
    not match ``unmeasured_checks``, so this asserts the file it accepted is
    the one committed rather than an empty read: a baseline whose holes lost
    their reasons would send an operator back to the run to find out why.
    """
    baseline = dampener.read_baseline(BASELINE_PATH)

    assert set(baseline.unmeasured_reasons) == set(baseline.unmeasured_checks)
    assert all(reason.strip() for reason in baseline.unmeasured_reasons.values())
    assert not set(baseline.measured_checks) & set(baseline.unmeasured_checks)
