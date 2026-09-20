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
"""

from __future__ import annotations

from pathlib import Path

from kstrl.verify import check_bad_patterns
from tests.helpers import gitrepo

#: The ``sk-`` fixture string this suite already carries, reused verbatim.
SK_KEY = "sk-abcdefghijklmnopqrstuvwxyz"
#: Built by concatenation, never as one literal: measured, the assembled
#: spelling is what the gitleaks pre-commit hook refuses to let be committed,
#: and tests/test_encoding_sites.py already writes it this way.
AWS_KEY = "AKIA" + "A" * 16


def _repo(root: Path) -> Path:
    """A real repository on ``main``, with an identity, ready for a base commit."""
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    return root


def _commit(repo: Path, message: str) -> None:
    gitrepo.git_in(repo, "add", "-A")
    gitrepo.git_in(repo, "commit", "-q", "-m", message)


def test_a_secret_the_base_already_carried_does_not_block_an_unrelated_edit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "fixtures.py").write_text(f'SAMPLE = "{SK_KEY}"\nVALUE = 1\n', encoding="utf-8")
    _commit(repo, "base")
    gitrepo.git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "fixtures.py").write_text(f'SAMPLE = "{SK_KEY}"\nVALUE = 2\n', encoding="utf-8")
    _commit(repo, "unrelated edit")

    row = check_bad_patterns(repo, "main")

    assert row.passed is True
    assert row.details == []
    assert row.message == "Scanned 1 of 1 changed Python files, no issues"
    assert row.measured is True


def test_a_secret_the_branch_adds_still_blocks_and_names_the_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(repo, "base")
    gitrepo.git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "app.py").write_text(f'VALUE = 1\nKEY = "{AWS_KEY}"\n', encoding="utf-8")
    _commit(repo, "add a key")

    row = check_bad_patterns(repo, "main")

    assert row.passed is False
    assert row.details == ["app.py: possible secret/credential detected"]
    assert row.measured is True


def test_a_secret_in_a_file_the_branch_creates_blocks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "seed.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(repo, "base")
    gitrepo.git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "leaked.py").write_text(f'TOKEN = "{SK_KEY}"\n', encoding="utf-8")
    _commit(repo, "add leaked file")

    row = check_bad_patterns(repo, "main")

    assert row.passed is False
    assert row.details == ["leaked.py: possible secret/credential detected"]


def test_rewriting_the_line_that_carries_the_secret_blocks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "fixtures.py").write_text(f'SAMPLE = "{SK_KEY}"\n', encoding="utf-8")
    _commit(repo, "base")
    gitrepo.git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "fixtures.py").write_text(f'SAMPLE_KEY = "{SK_KEY}"\n', encoding="utf-8")
    _commit(repo, "rename the variable")

    row = check_bad_patterns(repo, "main")

    assert row.passed is False


def test_deleting_the_secret_line_does_not_block(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "fixtures.py").write_text(f'SAMPLE = "{SK_KEY}"\nVALUE = 1\n', encoding="utf-8")
    _commit(repo, "base")
    gitrepo.git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "fixtures.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(repo, "drop the secret line")

    row = check_bad_patterns(repo, "main")

    assert row.passed is True


def test_only_the_file_that_added_the_secret_is_named(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    _commit(repo, "base")
    gitrepo.git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "app.py").write_text(f'VALUE = 1\nKEY = "{AWS_KEY}"\n', encoding="utf-8")
    (repo / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    _commit(repo, "add a key and touch another file")

    row = check_bad_patterns(repo, "main")

    assert row.details == ["app.py: possible secret/credential detected"]
