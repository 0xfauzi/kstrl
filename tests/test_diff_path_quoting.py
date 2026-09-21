"""#408: a git-quoted diff header path must be unquoted once, in one place.

git C-quotes a diff header path holding a non-ASCII byte, a double quote,
a backslash or a tab. `git diff --name-status -z`, which every consumer
compares against, never quotes the same path. `adequacy._diff_path` used
to skip the undo, so `coverage_targets` dropped the file for not ending
in `.py` and the adequacy layers measured nothing for it and reported
clean. Every repository below is real git, because what a quoted path
decodes to is not worth guessing at.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kstrl import git, verify
from kstrl.adequacy import AdequacyConfig, coverage_targets, is_test_path
from kstrl.policy import PolicyConfig
from tests.helpers import gitrepo
from tests.helpers.astwalk import assert_census, folds_to, package_sources


def _init_repo(root: Path) -> None:
    """A fresh repository on `main`, with the suite's identity and git's
    default path quoting pinned explicitly."""
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    gitrepo.git_in(root, "config", "core.quotepath", "true")


def _repo_with_awkward_names(root: Path) -> None:
    """A real repository whose branch adds four files, three of whose names
    git C-quotes in a diff header and does not quote in --name-status."""
    _init_repo(root)
    (root / "seed.txt").write_text("x\n", encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "base")
    gitrepo.git_in(root, "checkout", "-q", "-b", "work")
    (root / "café.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / 'we"ird.py').write_text("def h():\n    return 3\n", encoding="utf-8")
    (root / "ascii_sib.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_café.py").write_text(
        "def test_one():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "add awkward names")


def _repo_with_one_module(root: Path, module: str) -> None:
    """A repository whose branch adds ONE non-test module plus a test that
    imports it. `module` is the stem: "café" or "asciimod"."""
    _init_repo(root)
    (root / "seed.txt").write_text("x\n", encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "base")
    gitrepo.git_in(root, "checkout", "-q", "-b", "work")
    (root / f"{module}.py").write_text(
        "def f(n):\n    if n > 0:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_it.py").write_text(
        f"import importlib\nm = importlib.import_module({module!r})\n\n\n"
        "def test_f():\n    assert m.f(1) == 1\n",
        encoding="utf-8",
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "add module and test")


def _repo_with_a_silent_test(root: Path, name: str) -> None:
    """A repository whose BASE commit already holds `tests/<name>` (so the
    file is MODIFIED, not added) and whose branch adds one assertionless
    test to it."""
    _init_repo(root)
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / name).write_text("def test_real():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "base")
    gitrepo.git_in(root, "checkout", "-q", "-b", "work")
    (tests_dir / name).write_text(
        "def test_real():\n    assert 1 + 1 == 2\n\n\ndef test_silent():\n    helper()\n",
        encoding="utf-8",
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "add a silent test")


def test_coverage_targets_selects_every_non_test_python_file(tmp_path: Path) -> None:
    _repo_with_awkward_names(tmp_path)
    diff = git.get_diff_content("main", tmp_path)
    assert coverage_targets(diff) == {
        "ascii_sib.py": {1, 2},
        "café.py": {1, 2},
        'we"ird.py': {1, 2},
    }


def test_the_selected_paths_are_the_paths_name_status_reports(tmp_path: Path) -> None:
    _repo_with_awkward_names(tmp_path)
    names = git.get_diff_names("main", tmp_path)
    expected = {n for n in names if n.endswith(".py") and not is_test_path(n)}
    selected = set(coverage_targets(git.get_diff_content("main", tmp_path)))
    assert selected == expected
    for path in selected:
        # No blanket "no quote character" check here: one of the builder's
        # own files is literally named `we"ird.py`, so its correctly
        # unquoted path CONTAINS a double quote (T1 pins that exact key).
        # A path that is STILL wrapped in git's quoting is instead caught
        # below: it would start and end with `"` and hold a backslash
        # escape, which the next two assertions rule out.
        assert "\\" not in path, path
        assert not path.startswith(("a/", "b/")), path


def _outcome(result: object) -> tuple[str, str | None]:
    """The part of a check_patch_coverage result that must not depend on
    the filename: its type, and its reason when it declined to measure."""
    return type(result).__name__, getattr(result, "reason", None)


def test_check_patch_coverage_treats_an_awkward_name_like_its_ascii_twin(
    tmp_path: Path,
) -> None:
    accent_root = tmp_path / "accent"
    ascii_root = tmp_path / "ascii"
    accent_root.mkdir()
    ascii_root.mkdir()
    _repo_with_one_module(accent_root, "café")
    _repo_with_one_module(ascii_root, "asciimod")

    accent = verify.check_patch_coverage(accent_root, "main", "pytest -q", 120.0)
    plain = verify.check_patch_coverage(ascii_root, "main", "pytest -q", 120.0)

    # The vacuous outcome, named so the failure says what went wrong.
    assert _outcome(accent)[1] != verify.NOT_MEASURED_NO_TARGET, accent
    # The stronger claim: the two repositories differ only in a filename, so
    # the check must reach the same place in both.
    assert _outcome(accent) == _outcome(plain), (accent, plain)


@pytest.mark.parametrize("name", ["test_café.py", "test_ascii.py"])
def test_check_test_adequacy_flags_a_silent_test_in_an_awkward_name(
    tmp_path: Path, name: str
) -> None:
    _repo_with_a_silent_test(tmp_path, name)
    result = verify.check_test_adequacy(tmp_path, "main", AdequacyConfig(enabled=True))
    assert [(f.category, f.location) for f in result.findings] == [
        ("adequacy_no_oracle", f"tests/{name}")
    ]


def test_the_policy_envelope_reports_the_real_path_as_the_secret_location(
    tmp_path: Path,
) -> None:
    """GREEN before this fix and after: pins PR #405's behaviour, which no
    test covers today, at the Phase 1 entry point the issue's acceptance
    asks for."""
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    gitrepo.git_in(tmp_path, "add", "-A")
    gitrepo.git_in(tmp_path, "commit", "-q", "-m", "base")
    gitrepo.git_in(tmp_path, "checkout", "-q", "-b", "work")
    (tmp_path / "sécret.py").write_text(
        'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n', encoding="utf-8"
    )
    gitrepo.git_in(tmp_path, "add", "-A")
    gitrepo.git_in(tmp_path, "commit", "-q", "-m", "add a secret")

    result = verify.check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))
    assert result.passed is False
    assert [(f.category, f.location) for f in result.findings] == [
        ("policy_secret_pattern", "sécret.py")
    ]


#: Every expression in `kstrl/` that folds to a diff file-header prefix,
#: counted per module (#408). Seven in adequacy.py: the `+++ `/`--- ` pair
#: in each of _iter_diff_lines, analyze_test_diff and added_line_numbers,
#: plus the `--- ` fragment of mutation_patch's f-string, which WRITES a
#: header rather than reading one. One in knowledge.py: diff_added_content
#: excludes headers and never reads a path out of one. Two in policy.py:
#: parse_added_lines, the one reader that unquotes.
EXPECTED_DIFF_HEADER_LITERALS = {
    "adequacy.py": 7,
    "knowledge.py": 1,
    "policy.py": 2,
}


def _reads_a_diff_header(node: ast.AST) -> bool:
    return folds_to("+++ ")(node) or folds_to("--- ")(node)


def test_the_diff_header_literals_in_the_package_are_pinned() -> None:
    assert_census(
        sources=package_sources(),
        sees=_reads_a_diff_header,
        expected=EXPECTED_DIFF_HEADER_LITERALS,
        control=('x.startswith("+++ ")', 'x.startswith("--- ")'),
        message=(
            "a diff file-header literal moved. A new reader of a `+++ `/`--- ` "
            "header must take its path through policy.unquote_diff_path (#408); "
            "git quotes that path and `git diff --name-status` does not."
        ),
    )
