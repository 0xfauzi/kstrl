"""Access to the real tool output checked in under ``tests/tool_output`` (#258).

Every file there was produced by running the named tool at the named
version against a deliberately broken toy project and capturing what came
out of the pipe. None of it is written from documentation or from memory,
because the shapes this suite has to parse are exactly the shapes that
were guessed wrong: the issue predicted tsc would emit ``file:line:col:``
like mypy, and it emits ``src/broken.ts(5,7):`` instead. A hand-written
fixture would have encoded the guess and passed.

The captures are through a PIPE, not a terminal, because that is how
``verify.run_scrubbed`` invokes every gate. That is not cosmetic: vitest
drops its ANSI colour when stdout is not a tty, eslint does the same, and
pytest truncates its short-summary lines to 80 columns, which is why the
pytest fixture's messages end in "...".

Two normalizations are applied, both recorded here because a fixture that
claims to be verbatim has to say where it is not:

1. The capture machine's absolute paths are replaced with ``/repo``.
   Nothing in the suite may depend on one developer's directory layout.
2. Trailing whitespace is stripped and the file ends in exactly one
   newline, so four of these lost the blank line their tool printed
   last. The repo's ``trailing-whitespace`` and ``end-of-file-fixer``
   hooks impose both on the first commit, so the alternative was not
   verbatim files, it was files whose checked-in bytes differ from what
   the tests were written against. What was removed is a blank line and
   some padding inside vitest's code frames; no parser reads either.

The escape characters in ``tsc-5.6.3-pretty.txt`` are deliberate: that
file is real ``--pretty`` output with its ANSI sequences intact, and it
is what proves ``parsers.strip_ansi`` earns its place.
"""

from __future__ import annotations

from pathlib import Path

TOOL_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "tool_output"


def tool_output(name: str) -> str:
    """Captured output of one tool run, by file name."""
    return (TOOL_OUTPUT_DIR / name).read_text(encoding="utf-8")
