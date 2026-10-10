"""#408: a git-quoted diff header path must be unquoted once, in one place.

git C-quotes a diff header path holding a non-ASCII byte, a double quote,
a backslash or a tab. `git diff --name-status -z`, which every consumer
compares against, never quotes the same path. A reader that skipped the
undo once dropped a file for not ending in `.py` and reported clean.
Every repository below is real git, because what a quoted path decodes
to is not worth guessing at.

Coordinator addendum on PR #412 (simplify pass, Groups A to D) folded the
unquote-then-strip logic that used to live twice into one function,
`policy.diff_header_path`, and replaced the four hand-rolled repository
builders this file used to carry with `tests.conftest.make_review_repo`,
so this file itself now runs no `git commit` and adds no row to
`tests/test_git_identity.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kstrl import git, verify_diff
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


def test_the_policy_envelope_reports_the_real_path_as_the_secret_location(
    tmp_path: Path,
) -> None:
    """T5. GREEN before this fix and after. `tests/test_policy_envelope.py`
    already pins the unquote itself (its own lines 123-152, 154-186,
    188-218), so what this test alone covers is the LOCATION passthrough
    `_scan_secrets` -> `PolicyViolation.location` -> `Finding.location`
    through the real Phase 1 entry point, `verify_diff.check_policy_envelope`,
    rather than `evaluate_policy` called with pre-fetched artifacts
    (#408 addendum Group C4)."""
    repo = make_review_repo(
        tmp_path,
        base_files={"seed.txt": "x\n"},
        files={"sécret.py": 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n'},
    )
    gitrepo.git_in(repo.path, "config", "core.quotepath", "true")
    result = verify_diff.check_policy_envelope(
        repo.path, repo.base_branch, PolicyConfig(enabled=True)
    )
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
#: RUNNING the walk (never typed); #696 slice 8 removed adequacy.py's
#: nine with the diff readers that held them.
#: knowledge.py's two are diff_added_content's own `+++ ` exclusion (it
#: still never reads a path out of a header) and an unrelated bare
#: `---` used as a markdown frontmatter delimiter, not a diff reader.
#: policy.py's two are parse_added_lines' `+++ `/`--- ` gating pair, the
#: one reader that calls diff_header_path. pr_body.py's two are both bare
#: `---` written as a markdown horizontal rule in a PR body, not read
#: from a diff at all; #639 slice 5 moved them out of pr.py with the two
#: PR body writers.
EXPECTED_DIFF_HEADER_LITERALS = {
    "knowledge.py": 2,
    "policy.py": 2,
    "pr_body.py": 2,
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
#: Re-derived by running the walk: policy.py's two are the def and the
#: one call in parse_added_lines (#696 slice 8 removed adequacy.py's
#: three with its diff reader).
EXPECTED_DIFF_HEADER_PATH_CALLERS = {"policy.py": 2}


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
