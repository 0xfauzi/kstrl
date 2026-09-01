"""Every way a kstrl.toml can fail to parse, as real bytes.

One table, imported by the loader tests and by the CLI seam tests, so
that a fault discovered at one level is asserted at both. #318 shipped
twice because that was not true: round 1 fixed the encoding fault at the
loader and covered it across the CLI, and round 2 found a third fault
that walked past the round-1 handler on all the same commands.

REAL BYTES, NEVER A STUB
------------------------
Each entry is the file content, not the exception. Both #318 defects
were about WHICH exception ``tomllib.load`` actually raises, so a
fixture that supplies the exception itself would have passed in both
broken states and proved nothing. The cost of that fidelity is one
temp file per case, measured at well under a millisecond.
"""

from __future__ import annotations

import sys

from kstrl.config import UNPARSEABLE_TOML_MESSAGE

#: Syntactically broken: an unterminated table header. Raises
#: ``tomllib.TOMLDecodeError``, which carries a line and column.
MALFORMED_TOML = b"[verify\ntest_command = 'pytest'\n"

#: Syntactically perfect and not utf-8: one 0xe9, the byte an editor set
#: to ISO-8859-1 writes for an e-acute. ``tomllib.load`` decodes the
#: stream itself before it lexes anything, so this raises
#: ``UnicodeDecodeError`` - a ``ValueError``, and NOT a
#: ``TOMLDecodeError``. That is #318 round 1.
NON_UTF8_TOML = b'[agent]\nname = "\xe9"\n'

#: Valid utf-8, valid TOML grammar, and still unparseable: an integer
#: literal one digit past ``sys.get_int_max_str_digits()`` (4300 by
#: default since 3.11). ``int()`` refuses to build it and raises a PLAIN
#: ``ValueError`` - neither ``TOMLDecodeError`` nor
#: ``UnicodeDecodeError`` - so it walked past both specific handlers.
#: That is #318 round 2, and it is the case that proves the family is
#: not enumerable from outside the parser.
#:
#: Written from the live limit rather than a hardcoded 4301 so the
#: fixture stays one digit over the line if a future interpreter, or a
#: process that called ``sys.set_int_max_str_digits``, moves it.
INT_LIMIT_TOML = b"[run]\nmax_iterations = " + b"9" * (sys.get_int_max_str_digits() + 1) + b"\n"

#: What the loader's catch-all says, IMPORTED from the production
#: constant rather than restated. ``tests/test_config_toml.py`` asserts
#: on this string's ABSENCE from the other two faults' messages, which
#: is half of how the handler order is pinned - and an absence assertion
#: against a stale literal passes vacuously instead of failing. Same
#: reason ``agents.proc.TIMEOUT_MESSAGE_PREFIX`` is imported by its
#: tests rather than repeated in them.
BROAD_FRAGMENT = UNPARSEABLE_TOML_MESSAGE

#: The one source: ``(name, file bytes, the message fragment the
#: operator is shown)``. Private because every caller wants one of the
#: two views below rather than all three fields, and a parametrize whose
#: test never uses the name is how an unused argument gets written.
_FAULTS: list[tuple[str, bytes, str]] = [
    ("syntax", MALFORMED_TOML, "Invalid TOML"),
    ("encoding", NON_UTF8_TOML, "not valid UTF-8"),
    ("int_limit", INT_LIMIT_TOML, BROAD_FRAGMENT),
]

#: ``(file bytes, expected message fragment)``, the pair every
#: parametrize in this suite takes. The fragment is what makes the table
#: an ORDERING assertion as well as a coverage one: move the loader's
#: broad handler above the specific ones and the first two entries stop
#: matching their fragment and start matching :data:`BROAD_FRAGMENT`.
TOML_PARSE_FAULTS: list[tuple[bytes, str]] = [(body, frag) for _, body, frag in _FAULTS]

#: Parametrize ids for :data:`TOML_PARSE_FAULTS`, in the same order.
#: Single words, so `-k` and `--deselect` need no quoting. Derived from
#: the same table rather than restated, so the two cannot drift apart.
FAULT_IDS: list[str] = [name for name, _, _ in _FAULTS]

#: Every fragment any of the three handlers can produce.
#:
#: This is what makes a fault's message assertable EXCLUSIVELY - "says
#: its own fragment and none of the others" - rather than only
#: inclusively. The distinction is the whole ordering guarantee: move
#: the broad handler above the specific ones and each message still
#: contains SOMETHING, so an inclusive assertion on the wrong fragment
#: is the only thing that fails, and an assertion merely that the three
#: messages differ fails nothing at all. (It cannot: every message
#: interpolates the file path, and the three faults are written to
#: three different paths. A first cut of the ordering test asserted
#: exactly that and was vacuous - it passed with all three handlers
#: collapsed into one.)
ALL_FRAGMENTS: frozenset[str] = frozenset(frag for _, _, frag in _FAULTS)
