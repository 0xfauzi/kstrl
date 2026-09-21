"""Reading a JSON document: the parse, and its error taxonomy.

#427. The sibling of ``kstrl.config_toml.load_toml_document``, one
parser over, and it exists for the same reason.

``json.loads`` raises for a family of bad input that is not enumerable
from the outside, and every enumeration in this package was wrong:

- ``json.JSONDecodeError``, a syntax error. What 28 of the 52 sites named.
- a plain ``ValueError``: ``json.loads("1" * 5000)`` raises "Exceeds the
  limit (4300 digits) for integer string conversion" out of
  ``sys.get_int_max_str_digits``. NOT a ``JSONDecodeError``, so it walked
  past every one of those 28.
- ``RecursionError``, from the scanner's recursive descent, at no one
  depth: the caller's own stack sets it. It derives from
  ``RuntimeError``, NOT ``ValueError``, so it walked past the 14 sites
  that caught ``ValueError`` as well.

So the catch-all is ``Exception``, and that is a ceiling rather than a
fourth guess: everything a parser can say about a DOCUMENT derives from
it, while ``KeyboardInterrupt`` and ``SystemExit`` are about the PROCESS
and must never be relabelled as a bad document.

ALL the I/O is outside the guard. :func:`read_json` takes text and
:func:`read_json_file` reads the handle before it calls it, so nothing
the guard catches can be an I/O fault and no widening can reach an
``OSError``. That is the same guarantee ``load_toml_document`` gives,
and it deletes an ``except OSError: raise`` clause no test could enter.

The ``JSONDecodeError`` clause is a PASS-THROUGH, not an enumeration:
it re-raises the parser's own exception unchanged so that the 28 sites
whose error strings carry ``{exc}`` keep saying exactly what they said.
It is above the catch-all, which is where a specific clause has to be
or it can never run. Measured: an owner that wrapped a syntax error
instead of passing it through turned four tests red, among them
``tests/test_observability.py::TestReadProgressEvents::
test_skips_malformed_tail_line`` and
``tests/test_manifest.py::TestManifest::test_load_invalid_json``.

``tests/test_json_readers.py`` is the guard: this is the only module in
``kstrl/`` that may call ``json.load`` or ``json.loads``.
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class _Readable(Protocol):
    """What :func:`read_json_file` needs of a handle, and nothing more."""

    def read(self) -> str: ...


class JsonDocumentError(json.JSONDecodeError):
    """A JSON document the parser rejected for a reason that was not a
    ``JSONDecodeError``.

    A ``JSONDecodeError`` SUBCLASS, deliberately. Every reader in this
    package already handles that type or its ``ValueError`` base, several
    on purpose, so a new sibling type would have meant editing 28
    handlers and getting one of them wrong turns "skip the torn line"
    into a crash.

    ``ValueError.__init__`` runs after ``super().__init__`` so that
    ``str(exc)`` is the parser's own message and nothing more. The
    inherited constructor would append "line 1 column 1 (char 0)", which
    is a position this exception does not have: the failure was not at a
    position. ``msg``, ``doc``, ``pos``, ``lineno`` and ``colno`` keep
    the type's contract; nothing in kstrl reads them.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, "", 0)
        ValueError.__init__(self, message)

    def __reduce__(self) -> tuple[Any, ...]:
        # The inherited __reduce__ calls the class with (msg, doc, pos),
        # which this one-argument constructor refuses. Exceptions cross
        # process boundaries; a pickle that raises on the way back is a
        # worse failure than the one it carries.
        return (self.__class__, (self.msg,))


def read_json(text: str | bytes) -> Any:
    """Parse one JSON document. Raises ``json.JSONDecodeError``, always.

    A syntax error comes back as the parser's own exception, unchanged.
    Anything else the parse raises comes back as
    :class:`JsonDocumentError`, whose message repeats what the parser
    said and claims nothing beyond it.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise
    except Exception as exc:
        # LAST, and ``Exception`` rather than ``ValueError``: see the
        # module docstring. Says what the parser said; claims no cause
        # beyond that.
        raise JsonDocumentError(f"{type(exc).__name__}: {exc}") from exc


def read_json_file(fp: _Readable) -> Any:
    """:func:`read_json` for an open text handle.

    The read happens HERE, outside the guard, so an ``OSError`` from the
    handle stays an ``OSError`` for the caller that opened the file.
    """
    return read_json(fp.read())
