"""#428: every engineer-facing notice the codebase scan writes is enrolled."""

from __future__ import annotations

import ast

import pytest

from tests.helpers import astwalk
from tests.helpers.feedforward_prompts import NOTICE_PROMPTS

# ---------------------------------------------------------------------------
# #428: every sentence the engineer reads is enrolled
# ---------------------------------------------------------------------------


def _joined_str_template(node: ast.JoinedStr) -> str | None:
    """The TEMPLATE an f-string writes, folding a run-time placeholder to
    `{<expr>}` instead of giving up on it the way `astwalk.folded_str` does.

    Split out of `_notice_template` (complexipy C003) so the dispatch
    function stays under the repo's cognitive-complexity gate; the
    behaviour is unchanged.
    """
    parts: list[str] = []
    for piece in node.values:
        if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
            parts.append(piece.value)
        elif isinstance(piece, ast.FormattedValue):
            parts.append("{" + ast.unparse(piece.value) + "}")
        else:
            return None
    return "".join(parts)


def _notice_template(node: ast.AST) -> str | None:
    """The TEMPLATE a string expression writes, or None if it is not one.

    `astwalk.folded_str` stops at a placeholder it cannot decide, which is
    the skip direction for this subject: a notice written
    `f"(did not fit: {n} characters)"` folds to None there and is invisible
    to the one walk that exists to find it. Here the placeholder becomes
    `{<expr>}` instead, so an f-string carrying run-time values is still a
    template this census can see and name.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return _joined_str_template(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _notice_template(node.left), _notice_template(node.right)
        return None if left is None or right is None else left + right
    return None


def _is_notice(node: ast.AST) -> bool:
    """Parenthesised prose is how every notice in this module opens."""
    return (_notice_template(node) or "").startswith("(")


#: Strings this net over-matches on. It FLAGS rather than clears, so per
#: CLAUDE.md's third static-guard rule over-matching costs a row a reader
#: has to explain, not a hole. Empty since #696 slice 6 deleted
#: `_format_function_signature`, whose bare "(" was the one row.
_NOT_A_NOTICE: dict[str, int] = {}

#: Every notice template `kstrl/feedforward.py` can put in front of the
#: engineer, keyed by the template itself rather than by module. Closed by
#: construction: the expected set is READ OFF the enrolled constants, so a
#: sixth notice written as a bare f-string is a row with no constant behind
#: it and fails here, while a REWORD of an enrolled notice moves this row
#: and its constant together and is caught by the H3 hash instead.
EXPECTED_NOTICES: dict[str, int] = {
    **{body: 1 for body in NOTICE_PROMPTS.values()},
    **_NOT_A_NOTICE,
}


def test_every_notice_the_engineer_reads_is_enrolled() -> None:
    """The block this module builds is pasted into the engineer prompt.

    PR #417 removed "Raise codebase_scan.max_context_tokens to see it." from
    the dependency graph's notice by hand and left no guard behind.
    Measured on 6a354cc: putting a short imperative back into the #420
    notice leaves the whole suite green (7203 passed, 0 failed). This is
    the mechanism that was missing.
    """
    astwalk.assert_census(
        sources=[astwalk.KSTRL_PACKAGE / "feedforward.py"],
        sees=_is_notice,
        key=lambda _source, node: _notice_template(node) or "",
        expected=EXPECTED_NOTICES,
        control=(
            # One per branch of _notice_template: a plain literal, an
            # f-string carrying a run-time value, and an explicit "+".
            'X = "(none: nothing was found)"\n',
            'def f(n):\n    return f"(did not fit: {n} characters)"\n',
            'X = "(none: " + "already said)"\n',
        ),
        message=(
            "the set of parenthesised notices kstrl/feedforward.py can put in "
            "front of the engineer changed. Every one of them is prompt text: "
            "hoist it to a *_PROMPT constant, enrol it in "
            "tests/helpers/feedforward_prompts.py (body, snapshot, renderer) "
            "and bump CODEBASE_SCAN_NOTICE_PROMPT_VERSION, or, if it is not a "
            "notice, add the row to _NOT_A_NOTICE with the reason."
        ),
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_notice_that_does_not_open_with_a_paren_is_not_seen() -> None:
    """Disclosed limit: the net keys on the opening "(" every notice in
    this module is written with. A notice spelled any other way is
    invisible to it, and this row is what stops that going unsaid.
    """
    astwalk.blind_spot(
        lambda text: any(_is_notice(node) for node in astwalk.all_nodes(astwalk.parse(text))),
        'X = "none: no Python source root found"\n',
    )
