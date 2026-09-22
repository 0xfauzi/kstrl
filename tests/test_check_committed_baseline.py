"""This repository's OWN committed baseline, read the way `ks check --compare-baseline` reads it.

Every other test about the baseline builds a baseline under ``tmp_path``. None
of them touches ``scripts/kstrl/baseline.json``, which is this
repository's own committed baseline and the worked example
``docs/baseline.md`` points at. Round 2 of review on #357 measured the gap -
`grep -rn sense-baseline tests/` found only fixtures and a string constant.

What that costs. The baseline records a digest of the three verify commands and
the timeout, and ``ks check --compare-baseline`` refuses a mismatch with exit 2.
So a `[verify]` command change in ``kstrl.toml``, a bump to
``BASELINE_SCHEMA_VERSION``, or moving ``[tool.mypy] files`` out of
``pyproject.toml`` (which flips ``_default_typecheck_command``) invalidates the
committed file with a green local suite, and the first signal is
`ks check --compare-baseline` exiting 2 for anyone who runs it next. Here it
is a red test on the commit that did it.

The timeout is a constant here. It used to be read out of the baseline
workflow, which was this repository's only consumer of the baseline until #394
deleted that job; a baseline compared at a different timeout is not a
comparison, so the number the file was written at is pinned in one place and
the regeneration command in the failure message is built from it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kstrl import baseline
from kstrl.cli import CHECK_SCHEMA_VERSION
from kstrl.evolution import _CATEGORY_BY_CHECK
from kstrl.verify import VerifyConfig, resolve_verify_commands

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / baseline.DEFAULT_BASELINE_PATH

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
    """The digest, recomputed from this checkout at the pinned timeout.

    This is the equality `ks check --compare-baseline` exits 2 on. It is
    checkable in under a second and nothing was checking it.
    """
    committed = baseline.read_baseline(BASELINE_PATH)
    commands = resolve_verify_commands(repository_config, ROOT)

    assert committed.verify_digest == baseline.verify_digest(commands, BASELINE_TIMEOUT_SECONDS), (
        "the committed baseline was measured with different verify "
        "commands or a different timeout than this checkout resolves, so "
        "`ks check --compare-baseline` exits 2 for anyone who runs it. "
        f"Regenerate it: KSTRL_TIMEOUT_VERIFY={BASELINE_TIMEOUT_SECONDS:.0f} uv run ks check "
        "--write-baseline --force"
    )
    assert committed.check_schema_version == CHECK_SCHEMA_VERSION


def test_a_different_timeout_gives_a_different_digest(
    repository_config: VerifyConfig,
) -> None:
    """The control for the equality above.

    Without it, a ``verify_digest`` that ignored its inputs and returned a
    constant would pass, and the refusal the committed baseline relies on would
    be gone with nothing failing. The 300 is the default an operator gets by
    running the command without setting ``KSTRL_TIMEOUT_VERIFY``, which is the
    exact mistake the digest exists to catch.
    """
    commands = resolve_verify_commands(repository_config, ROOT)

    assert baseline.verify_digest(commands, 300.0) != baseline.verify_digest(
        commands, BASELINE_TIMEOUT_SECONDS
    )


def test_every_check_the_baseline_names_still_exists() -> None:
    """A renamed check is a hole the digest cannot see.

    The digest covers the three gate COMMANDS and the timeout, so renaming a
    check leaves it matching while every signature under the old name silently
    stops being comparable: the name vanishes from ``measured_checks``, and the
    baseline reports it as a check that stopped measuring on every pull
    request. ``_CATEGORY_BY_CHECK`` is the enrolled vocabulary of check names,
    kept honest by ``tests/test_check_name_enrolment.py``.
    """
    committed = baseline.read_baseline(BASELINE_PATH)
    named = {*committed.measured_checks, *committed.unmeasured_checks}

    assert named, "the committed baseline names no checks at all"
    assert named <= set(_CATEGORY_BY_CHECK), sorted(named - set(_CATEGORY_BY_CHECK))


def test_the_baseline_reasons_are_the_ones_its_own_checks_give() -> None:
    """The unmeasured half of the file, checked against itself.

    ``read_baseline`` refuses a document whose ``unmeasured_reasons`` keys do
    not match ``unmeasured_checks``, so this asserts the file it accepted is
    the one committed rather than an empty read: a baseline whose holes lost
    their reasons would send an operator back to the run to find out why.
    """
    committed = baseline.read_baseline(BASELINE_PATH)

    assert set(committed.unmeasured_reasons) == set(committed.unmeasured_checks)
    assert all(reason.strip() for reason in committed.unmeasured_reasons.values())
    assert not set(committed.measured_checks) & set(committed.unmeasured_checks)
