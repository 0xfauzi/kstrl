"""#428: the engineer-facing notices, and the room the interfaces builder gets.

Split from ``tests/test_feedforward.py`` because that file is 20 lines under
the repo's 800-line ratchet and these two blocks do not fit inside it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kstrl import feedforward
from kstrl.feedforward import (
    CodebaseScanConfig,
    build_codebase_scan_context,
    extract_public_interfaces,
)
from tests.helpers import astwalk
from tests.helpers.feedforward_prompts import NOTICE_PROMPTS
from tests.test_context import section
from tests.test_feedforward import _deep_repo, _deep_repo_with_conventions

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
#: has to explain, not a hole. The one row is the bare "(" between two
#: placeholders in `_format_function_signature`'s signature f-string.
_NOT_A_NOTICE: dict[str, int] = {"(": 1}

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


# ---------------------------------------------------------------------------
# #428: the interfaces builder honours the room left
# ---------------------------------------------------------------------------


def _deep_repo_where_some_files_say_nothing(root: Path) -> None:
    """`_deep_repo_with_conventions`, with every third module silent.

    The shipped fixture gives every module a class, so the count of files
    READ and the count of lines PRODUCED are the same number there and an
    assertion against either passes. Measured at 450 tokens on this tree:
    16 files read, 11 of them yielding a line. That gap is what makes
    ``read`` an assertion about the work actually done rather than an
    identity.
    """
    pkg = root / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod00.py").write_text("class Base00:\n    pass\n", encoding="utf-8")
    for index in range(1, 40):
        head = f"from demo_pkg.mod{index - 1:02d} import Base{index - 1:02d}\n"
        tail = "" if index % 3 == 2 else f"\n\nclass Base{index:02d}:\n    pass\n"
        (pkg / f"mod{index:02d}.py").write_text(head + tail, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )


@pytest.mark.parametrize(
    "component_deps",
    [None, ["demo_pkg"]],
    ids=["whole-repo", "component-filtered"],
)
def test_the_interfaces_builder_stops_when_the_room_left_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component_deps: list[str] | None
) -> None:
    """The room `_append_section` will judge the body against is the room
    the builder is given, so a body that cannot be delivered is not built.

    `build_dependency_graph` has taken that bound since #403. Before #428
    the interfaces builder bound it as `_left` and read every candidate
    file, then handed `_append_section` a body it immediately discarded:
    measured at 400 tokens on this repository, 17704 characters built and
    replaced by a 94-character line.
    """
    _deep_repo_where_some_files_say_nothing(tmp_path)

    read: list[Path] = []
    real = feedforward._extract_symbols_from_file

    def spy(path: Path) -> list[str]:
        read.append(path)
        return real(path)

    monkeypatch.setattr(feedforward, "_extract_symbols_from_file", spy)

    context = build_codebase_scan_context(
        tmp_path,
        CodebaseScanConfig(max_context_tokens=450),
        component_id="demo_pkg",
        component_deps=component_deps,
    )

    interfaces = section(context, "## Public interfaces")
    fit = re.search(
        r"passed the (\d+) characters left in the context budget "
        r"after (\d+) of (\d+) files",
        interfaces,
    )
    assert fit is not None, context
    _room, reported, total = (int(group) for group in fit.groups())
    # The builder stopped, and it stopped where it says it stopped.
    assert len(read) == reported, (read, interfaces)
    assert reported < total, interfaces


def test_an_interfaces_body_that_exactly_fills_the_room_is_delivered_whole(
    tmp_path: Path,
) -> None:
    """The accounting must never OVER-count, or a body that would have fit
    is refused. `_record_edge` carries the same invariant for the graph.

    A bound equal to the finished body's length is the exact-fit case: the
    running total reaches it and never passes it, so nothing is refused.
    Charge the joining newline for the first line as well and the total
    overshoots by one, the last iteration finds the budget spent, and the
    engineer gets a notice instead of a body that fits.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    (pkg / "b.py").write_text("class B:\n    pass\n", encoding="utf-8")
    # A trailing candidate with nothing public. Without a file AFTER the
    # last delivered line the loop ends before its bound check runs again,
    # the over-count is never consulted, and this test cannot fail.
    (pkg / "z.py").write_text("X = 1\n", encoding="utf-8")

    whole = extract_public_interfaces(tmp_path)
    assert "\n" in whole, whole

    assert extract_public_interfaces(tmp_path, max_chars=len(whole)) == whole

    first_line_cost = len(whole.split("\n")[0])
    stopped = extract_public_interfaces(tmp_path, max_chars=first_line_cost - 1)
    assert "after 1 of" in stopped, stopped


def test_the_interfaces_section_as_the_only_section_is_cut_not_refused(
    tmp_path: Path,
) -> None:
    """A FIRST section gets no bound, exactly as `_append_section` exempts
    a first section from its own refusal.

    `_truncate_to_budget` CUTS a lone section to fit rather than dropping
    it, so a builder that refused here would deliver a 130-character
    notice where main delivers 300-odd characters of real interface. Pass
    `left` unconditionally and this goes red.
    """
    _deep_repo(tmp_path)
    config = CodebaseScanConfig(
        module_map=False, dependency_graph=False, conventions=False, max_context_tokens=100
    )

    context = build_codebase_scan_context(tmp_path, config)

    interfaces = section(context, "## Public interfaces")
    assert interfaces.endswith("... (truncated)"), context
    assert "did not fit" not in context, context


def test_a_section_the_assembler_refuses_reads_exactly_as_it_did_on_main(
    tmp_path: Path,
) -> None:
    """The hoist moved ``heading.lower()`` out of the f-string and into a
    ``.format`` argument, and nothing else in the suite reads that word.

    Measured on 6a354cc and on this tree: identical bytes. Pass ``heading``
    instead of ``heading.lower()`` and the engineer reads "Public
    interfaces is 959 characters" where main wrote "public interfaces";
    the snapshot hash does not move, because the template did not change.
    This is the assertion that objects.
    """
    _deep_repo_with_conventions(tmp_path)

    context = build_codebase_scan_context(tmp_path, CodebaseScanConfig(max_context_tokens=600))

    assert section(context, "## Public interfaces") == (
        "(did not fit: public interfaces is 959 characters "
        "against the 937 left in the context budget.)"
    ), context


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
