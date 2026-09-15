"""Reading ``kstrl.toml``: the parse, its error taxonomy, and the scope
that parses one file once.

Split out of ``kstrl/config.py`` by #366, which was 19 lines under the
800-line file-length ratchet. Nothing here changed in the move. Every
name below is re-exported from ``kstrl.config``, which is where every
caller in this tree imports it from, so the split is invisible outside
these two files.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Configuration the operator has to fix before anything can run.

    A ``ValueError`` SUBCLASS, deliberately: every loader in this package
    has always raised ``ValueError`` for a rejected value, and several
    callers catch exactly that on purpose (``ks config show``,
    ``EvolutionConfig.load_or_none``, the TUI config screen). Narrowing
    the type would silently step outside those guards. What the subclass
    adds is a name the CLI can catch at its entry seam and render as an
    ``error:`` line, the way ``BudgetConfigError`` (also a ValueError)
    already is - so a typo in kstrl.toml stops being an unhandled
    traceback from wherever the run happened to reach first (#272).
    """


#: What :func:`load_toml_document`'s catch-all says when the parser
#: rejected the file for a reason it has not established. Deliberately
#: says nothing about the cause; see that function's docstring.
#:
#: A named constant because the tests assert on its ABSENCE from the two
#: specific handlers' messages, which is how the handler order is
#: pinned. A reworded literal restated in a test would make those
#: assertions vacuously true rather than failing, so the test imports
#: this. Same reason ``agents.proc.TIMEOUT_MESSAGE_PREFIX`` is a
#: constant the tests import rather than a string they repeat.
UNPARSEABLE_TOML_MESSAGE = "could not be parsed as TOML"

#: Documents parsed inside the innermost :func:`toml_parse_scope`, or
#: None outside one. A ContextVar rather than a module global so a
#: scope cannot leak into another thread or task.
_PARSE_SCOPE: ContextVar[dict[Path, dict[str, Any]] | None] = ContextVar(
    "kstrl_toml_parse_scope",
    default=None,
)


@contextmanager
def toml_parse_scope() -> Iterator[None]:
    """Parse each kstrl.toml at most once for the duration of the block.

    Every config dataclass reads its own section, and each read reparses
    the whole file: resolving all 22 sections at command entry lexed the
    same bytes 23 times. Measured on the shipped 21 KB
    kstrl.toml.example, the entry check cost 9.4 ms without this scope
    and 0.6 ms with it, which took `ks status` from 159.9 ms to
    157.7 ms against the same file.

    SCOPED, not process-wide, and the distinction is the whole design.
    A process-wide snapshot would freeze the file for the life of the
    process, which two surfaces would be wrong about: the TUI config
    screen has a refresh action whose job is to re-read the file on
    demand (``tui/screens/config.py``), and ``ks serve`` is a long-lived
    daemon that re-reads config per queue item. Inside a block that runs
    for under a millisecond there is no window for either, and no
    caller outside one sees any change at all.

    Nothing in this package mutates what ``load_toml_section`` returns,
    which is what makes handing out the same sub-dict twice safe.
    """
    token = _PARSE_SCOPE.set({})
    try:
        yield
    finally:
        _PARSE_SCOPE.reset(token)


def load_toml_document(path: Path) -> dict[str, Any]:
    """Load and parse a TOML file.

    Raises :class:`ConfigError` for anything the PARSE raises, naming the
    file in all of them. Two things deliberately pass through instead:
    anything deriving from ``BaseException`` rather than ``Exception``,
    and every I/O failure, which cannot reach the guard because all of
    the I/O happens before it.

    Inside a :func:`toml_parse_scope` the parsed document is reused
    rather than re-read.

    ``tomllib.load`` raises for a whole family of bad input, and the
    family is not enumerable from the outside. #318 tried three times
    and the sequence is the argument for where it stopped:

    - ``TOMLDecodeError``, a syntax error. All the original named.
    - ``UnicodeDecodeError``, a file that is not utf-8, because
      ``tomllib.load`` decodes the stream ITSELF before it lexes
      anything. A ``ValueError``, NOT a ``TOMLDecodeError``, so it
      walked past that. Same defect
      ``verify._default_typecheck_command`` fixed for pyproject.toml in
      #288, and the encoding rule CLAUDE.md states from #291.
    - A plain ``ValueError``, for input the parser accepts and Python
      then refuses to build: ``max_iterations = <4301 digits>`` raises
      "Exceeds the limit (4300 digits) for integer string conversion"
      from ``sys.get_int_max_str_digits``. Walked past round 1.
    - ``RecursionError``, from tomllib's recursive-descent parser, at no
      one depth: at the default limit nested arrays first fail at 497
      levels from a one-frame caller and 397 under 200 more, inline
      tables at 331 and 264; the caller's own stack sets it. It derives from
      ``RuntimeError``, NOT ``ValueError``, so it walked past round 2 -
      whose docstring, whose AST guard and whose CLAUDE.md line all
      asserted that ``ValueError`` WAS the whole class. Round 2 stated
      the right thesis and then named the wrong ceiling for it, which
      is a worse failure than round 1: a future author could satisfy
      every guard it left behind and still take the CLI down.

    So the catch-all is ``Exception``, and that is a ceiling rather than
    a fourth guess. Everything a parser can say about a DOCUMENT derives
    from ``Exception``. What does not derive from it is
    ``KeyboardInterrupt`` and ``SystemExit`` - which are about the
    PROCESS, not the file, and must never be relabelled as the
    operator's broken config. ``MemoryError`` on a hostile-sized file IS
    covered, since it derives from ``Exception``, with the honest caveat
    that no handler can promise the interpreter has the headroom left to
    render the message.

    Provenance follows (#323): the parse runs 9 to 16 frames deep under
    ``ks status``, 13 on the Textual event loop's own callbacks (one TUI
    parse inherits its caller's stack), so a ``RecursionError`` here is
    the document's. Not by construction: measured, a CALLER with 5 or 6
    of 1000 frames left gets a valid file called unparseable, and IS the
    runaway. ``raise_if_defect`` has the rest.

    ``OSError`` is not in the catch-all's reach at all, which is the
    stronger form of the guarantee two callers depend on. Re-raising it
    from inside the guard was correct and unpinnable: with the I/O
    hoisted out, no test could reach that clause, and a special case no
    test can enter is a special case that has not been deleted yet.

    The rule, after being wrong three times: a parser's error taxonomy
    belongs to the parser, and a reader naming any class narrower than
    "an exception, out of this call" is asserting something about the
    standard library that it cannot check.

    Fail closed WITHOUT overclaiming, though. The catch-all names the
    file and repeats what the parser said; it does not diagnose a cause
    it has not established. Telling an operator to re-save a file that
    is already good utf-8 would be a silent semantic substitution one
    step on from swallowing the error.

    That is what earns the ``UnicodeDecodeError`` rung specifically: the
    remedy "re-save as utf-8" is nowhere in ``str(exc)``, so a reader
    who saw only the catch-all's line would lose it. It is NOT what
    earns the ``TOMLDecodeError`` rung, and saying so would be
    over-fitting the rule to two cases. That rung's ``{exc}`` carries
    the line and column, and the catch-all interpolates ``{exc}``
    identically, so it would lose nothing diagnostic. What keeps it is
    narrower and duller: "Invalid TOML" is a message contract, asserted
    at thirteen sites across five test files including the TUI banner an
    operator reads. It stays for compatibility, not because it tells
    anyone more.

    Order is load-bearing, and only because of the catch-all.
    ``TOMLDecodeError`` and ``UnicodeDecodeError`` are SIBLINGS - both
    derive from ``ValueError``, neither from the other - so their
    relative order is free, and a round-1 test that claimed to pin it
    passed with the two reversed. The broad clause is the real
    constraint: it is a supertype of every clause above it, so it must
    come last or it swallows them and relabels every syntax error and
    every bad byte as an unspecified parse failure.
    ``test_the_broad_handler_must_come_last`` fails if it moves; it was
    watched failing, with ``__pycache__`` purged, because a handler
    permutation leaves the file byte-identical and a same-second rewrite
    is otherwise served from a stale ``.pyc``.

    See ``config_preflight.SURFACE_REJECTIONS`` for the two callers that
    depend on telling an unreadable file from an unparseable one.
    """
    scope = _PARSE_SCOPE.get()
    if scope is not None and path in scope:
        return scope[path]
    # ALL the I/O happens here, OUTSIDE the guard, so that nothing the
    # guard catches can be an I/O fault. That is what lets the catch-all
    # be as wide as it is without lying: it cannot reach an ``OSError``
    # that ``config_preflight.SURFACE_REJECTIONS`` needs to stay raw,
    # and it cannot reach ``open``'s own ``ValueError("embedded null
    # byte")`` on ``Path("bad\\x00path.toml")``, which an earlier draft
    # relabelled "could not be parsed as TOML" for a file it had never
    # opened. ``tomllib.load`` is ``fp.read()``, ``b.decode()``,
    # ``loads(s)``; splitting it here changes no exception type (all
    # four faults measured identical both ways) and removes the need for
    # an ``except OSError: raise`` clause that no test could pin.
    raw = path.read_bytes()
    try:
        data: dict[str, Any] = tomllib.loads(raw.decode())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid UTF-8, which TOML requires; re-save the file as UTF-8: {exc}"
        ) from exc
    except Exception as exc:
        # LAST, and ``Exception`` rather than ``ValueError``: see the
        # docstring. Says what the parser said and names the file;
        # claims no cause beyond that.
        raise ConfigError(f"{path} {UNPARSEABLE_TOML_MESSAGE}: {exc}") from exc
    if scope is not None:
        scope[path] = data
    return data


def load_toml_section(toml_path: Path, section: str) -> dict[str, Any]:
    """Read a named section from a kstrl.toml file.

    Shared by every config dataclass that has a corresponding
    ``[section]`` in the canonical kstrl.toml. Returns ``{}`` when the
    file or the section is absent; raises :class:`ConfigError` (a
    ``ValueError``) with a clear message when the file will not parse -
    a syntax error OR a non-utf-8 byte, see :func:`load_toml_document` -
    so every loader behaves consistently. ``OSError`` is NOT normalized
    into that, so a caller reading this file without the entry check in
    front of it catches both. Sub-section keys that are not
    dicts (e.g. someone
    wrote ``factory = "hi"`` instead of ``[factory]``) return ``{}``
    rather than crashing later in the per-key cast.
    """
    if not toml_path.exists():
        return {}
    data = load_toml_document(toml_path)
    section_data = data.get(section, {})
    if not isinstance(section_data, dict):
        return {}
    return section_data
