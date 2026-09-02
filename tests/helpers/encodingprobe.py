"""Scan one source string and ask what the #320 walk said about it.

FIVE FUNCTIONS AND ONE DEFINITION OF EACH. Two test modules ask these
questions - ``tests/test_encoding_walk.py`` and
``tests/test_encoding_swallowers.py`` - and a local copy in each is
exactly the shape #318 round 3 paid for: two hand-written lists drifted,
one covered 3 of 6 forms, and nothing made that visible.
"""

from __future__ import annotations

from tests.helpers.encodingwalk import Scan, scan_source


def scan(source: str) -> Scan:
    return scan_source(source, where="probe.py", module="probe")


def reported(source: str) -> tuple[str, ...]:
    return scan(source).reported


def cleared(source: str) -> tuple[str, ...]:
    return scan(source).clear


def undecided(source: str) -> tuple[str, ...]:
    return scan(source).undecided


def cleared_reads(source: str) -> tuple[str, ...]:
    """The cleared rows that are READS THROUGH A HANDLE.

    An ``open`` and the read through its handle are two rows: the open
    answers to the encoding rule, the read to the handler rule. A test
    about the handler rule that asserts on ``_cleared`` as a whole
    therefore fails on the open's own row, which says nothing about its
    subject.
    """
    return tuple(row for row in cleared(source) if "on an open() handle" in row)
