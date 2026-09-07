"""Building and reading one operator-authored context block.

Shared by ``tests/test_operator_context.py`` (the R10.8 loader, one file)
and ``tests/test_operator_file_kinds.py`` (the R10.9 row table, every
file). It lives here rather than in either of them because
``tests/helpers/`` may not import a test module
(``tests/test_helper_import_direction.py``), so a helper two test modules
share has exactly one place to be.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from kstrl.init_cmd import SCAFFOLDED_TEMPLATES
from kstrl.operator_context import (
    GOLDEN_PATTERNS,
    OperatorFile,
    OperatorFileKind,
    operator_file_spec,
)

#: The delimiter lines carry a per-build random token (S4), so a test
#: matches the fixed part and asserts the token is there rather than
#: spelling a whole line it could not predict.
TOKEN = re.compile(r"^KSTRL-DATA-[0-9a-f]{32} ===$")

#: The body ``ks init`` writes today, per scaffolded filename. Looked up
#: rather than imported one constant at a time, so a kind added to
#: ``OPERATOR_FILES`` is covered by the parametrized cases the moment its
#: ledger row lands.
SHIPPED_BODIES = {t.filename: t.body for t in SCAFFOLDED_TEMPLATES}


def spec_for(
    kind: OperatorFileKind,
    path: Path,
    max_chars: int | None = None,
    scaffold: str | None = None,
) -> OperatorFile:
    """A spec built the way production builds one, with the two knobs varied.

    Through ``operator_file_spec`` and not through a literal, so a field
    added to :class:`~kstrl.operator_context.OperatorFile` reaches these
    cases the same way it reaches the factory, and so a case parametrized
    over ``OPERATOR_FILES`` exercises the row rather than a copy of it.
    """
    built = operator_file_spec(kind, path.parent, path)
    return replace(
        built,
        max_chars=kind.max_chars if max_chars is None else max_chars,
        scaffold=scaffold,
    )


def golden(
    path: Path,
    max_chars: int | None = None,
    scaffold: str | None = None,
) -> OperatorFile:
    """:func:`spec_for` pinned to the golden-patterns row.

    The R10.8 cases were written against a golden-only loader and their
    docstrings carry the round-1 and round-2 audit trail, so R10.9 moved
    them by rename and by nothing else.
    """
    return spec_for(GOLDEN_PATTERNS, path, max_chars, scaffold)


def split_block(block: str) -> tuple[str, list[str], str]:
    """``(open line, inner lines, close line)`` of a rendered block."""
    lines = block.split("\n")
    return lines[0], lines[1:-1], lines[-1]


def assert_delimited(block: str, header: str) -> str:
    """Both delimiters present, well formed, and carrying ONE token.

    Takes the header rather than closing over one, so the R10.8 cases
    and the cases parametrized over ``OPERATOR_FILES`` check the same
    thing. The open-token-equals-close-token half is the reason this is
    one function: an inline copy that checks each line separately passes
    a block whose two delimiters carry different tokens, which is a
    block a body could have split.
    """
    opened, _inner, closed = split_block(block)
    start, end = f"=== {header} ", f"=== END {header} "
    assert opened.startswith(start)
    assert closed.startswith(end)
    token = opened[len(start) :]
    assert TOKEN.match(token), token
    assert closed[len(end) :] == token
    return token.split(" ")[0]


def body_of(block: str) -> str:
    """The operator's own text out of a rendered block."""
    return block.split("\n", 1)[1].split("\n[truncated:", 1)[0]
