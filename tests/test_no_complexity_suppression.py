"""A permanent complexity-gate suppression is refused, closed over kstrl/.

``# complexipy: ignore`` (see ``.pre-commit-config.yaml``'s complexipy
hook) removes a function from complexipy's census entirely: with the
marker in place, a function that later grows past the gate reports
nothing at all, because the row is gone rather than measured and passing.
That is the exact failure mode ``scripts/precommit/cyclomatic_ratchet.py``
was built to prevent for its own metric, and CLAUDE.md forbids the same
shape for complexipy ("Do NOT add `# noqa: C901` to any function... The
escape hatches are all closed").

#395 added the repository's first such marker (on
``review.claim_retry_context``) and then removed it again by extracting
the function's body into helpers that measure under the gate on their
own; #423/#426 hit and fixed the identical shape for ``run_loop`` a
Python file over. Neither PR left a guard behind, so a third instance
would go unnoticed until the next code review happened to grep for it.
This is that guard.

A comment is not part of the AST kstrl's other static guards walk, so
this is a plain line scan rather than an ``astwalk`` user: there is no
call site or binding to resolve, only a marker either present in the
source text or not.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KSTRL_DIR = REPO_ROOT / "kstrl"

#: Matches "# complexipy: ignore", "#complexipy:ignore", any amount of
#: whitespace around the colon, case-insensitively, so the guard is not
#: defeated by a spelling variant that still does what the tool reads.
SUPPRESSION_PATTERN = re.compile(r"#\s*complexipy\s*:\s*ignore", re.IGNORECASE)


def _kstrl_python_files() -> list[Path]:
    return sorted(KSTRL_DIR.rglob("*.py"))


def test_no_complexipy_suppression_marker_anywhere_in_kstrl() -> None:
    files = _kstrl_python_files()
    assert len(files) >= 50, f"walked only {len(files)} files - the walk is not running"

    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if SUPPRESSION_PATTERN.search(line):
                rel = path.relative_to(REPO_ROOT).as_posix()
                hits.append(f"{rel}:{lineno}: {line.strip()}")

    assert not hits, (
        "a complexipy suppression marker permanently removes a function "
        "from the complexity census instead of bringing it under the "
        "gate; extract the function's body until it measures under the "
        "limit instead:\n" + "\n".join(hits)
    )
