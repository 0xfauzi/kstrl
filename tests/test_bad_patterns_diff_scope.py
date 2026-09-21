"""The Phase 1 secret scan reads the lines the branch ADDED (#399).

``check_bad_patterns`` used to search whole file CONTENTS, so any change that
touched a file inherited every secret-shaped string already in it. Measured on
the tree at the time: a branch that edited ``tests/test_verify.py`` for any
reason failed a blocking gate on the ``sk-`` fixture that file has carried for
months, and the failure text went into the engineer's retry prompt every
iteration.

Real repositories and real ``git``, because what is under test is which LINES
the check reads out of a diff, and a stubbed diff would be the test deciding
that.

#399 simplify pass on #405 (C1): built on ``tests.conftest.make_review_repo``
rather than a local ``_repo``/``_commit`` pair. That helper already builds a
base-then-branch repository under a real identity and is imported by eight
other modules; every commit these tests need happens inside it, so this file
adds no new row to ``tests/test_git_identity.py``'s per-file census.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from kstrl import policy
from kstrl.verify import VerifyConfig, check_bad_patterns, run_mechanical_verification
from tests.conftest import make_review_repo

#: The ``sk-`` fixture string this suite already carries, reused verbatim.
SK_KEY = "sk-abcdefghijklmnopqrstuvwxyz"
#: Built by concatenation, never as one literal: measured, the assembled
#: spelling is what the gitleaks pre-commit hook refuses to let be committed,
#: and tests/test_encoding_sites.py already writes it this way.
AWS_KEY = "AKIA" + "A" * 16


def test_a_secret_the_base_already_carried_does_not_block_an_unrelated_edit(
    tmp_path: Path,
) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"fixtures.py": f'SAMPLE = "{SK_KEY}"\nVALUE = 1\n'},
        files={"fixtures.py": f'SAMPLE = "{SK_KEY}"\nVALUE = 2\n'},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is True
    assert row.details == []
    assert row.message == "Scanned 1 of 1 changed Python files, no issues"
    assert row.measured is True


def test_a_secret_the_branch_adds_still_blocks_and_names_the_file(tmp_path: Path) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"app.py": "VALUE = 1\n"},
        files={"app.py": f'VALUE = 1\nKEY = "{AWS_KEY}"\n'},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.details == ["app.py: possible secret/credential detected"]
    assert row.measured is True


def test_a_secret_in_a_file_the_branch_creates_blocks(tmp_path: Path) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"seed.py": "VALUE = 1\n"},
        files={"leaked.py": f'TOKEN = "{SK_KEY}"\n'},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.details == ["leaked.py: possible secret/credential detected"]


def test_rewriting_the_line_that_carries_the_secret_blocks(tmp_path: Path) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"fixtures.py": f'SAMPLE = "{SK_KEY}"\n'},
        files={"fixtures.py": f'SAMPLE_KEY = "{SK_KEY}"\n'},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False


def test_deleting_the_secret_line_does_not_block(tmp_path: Path) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"fixtures.py": f'SAMPLE = "{SK_KEY}"\nVALUE = 1\n'},
        files={"fixtures.py": "VALUE = 1\n"},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is True


def test_only_the_file_that_added_the_secret_is_named(tmp_path: Path) -> None:
    """The test that proves the workaround #405 shipped (an added-line TEXT
    set with no per-file attribution) is actually gone: A2 of the #399
    simplify pass keys the scan back on PATH, so a branch that adds a secret
    in one file and touches an unrelated line in another names only the
    file that added it."""
    repo = make_review_repo(
        tmp_path,
        base_files={"app.py": "VALUE = 1\n", "other.py": "OTHER = 1\n"},
        files={
            "app.py": f'VALUE = 1\nKEY = "{AWS_KEY}"\n',
            "other.py": "OTHER = 2\n",
        },
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.details == ["app.py: possible secret/credential detected"]


def test_a_secret_two_files_already_share_does_not_leak_onto_the_one_that_did_not_add_it(
    tmp_path: Path,
) -> None:
    """The scenario A2 of the #399 simplify pass on #405 was measured
    against, not a hypothetical: a branch adds a secret in a NEW file while
    making an UNRELATED edit to a file that already carried that identical
    secret LINE (this repository's own test suite hardcodes the ``sk-``
    fixture in more than one file). Keying the scan on added-line TEXT
    rather than on the file that ADDED the line - the workaround #405
    shipped - would report both files, because the carrier's untouched
    secret line happens to equal a line the other file's branch commit
    really did add. Keying on PATH again (A2) is the fix: the carrier's
    secret line was never one of ITS OWN added lines, so it is not named.
    """
    secret_line = f'API_KEY = "{SK_KEY}"'
    repo = make_review_repo(
        tmp_path,
        base_files={"carrier.py": f"{secret_line}\nVALUE = 1\n"},
        files={
            "carrier.py": f"{secret_line}\nVALUE = 2\n",
            "leaked.py": f"{secret_line}\n",
        },
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.details == ["leaked.py: possible secret/credential detected"]


def test_the_default_secret_patterns_are_the_envelopes_own_list() -> None:
    """A3 of the #399 simplify pass on #405: ``verify.SECRET_PATTERNS`` (a
    second, independently-maintained copy of the same five regexes) is
    gone. ``check_bad_patterns``'s own default falls back to
    ``policy.DEFAULT_SECRET_PATTERNS`` by IDENTITY, not by a re-typed
    literal, so a pattern added to that one list is the pattern the
    default Phase 1 gate sees too - one rule, not two. Measured directly
    (not just by this identity check): editing ``DEFAULT_SECRET_PATTERNS``
    to add a sixth pattern and re-running ``check_bad_patterns`` against a
    branch that adds a matching string blocks it, with no edit anywhere in
    ``kstrl/verify.py``.
    """
    default = inspect.signature(check_bad_patterns).parameters["secret_patterns"].default
    assert default is policy.DEFAULT_SECRET_PATTERNS


def test_run_mechanical_verification_passes_the_envelopes_configured_patterns_to_bad_patterns(
    tmp_path: Path,
) -> None:
    """#399 blocker 2: the previous test only pins the DEFAULT by identity,
    which is a mechanism-free claim about the CONFIGURED case - a plant
    that hardcodes ``bad_patterns_secret_patterns = DEFAULT_SECRET_PATTERNS``
    at the ``run_mechanical_verification`` call site satisfies it while
    silently dropping a configured ``[policy] secret_patterns`` on the
    floor. Driven end to end through ``run_mechanical_verification``, not
    ``check_bad_patterns`` directly, because the call site is what the
    plant targets.

    ``policy_config.enabled=False`` is load-bearing: the envelope gate
    itself never runs (only ``[policy] secret_patterns`` is read, which
    ``PolicyConfig.load`` does unconditionally), so a pass here proves the
    patterns reach ``check_bad_patterns`` separately from the envelope's
    own on/off switch.
    """
    repo = make_review_repo(
        tmp_path,
        base_files={"app.py": "VALUE = 1\n"},
        files={"app.py": 'VALUE = 1\nTOKEN = "zzplant-123456"\n'},
    )
    config = VerifyConfig(
        test_command="true",
        typecheck_command="true",
        lint_command="true",
        subprocess_timeout=30.0,
    )
    policy_config = policy.PolicyConfig(enabled=False, secret_patterns=["zzplant-[0-9]{6}"])

    result = run_mechanical_verification(
        repo.path,
        prd_path=None,
        base_branch=repo.base_branch,
        allowed_paths=None,
        config=config,
        policy_config=policy_config,
    )

    bad_patterns_rows = [c for c in result.checks if c.name == "bad_patterns"]
    assert len(bad_patterns_rows) == 1
    row = bad_patterns_rows[0]
    assert row.passed is False
    assert row.details == ["app.py: possible secret/credential detected"]
