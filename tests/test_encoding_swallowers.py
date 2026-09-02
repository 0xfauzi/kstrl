"""What the #320 walk treats as a construct that swallows an exception.

``tests/helpers/encodingguards.py`` is the code; this is one fixture per
claim it makes. It is a separate module from
``tests/test_encoding_walk.py`` for the same reason the helper is a
separate module: the 800-line ratchet, and a cut the subject already
made.

None of these shapes is around a text read in ``kstrl/`` today. That is
the point, not a reason to skip them: the hole was open through three
review rounds precisely because a package-level count cannot fail on a
spelling nobody has written yet.
"""

from __future__ import annotations

import ast

from tests.helpers.astwalk import handler_clauses, try_body_nodes
from tests.helpers.encodingprobe import cleared, cleared_reads, reported, scan, undecided


class TestTheSwallowerIsNotAlwaysSpelledTry:
    """#344 round 4's subject, and the lesson is bigger than the shapes.

    The walk used to enumerate ``ast.Try``. THAT IS A SPELLING, NOT THE
    PROPERTY. The property is "a construct that swallows exceptions
    here", and three things have it: ``try``, ``try/except*``, and a
    ``with`` whose ``__exit__`` returns true. ``contextlib.suppress
    (OSError)`` is #320's own defect written without the word ``try``,
    and it is a live idiom in this repo - ``procdispose.py`` and
    ``config_preflight.py`` both use it.

    None of these is around a text read in ``kstrl/`` today, which is
    precisely why nothing failed while the hole was open. The fixtures
    are the mechanism; the package count is not.
    """

    def test_a_suppress_that_hides_the_io_alone_is_reported(self) -> None:
        """The headline. This module has no ``try`` in it at all, and it
        commits #320's defect exactly."""
        source = (
            "from contextlib import suppress\n"
            "def f(p):\n"
            "    with suppress(OSError):\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            return h.read()\n"
        )
        assert any("suppress(OSError)" in row for row in reported(source)), scan(source)

    def test_a_suppress_that_covers_the_decode_clears(self) -> None:
        """The other direction, so the rule above is not satisfied by
        refusing every ``suppress``."""
        source = (
            "from contextlib import suppress\n"
            "def f(p):\n"
            "    with suppress(OSError, UnicodeDecodeError):\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            return h.read()\n"
        )
        assert cleared(source) and not reported(source) and not undecided(source)

    def test_a_suppress_whose_arguments_do_not_fold_isundecided(self) -> None:
        """``suppress(*NAMES)`` is the shape ``config_preflight.py``
        actually writes. The walk cannot read the tuple, so it declines
        to answer rather than assuming either way."""
        source = (
            "from contextlib import suppress\n"
            "BOTH = (OSError, UnicodeDecodeError)\n"
            "def f(p):\n"
            "    with suppress(*BOTH):\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            return h.read()\n"
        )
        assert any("cannot name" in row for row in undecided(source)), scan(source)
        assert not cleared_reads(source), scan(source)

    def test_a_suppress_that_answers_for_nothing_lets_the_ladder_continue(self) -> None:
        """``suppress()`` swallows nothing, so the ``try`` outside it is
        still the construct that answers. A walk that treated any
        ``suppress`` as terminal would clear this."""
        source = (
            "from contextlib import suppress\n"
            "def f(p):\n"
            "    try:\n"
            "        with suppress():\n"
            "            with open(p, encoding='utf-8') as h:\n"
            "                return h.read()\n"
            "    except OSError:\n"
            "        return None\n"
        )
        assert any("except OSError" in row for row in reported(source)), scan(source)

    def test_an_except_star_that_misses_the_decode_is_reported(self) -> None:
        """``ast.TryStar`` is a different node type with the same
        meaning, and an ``isinstance(node, ast.Try)`` does not match it."""
        source = (
            "def f(p):\n"
            "    got = None\n"
            "    try:\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            got = h.read()\n"
            "    except* OSError:\n"
            "        pass\n"
            "    return got\n"
        )
        assert any("except OSError" in row for row in reported(source)), scan(source)

    def test_an_except_star_that_covers_the_decode_clears(self) -> None:
        source = (
            "def f(p):\n"
            "    got = None\n"
            "    try:\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            got = h.read()\n"
            "    except* (OSError, UnicodeDecodeError):\n"
            "        pass\n"
            "    return got\n"
        )
        assert cleared(source) and not reported(source) and not undecided(source)

    def test_a_context_manager_that_is_not_open_isundecided(self) -> None:
        """Option A in one fixture. ``TemporaryDirectory.__exit__``
        returns None and swallows nothing, and the walk still refuses to
        say so, because the thing it would have to read is a method body
        in another module."""
        source = (
            "import tempfile\n"
            "def f(p):\n"
            "    with tempfile.TemporaryDirectory():\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            return h.read()\n"
        )
        assert any("__exit__" in row for row in undecided(source)), scan(source)
        assert not cleared_reads(source), scan(source)

    def test_an_outer_handler_does_not_clear_past_a_context_manager(self) -> None:
        """The INNERMOST construct that answers decides, and a ``with``
        item has no ``lineno`` of its own to be sorted by.

        Measured while writing this: keying the ladder on
        ``getattr(node, 'lineno', 0)`` sorted every ``with`` item to line
        0 - OUTERMOST - so the ``try`` below answered first and cleared
        the read. Which is the whole hole: if the context manager were
        ``suppress(Exception)`` the handler would never run at all.
        """
        source = (
            "import tempfile\n"
            "def f(p):\n"
            "    try:\n"
            "        with tempfile.TemporaryDirectory():\n"
            "            with open(p, encoding='utf-8') as h:\n"
            "                return h.read()\n"
            "    except (OSError, UnicodeDecodeError):\n"
            "        return None\n"
        )
        assert not cleared_reads(source), "an unreadable context manager was cleared past"
        assert any("__exit__" in row for row in undecided(source)), scan(source)

    def test_the_open_context_manager_itself_does_not_stop_the_ladder(self) -> None:
        """The one ``with`` the walk PROVES. Without this arm every
        ``with open(...) as h:`` in the package would be undecided, and
        the 84-row cleared inventory would be nearly empty - a guard
        that answers 'I do not know' about everything carries no more
        information than one that clears everything.
        """
        source = (
            "def f(p):\n"
            "    try:\n"
            "        with open(p, encoding='utf-8') as h:\n"
            "            return h.read()\n"
            "    except (OSError, UnicodeDecodeError):\n"
            "        return None\n"
        )
        assert cleared(source) and not reported(source) and not undecided(source)


class TestTheSharedResolverHandlesExceptStar:
    """``handler_clauses`` and ``try_body_nodes`` accept ``ast.TryStar``
    at RUN TIME, not only in their annotations.

    #344 round 4 widened both signatures and mutated them, and the
    mutation came back GREEN: narrowing the parameter type back to
    ``ast.Try`` changes nothing pytest can see, because ``ast.TryStar``
    carries ``.handlers`` and ``.body`` exactly as ``ast.Try`` does. The
    annotation is enforced by mypy, and ``pyproject.toml`` scopes mypy to
    ``kstrl/``, so nothing in CI reads it.

    These two assertions are the part of that claim CI can check. The
    annotation row in the mutation battery is run by hand against mypy
    and says so; this is the mechanism that does not depend on that.
    """

    SOURCE = (
        "def f(p):\n"
        "    try:\n"
        "        p.read_text(encoding='utf-8')\n"
        "    except* (OSError, ValueError):\n"
        "        pass\n"
    )

    def _try_star(self) -> ast.TryStar:
        found = [n for n in ast.walk(ast.parse(self.SOURCE)) if isinstance(n, ast.TryStar)]
        assert len(found) == 1, found
        return found[0]

    def test_handler_clauses_reads_an_except_star_ladder(self) -> None:
        clauses = handler_clauses(self._try_star())
        assert [sorted(c.names) for c in clauses] == [["OSError", "ValueError"]]
        assert all(c.decided for c in clauses)

    def test_try_body_nodes_reads_an_except_star_body(self) -> None:
        body = try_body_nodes(self._try_star())
        assert any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "read_text" for n in body
        ), [ast.dump(n) for n in body]
