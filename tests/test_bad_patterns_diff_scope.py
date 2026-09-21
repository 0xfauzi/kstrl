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
from tests.conftest import ReviewRepo, make_review_repo
from tests.helpers import gitrepo

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


# ---------------------------------------------------------------------------
# #414: a finding the BASE already carried is not the branch's.
#
# The secret rule reads added lines (#399/#405). The empty-file and
# syntax-error rules read the whole file, so a branch that moves or edits a
# file it did not write inherits every finding already in it. Measured on
# main at 6a354cc: a pure rename of a file that does not parse (zero added
# lines) failed this blocking gate.
#
# Real repositories and real git, for the reason this module's own docstring
# gives: what is under test is which CONTENT the check attributes to the
# branch, and a stubbed base would be the test deciding that.
# ---------------------------------------------------------------------------

#: A file that does not compile. One spelling, so a test that plants it and a
#: test that asserts it was NOT attributed cannot drift.
BROKEN = "def f(:\n    pass\n"


def _commit_rename(repo: ReviewRepo, source: str, destination: str) -> None:
    """``git mv`` on the feature branch, as a second commit.

    ``make_review_repo`` writes files and cannot delete one, so a rename is
    a commit of its own on top of the branch it already built. The diff the
    check reads is ``main...HEAD``, which spans both commits, and git's
    rename detection (``-M -C``, which ``git.get_diff_name_status`` passes)
    reports one ``R`` record over the pair.
    """
    (repo.path / destination).parent.mkdir(parents=True, exist_ok=True)
    gitrepo.git_in(repo.path, "mv", source, destination)
    gitrepo.git_in(repo.path, "commit", "-qm", f"rename {source} to {destination}")


def test_a_pure_rename_of_a_file_that_does_not_parse_is_not_this_branchs_finding(
    tmp_path: Path,
) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"broken.py": BROKEN, "keep.py": "x = 1\n"},
        files={"keep.py": "x = 2\n"},
    )
    _commit_rename(repo, "broken.py", "moved.py")

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is True
    assert row.details == [
        "moved.py: syntax error was already there at main:broken.py; not this branch's change"
    ]
    assert "1 already at the base" in row.message


def test_a_rename_of_an_already_empty_file_is_not_this_branchs_finding(
    tmp_path: Path,
) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"pkg/__init__.py": "", "pkg/m.py": "y = 2\n"},
        files={"pkg/m.py": "y = 3\n"},
    )
    _commit_rename(repo, "pkg/__init__.py", "pkg2/__init__.py")

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is True
    assert row.details == [
        "pkg2/__init__.py: empty file was already there at main:pkg/__init__.py; "
        "not this branch's change"
    ]


def test_editing_a_file_that_already_did_not_parse_is_not_this_branchs_finding(
    tmp_path: Path,
) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"legacy.py": BROKEN},
        files={"legacy.py": f"{BROKEN}# an unrelated comment\n"},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is True
    assert row.details == [
        "legacy.py: syntax error was already there at main:legacy.py; not this branch's change"
    ]


def test_two_renamed_broken_files_are_each_read_against_their_own_source(
    tmp_path: Path,
) -> None:
    """The pairing, which a single rename cannot measure.

    ``git.get_diff_name_status`` flattens a rename into two records under one
    status token, source then destination. Two renames in one branch is the
    case where pairing them in the wrong order silently reads the wrong base
    path, and both files here fail the same rule, so only the PATH in the row
    tells the two apart.
    """
    repo = make_review_repo(
        tmp_path,
        base_files={"one.py": BROKEN, "two.py": f"# two\n{BROKEN}", "keep.py": "x = 1\n"},
        files={"keep.py": "x = 2\n"},
    )
    _commit_rename(repo, "one.py", "moved_one.py")
    _commit_rename(repo, "two.py", "moved_two.py")

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is True
    assert sorted(row.details) == [
        "moved_one.py: syntax error was already there at main:one.py; not this branch's change",
        "moved_two.py: syntax error was already there at main:two.py; not this branch's change",
    ]


def test_a_syntax_error_the_branch_writes_still_blocks(tmp_path: Path) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"app.py": "def f():\n    return 1\n"},
        files={"app.py": "def f(:\n    return 1\n"},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert len(row.details) == 1
    assert row.details[0].startswith("app.py: syntax error - ")


def test_emptying_a_file_the_base_had_content_in_still_blocks(tmp_path: Path) -> None:
    repo = make_review_repo(
        tmp_path,
        base_files={"app.py": "VALUE = 1\n"},
        files={"app.py": ""},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.details == ["app.py: empty file"]


def test_an_empty_file_the_branch_adds_still_blocks(tmp_path: Path) -> None:
    """The case that refutes "no added lines means not the branch's": git
    writes no content line for a new EMPTY file, so the diff this branch
    adds is a `new file mode` header and nothing else."""
    repo = make_review_repo(
        tmp_path,
        base_files={"seed.py": "VALUE = 1\n"},
        files={"seed.py": "VALUE = 1\n", "brand_new.py": ""},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.details == ["brand_new.py: empty file"]


def test_a_rename_that_also_breaks_the_file_still_blocks(tmp_path: Path) -> None:
    """The issue's own "do not skip renamed files wholesale": the source
    compiled at the base, so the destination not compiling is this branch's
    doing however it got there."""
    body = "def f():\n    return 1\n" + "# pad\n" * 20
    repo = make_review_repo(
        tmp_path,
        base_files={"app.py": body, "keep.py": "x = 1\n"},
        files={"keep.py": "x = 2\n"},
    )
    _commit_rename(repo, "app.py", "moved.py")
    (repo.path / "moved.py").write_text(
        "def f(:\n    return 1\n" + "# pad\n" * 20, encoding="utf-8"
    )
    gitrepo.git_in(repo.path, "add", "-A")
    gitrepo.git_in(repo.path, "commit", "-qm", "break the renamed file")

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert len(row.details) == 1
    assert row.details[0].startswith("moved.py: syntax error - ")


def test_a_blocking_finding_and_a_preexisting_one_are_both_reported(
    tmp_path: Path,
) -> None:
    """Both rows in one result: the gate still fails for what the branch
    wrote, and the operator can still see what was not counted and why."""
    repo = make_review_repo(
        tmp_path,
        base_files={"legacy.py": BROKEN, "app.py": "def f():\n    return 1\n"},
        files={"legacy.py": f"{BROKEN}# a comment\n", "app.py": "def f(:\n    return 1\n"},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.message == "1 issues found in changed files"
    assert [d for d in row.details if d.startswith("app.py")] != []
    assert (
        "legacy.py: syntax error was already there at main:legacy.py; not this branch's change"
        in row.details
    )


def test_mechanical_verification_does_not_fail_on_a_preexisting_syntax_error(
    tmp_path: Path,
) -> None:
    """The altitude the issue complains about: a blocking Phase 1 row whose
    detail goes into the engineer's retry context. Driven through the real
    entry point, not through ``check_bad_patterns``."""
    repo = make_review_repo(
        tmp_path,
        base_files={"legacy.py": BROKEN},
        files={"legacy.py": f"{BROKEN}# an unrelated comment\n"},
    )
    config = VerifyConfig(
        test_command="true",
        typecheck_command="true",
        lint_command="true",
        subprocess_timeout=30.0,
    )

    result = run_mechanical_verification(
        repo.path,
        prd_path=None,
        base_branch=repo.base_branch,
        allowed_paths=None,
        config=config,
    )

    rows = [c for c in result.checks if c.name == "bad_patterns"]
    assert len(rows) == 1
    assert rows[0].passed is True
    assert rows[0].details == [
        "legacy.py: syntax error was already there at main:legacy.py; not this branch's change"
    ]


def test_a_syntax_error_the_branch_writes_into_an_empty_file_still_blocks(
    tmp_path: Path,
) -> None:
    """One of the two `== kind` halves (#414 critic finding 1): the base
    carried EMPTY FILE at this path, and the branch turned it into a SYNTAX
    ERROR. That is a DIFFERENT kind of finding, so it is this branch's own
    and must block, even though `_base_finding` returns non-None for this
    path. A ``is not None`` implementation wrongly clears this."""
    repo = make_review_repo(
        tmp_path,
        base_files={
            "keep.py": "x = 1\n",
            "pkg/__init__.py": "",
            "pkg/m.py": "y = 2\n",
        },
        files={"keep.py": "x = 2\n", "pkg/__init__.py": BROKEN},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.message == "1 issues found in changed files"
    assert len(row.details) == 1
    assert row.details[0].startswith("pkg/__init__.py: syntax error - ")


def test_emptying_a_file_that_did_not_parse_at_the_base_still_blocks(
    tmp_path: Path,
) -> None:
    """The other `== kind` half: the base carried a SYNTAX ERROR at this
    path, and the branch emptied it. That is a DIFFERENT kind of finding,
    so it is this branch's own and must block."""
    repo = make_review_repo(
        tmp_path,
        base_files={"keep.py": "x = 1\n", "legacy.py": BROKEN},
        files={"keep.py": "x = 2\n", "legacy.py": ""},
    )

    row = check_bad_patterns(repo.path, repo.base_branch)

    assert row.passed is False
    assert row.details == ["legacy.py: empty file"]
