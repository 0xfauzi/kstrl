"""The interrupted journal write, spelled once.

Two test files assert about the same crash from opposite sides:
``test_decompose.py`` that a torn tail does not cost the convergence
note (the read side), and ``test_journal_torn_tail.py`` that it does not
cost the entry written after it (the write side, #312). Both used to
carry their own copy of the fragment, so the claim that they pin the
same interrupted write was prose. It is an import now.
"""

from __future__ import annotations

from pathlib import Path

#: A partial JSONL line: valid JSON up to the point the process died,
#: with no closing brace and no newline. Short enough to read, and it is
#: the shape a crash actually leaves.
TORN_FRAGMENT = '{"event_type": "spec_iss'

#: Bytes that stop mid-utf-8-sequence: the last byte of "café" removed,
#: leaving a dangling 0xc3 that no decoder will accept. kstrl's own
#: writer emits pure ASCII (``json.dumps`` escapes), so bytes like these
#: reach a journal the way an operator's editor or a foreign writer puts
#: them there.
DANGLING_UTF8 = '{"project": "café'.encode()[:-1]


def tear(path: Path, fragment: str = TORN_FRAGMENT) -> None:
    """Leave ``path`` mid-line the way a crash does: no trailing newline."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(fragment)
