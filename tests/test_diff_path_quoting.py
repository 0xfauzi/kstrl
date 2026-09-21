"""#408: a git-quoted diff header path must be unquoted once, in one place.

git C-quotes a diff header path holding a non-ASCII byte, a double quote,
a backslash or a tab. `git diff --name-status -z`, which every consumer
compares against, never quotes the same path. `adequacy._diff_path` used
to skip the undo, so `coverage_targets` dropped the file for not ending
in `.py` and the adequacy layers measured nothing for it and reported
clean. Every repository below is real git, because what a quoted path
decodes to is not worth guessing at.

Coordinator addendum on PR #412 (simplify pass, Groups A to D) folded the
unquote-then-strip logic that used to live twice (`adequacy._diff_path`
and, inline, `policy.parse_added_lines`) into one function,
`policy.diff_header_path`, and replaced the four hand-rolled repository
builders this file used to carry with `tests.conftest.make_review_repo`,
so this file itself now runs no `git commit` and adds no row to
`tests/test_git_identity.py`.
"""

from __future__ import annotations

import ast
import shlex
import sys
from pathlib import Path

import pytest

from kstrl import git, verify
from kstrl.adequacy import AdequacyConfig, coverage_targets, is_test_path
from kstrl.policy import PolicyConfig, diff_header_path
from tests.conftest import make_review_repo
from tests.helpers import gitrepo
from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    folds_to,
    package_sources,
    parse,
    spells,
)

#: The one test command every check in this file spawns. `"pytest -q"`
#: resolves through the shell's PATH, which is not necessarily this
#: venv's interpreter; `sys.executable -m pytest` is the exact
#: interpreter running the suite, so a coverage run inside the temp
#: repository finds the same `pytest-cov` this venv has (#408 addendum
#: Group C3). Spelling matches `tests/helpers/adequacy_fixture.py`'s
#: `run_adequacy` default.
_TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest"


def test_coverage_targets_selects_every_non_test_python_file(tmp_path: Path) -> None:
    """T1. Also T2's one surviving assertion, folded in here (#408 addendum
    Group C1): the per-path loop T2 used to run afterward could not fail
    under any plant, because it ran only once `selected == expected` had
    already passed and `expected` is derived from `get_diff_names`, so a
    plant that broke the loop's own conditions would already have failed
    the equality above it. What is left is the set-equality check against
    `git.get_diff_names`, which pins the same claim without dead code.
    """
    repo = make_review_repo(
        tmp_path,
        base_files={"seed.txt": "x\n"},
        files={
            "café.py": "def f():\n    return 1\n",
            'we"ird.py': "def h():\n    return 3\n",
            "ascii_sib.py": "def g():\n    return 2\n",
            "tests/test_café.py": "def test_one():\n    assert 1 + 1 == 2\n",
        },
    )
    gitrepo.git_in(repo.path, "config", "core.quotepath", "true")
    diff = git.get_diff_content(repo.base_branch, repo.path)
    result = coverage_targets(diff)
    assert result == {
        "ascii_sib.py": {1, 2},
        "café.py": {1, 2},
        'we"ird.py': {1, 2},
    }
    names = git.get_diff_names(repo.base_branch, repo.path)
    expected = {n for n in names if n.endswith(".py") and not is_test_path(n)}
    assert set(result) == expected


def _outcome(result: object) -> tuple[str, str | None]:
    """The part of a check_patch_coverage result that must not depend on
    the filename: its type, and its reason when it declined to measure."""
    return type(result).__name__, getattr(result, "reason", None)


def test_check_patch_coverage_treats_an_awkward_name_like_its_ascii_twin(
    tmp_path: Path,
) -> None:
    """T3. Drives the real entry point. Measured on this machine after the
    fix, with `_TEST_COMMAND` (#408 addendum Group C3): both twins now
    reach a genuine `PatchCoverage(covered=3, total=4, ...)`, not the
    `NotMeasured(tool_missing)` the plan's original "pytest -q" spelling
    produced for both. That exact value is not asserted here as a third
    named outcome, because it is a property of what this venv's spawned
    coverage run finds, not of the fix: the claim this test enforces is
    that the two repositories, differing only in a filename, land on the
    SAME (type, reason), not which one."""
    accent = make_review_repo(
        tmp_path / "accent",
        base_files={"seed.txt": "x\n"},
        files={
            "café.py": "def f(n):\n    if n > 0:\n        return 1\n    return 0\n",
            "tests/test_it.py": (
                "import importlib\n"
                "m = importlib.import_module('café')\n\n\n"
                "def test_f():\n    assert m.f(1) == 1\n"
            ),
        },
    )
    plain = make_review_repo(
        tmp_path / "ascii",
        base_files={"seed.txt": "x\n"},
        files={
            "asciimod.py": "def f(n):\n    if n > 0:\n        return 1\n    return 0\n",
            "tests/test_it.py": (
                "import importlib\n"
                "m = importlib.import_module('asciimod')\n\n\n"
                "def test_f():\n    assert m.f(1) == 1\n"
            ),
        },
    )
    gitrepo.git_in(accent.path, "config", "core.quotepath", "true")
    gitrepo.git_in(plain.path, "config", "core.quotepath", "true")

    accent_result = verify.check_patch_coverage(
        accent.path, accent.base_branch, _TEST_COMMAND, 120.0
    )
    plain_result = verify.check_patch_coverage(plain.path, plain.base_branch, _TEST_COMMAND, 120.0)

    # The vacuous outcome, named so the failure says what went wrong
    # (#408 addendum Group C5: the bare reason, not the full outcome
    # tuple, is all this negation needs).
    assert getattr(accent_result, "reason", None) != verify.NOT_MEASURED_NO_TARGET, accent_result
    # The stronger claim: the two repositories differ only in a filename,
    # so the check must reach the same place in both.
    assert _outcome(accent_result) == _outcome(plain_result), (accent_result, plain_result)


@pytest.mark.parametrize("name", ["test_café.py", "test_ascii.py"])
def test_check_test_adequacy_flags_a_silent_test_in_an_awkward_name(
    tmp_path: Path, name: str
) -> None:
    """T4. The ASCII case is in the test on purpose, as the twin that
    shows what the non-ASCII case should have done."""
    repo = make_review_repo(
        tmp_path,
        base_files={f"tests/{name}": "def test_real():\n    assert 1 + 1 == 2\n"},
        files={
            f"tests/{name}": (
                "def test_real():\n    assert 1 + 1 == 2\n\n\ndef test_silent():\n    helper()\n"
            )
        },
    )
    gitrepo.git_in(repo.path, "config", "core.quotepath", "true")
    result = verify.check_test_adequacy(repo.path, repo.base_branch, AdequacyConfig(enabled=True))
    assert [(f.category, f.location) for f in result.findings] == [
        ("adequacy_no_oracle", f"tests/{name}")
    ]


def test_the_policy_envelope_reports_the_real_path_as_the_secret_location(
    tmp_path: Path,
) -> None:
    """T5. GREEN before this fix and after. `tests/test_policy_envelope.py`
    already pins the unquote itself (its own lines 123-152, 154-186,
    188-218), so what this test alone covers is the LOCATION passthrough
    `_scan_secrets` -> `PolicyViolation.location` -> `Finding.location`
    through the real Phase 1 entry point, `verify.check_policy_envelope`,
    rather than `evaluate_policy` called with pre-fetched artifacts
    (#408 addendum Group C4)."""
    repo = make_review_repo(
        tmp_path,
        base_files={"seed.txt": "x\n"},
        files={"sécret.py": 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n'},
    )
    gitrepo.git_in(repo.path, "config", "core.quotepath", "true")
    result = verify.check_policy_envelope(repo.path, repo.base_branch, PolicyConfig(enabled=True))
    assert result.passed is False
    assert [(f.category, f.location) for f in result.findings] == [
        ("policy_secret_pattern", "sécret.py")
    ]


def _first_header(diff_text: str, marker: str) -> str:
    """The first `marker` (`+++ ` or `--- `) line in a real diff. Each
    case below changes exactly one file, so there is exactly one such
    line and no ambiguity about which file it belongs to."""
    for line in diff_text.splitlines():
        if line.startswith(marker):
            return line
    raise AssertionError(f"no {marker!r} header in diff:\n{diff_text}")


#: One repository per header shape (#408 addendum Group A3). `expected`
#: is `None` for the `/dev/null` case: that spelling is git's own fixed
#: token, never a path this repository derived, so there is nothing to
#: read out of `get_diff_names` for it.
_HEADER_SHAPES: list[tuple[str, dict[str, str] | None, dict[str, str], str, bool]] = [
    ("plain_ascii", None, {"plain.py": "x = 1\n"}, "+++ ", False),
    ("non_ascii_quoted", None, {"café.py": "x = 1\n"}, "+++ ", False),
    ("double_quote", None, {'we"ird.py': "x = 1\n"}, "+++ ", False),
    ("backslash", None, {"sl\\ash.py": "x = 1\n"}, "+++ ", False),
    ("directory_named_b", None, {"b/x.py": "x = 1\n"}, "+++ ", False),
    ("dev_null_source", None, {"plain.py": "x = 1\n"}, "--- ", True),
    (
        "a_prefixed_source",
        {"modified.py": "old = 1\n"},
        {"modified.py": "new = 1\n"},
        "--- ",
        False,
    ),
]


@pytest.mark.parametrize(
    "base_files,files,marker,is_dev_null",
    [shape[1:] for shape in _HEADER_SHAPES],
    ids=[shape[0] for shape in _HEADER_SHAPES],
)
def test_diff_header_path_matches_a_real_diff_for_every_header_shape(
    tmp_path: Path,
    base_files: dict[str, str] | None,
    files: dict[str, str],
    marker: str,
    is_dev_null: bool,
) -> None:
    """A1/A3: one parametrised, always-run test of the shared function,
    each expectation taken from a real `git diff` rather than typed."""
    repo = make_review_repo(tmp_path, base_files=base_files or {"seed.txt": "x\n"}, files=files)
    gitrepo.git_in(repo.path, "config", "core.quotepath", "true")
    diff_text = git.get_diff_content(repo.base_branch, repo.path)
    header = _first_header(diff_text, marker)
    if is_dev_null:
        assert diff_header_path(header) == "/dev/null"
        return
    names = git.get_diff_names(repo.base_branch, repo.path)
    assert len(names) == 1, names
    assert diff_header_path(header) == names[0]


#: Every expression in `kstrl/` that folds to one of six diff file-header
#: tokens, counted per module (#408 addendum Group B1). Widened from the
#: two spellings with a trailing space to the full set a hand-rolled
#: reader plausibly writes: with and without the trailing space, and
#: with the `a/`/`b/` prefix baked into the same literal. Re-derived by
#: RUNNING the walk after Group A's edit (never typed): adequacy.py's
#: nine are the `+++ `/`--- ` gating pair in each of _iter_diff_lines,
#: analyze_test_diff and added_line_numbers (six), the `--- ` fragment
#: of mutation_patch's f-string, which WRITES a header (one), and the
#: bare `---`/`+++` content-line guards inside analyze_test_diff that
#: tell a removed/added assertion line from the header itself (two).
#: knowledge.py's two are diff_added_content's own `+++ ` exclusion (it
#: still never reads a path out of a header) and an unrelated bare
#: `---` used as a markdown frontmatter delimiter, not a diff reader.
#: policy.py's two are parse_added_lines' `+++ `/`--- ` gating pair, the
#: one reader that calls diff_header_path. pr.py's two are both bare
#: `---` written as a markdown horizontal rule in a PR body, not read
#: from a diff at all; lane #409's this round, unedited here.
EXPECTED_DIFF_HEADER_LITERALS = {
    "adequacy.py": 9,
    "knowledge.py": 2,
    "policy.py": 2,
    "pr.py": 2,
}


def _reads_a_diff_header(node: ast.AST) -> bool:
    return (
        folds_to("+++ ")(node)
        or folds_to("--- ")(node)
        or folds_to("+++")(node)
        or folds_to("---")(node)
        or folds_to("+++ b/")(node)
        or folds_to("--- a/")(node)
    )


def test_the_diff_header_literals_in_the_package_are_pinned() -> None:
    assert_census(
        sources=package_sources(),
        sees=_reads_a_diff_header,
        expected=EXPECTED_DIFF_HEADER_LITERALS,
        control=(
            'x.startswith("+++ ")',
            'x.startswith("--- ")',
            'x.startswith("+++")',
            'x.startswith("---")',
            'x.startswith("+++ b/")',
            'x.startswith("--- a/")',
        ),
        message=(
            "a diff file-header literal moved. A new reader of a diff header "
            "must take its path through policy.diff_header_path (#408); git "
            "quotes that path and `git diff --name-status` does not."
        ),
    )


#: Every place in `kstrl/` that writes the name `diff_header_path`
#: (#408). Counts the definition and every call, so a caller that stops
#: delegating and inlines the unquote-then-strip logic again shows up
#: here as a census delta even though it adds no new header literal.
#: Re-derived by running the walk: adequacy.py's three are the import
#: and the two calls in _iter_diff_lines; policy.py's two are the def
#: and the one call in parse_added_lines.
EXPECTED_DIFF_HEADER_PATH_CALLERS = {"adequacy.py": 3, "policy.py": 2}


def test_every_header_path_reader_still_delegates() -> None:
    assert_census(
        sources=package_sources(),
        sees=spells("diff_header_path"),
        expected=EXPECTED_DIFF_HEADER_PATH_CALLERS,
        control="diff_header_path(line)",
        message=(
            "a caller stopped delegating to policy.diff_header_path, or a "
            "new one appeared (#408). Unquote-then-strip lives in exactly "
            "one function; an inlined copy adds no header literal, so the "
            "literal census above cannot see it."
        ),
    )


def _reads_a_diff_header_in_source(source: str) -> bool:
    """`_reads_a_diff_header` applied to every node of a parsed source
    string, for :func:`~tests.helpers.astwalk.blind_spot`'s probe shape."""
    return any(_reads_a_diff_header(node) for node in all_nodes(parse(source)))


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_regex_diff_header_matcher_is_a_known_miss() -> None:
    """The disclosed residual (#408 addendum Group B2): the census folds
    an expression's VALUE and compares it for EQUALITY, so a regex
    spelling such as `r"\\+\\+\\+ b/(.*)"` is invisible to it - a raw
    string does not process its own backslash escapes, so the value this
    folds to is the literal characters `\\+\\+\\+ b/(.*)`, which equals
    none of the six tokens the census compares against. Under
    `strict=True` so that teaching the census to fold a regex XPASSes
    here and forces this disclosure to be edited in the same diff.
    """
    blind_spot(
        _reads_a_diff_header_in_source,
        'import re\n_PATTERN = re.compile(r"\\+\\+\\+ b/(.*)")\n',
    )
