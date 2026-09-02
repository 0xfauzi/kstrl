"""The invariant #320's walk rests on, tested against the interpreter.

WHY THIS FILE EXISTS RATHER THAN A TENTH CASE. #344's review found eight
clearing shapes in round 2, nine more in round 3 and nineteen in round 4,
and the guard's answer the first two times was another fixture. A pile of
fixtures is a collection of cases, and a collection of cases is closed
only over the shapes somebody already thought of - CLAUDE.md's
guard-design rule 1 names that as a ledger of give-ups and says to prefer
closed by construction. So this file does not assert about shapes. It
asserts the SAFETY PROPERTY the whole guard means:

    a read that SITS UNDER A HANDLER answering for the IO and not for
    the decode is never one the walk CLEARS.

THE PRECONDITION IS PART OF THE PROPERTY, and leaving it out made an
earlier draft of this docstring say something false. That draft claimed
"a read whose UnicodeDecodeError escapes at RUN TIME is never cleared",
and this three-line module violates it::

    def f(p):
        h = open(p, encoding='utf-8')
        return h.read()

The decode escapes, because nothing catches it, and the walk clears the
read - correctly, because there is no handler here to be wrong about and
the CALLER is the site that answers. MEASURED on ``kstrl/``: of the 48
decodes the handler rule is charged to, 8 sit under no swallowing
construct at all, and all 8 are cleared. The overclaim was not
harmless: the first author to add a handler-less row would have watched
this file fail for a reason that is not a defect, and the natural repair
is to WEAKEN the assertion. So the precondition is checked mechanically
by :func:`_answers_for_io_only` and its population is pinned, and a row
that does not meet it skips with its own message instead.

AND THE WALK MAY DECLINE TO ANSWER. #344 round 4 stopped patching the
clearing path and changed the rule: a construct the walk cannot PROVE
harmless makes the read ``undecided`` rather than ``clear``. So a
compliant shape has two acceptable answers, ``clear`` and ``undecided``,
and which one each gets is pinned in :data:`UNPROVABLE` rather than left
to whichever the walk happens to give.

THE ORACLE IS CPYTHON, not a second implementation of the walk. Every row
below is one source string used twice: scanned by the walk, and EXECUTED
against a file holding a byte no UTF-8 decoder accepts. What the
interpreter does with it is the truth, and the walk has to be
conservative with respect to that truth. An oracle written as a second
AST walk would drift from the first and agree with it for the wrong
reasons; #318 round 3 records two hand-written lists doing exactly that.

Adding a shape is one row, and the row cannot be half-written: the same
string feeds both halves, so a fixture that plants a defect the
interpreter does not actually commit shows up as ``CAUGHT`` and is
excluded from the safety claim rather than silently strengthening it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.helpers.astwalk import handler_clauses, leaf_name
from tests.helpers.encodingrules import answers_for_io, covers_the_decode
from tests.helpers.encodingwalk import scan_source

#: A byte no UTF-8 decoder accepts, inside a document that is otherwise
#: exactly what each reader expects. Shared with
#: ``tests/test_encoding_sites.py``'s reasoning: an unreadable-file
#: fixture would pass against every version of this walk.
BAD_BYTES = b'{"na\xefve": 1}\n'

#: ``(name, module source)``. Each defines ``f(p)`` and reads ``p``'s text
#: under a handler that answers for the IO and NOT for the decode, which
#: is #320's whole subject. The shapes are every one #344's three review
#: rounds found clearing, plus the ones that held, plus the compliant
#: controls.
#:
#: One string per row, used by both halves. That is the rule #318 round 3
#: paid for: two hand-written lists drifted, the unguarded half covered 3
#: of 6 forms, and nothing made it visible.
SHAPES: list[tuple[str, str]] = [
    # --- round 2: the handle is never bound to a followable name -------
    (
        "chained builtin",
        "def f(p):\n    try:\n        return open(p, encoding='utf-8').read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "chained Path.open",
        "def f(p):\n    try:\n        return p.open(encoding='utf-8').read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "handed over inline",
        "import json\ndef f(p):\n    try:\n"
        "        return json.load(open(p, encoding='utf-8'))\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "two targets",
        "def f(p):\n    try:\n        h = g = open(p, encoding='utf-8')\n"
        "        return h.read()\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "bare statement then reopen",
        "def f(p):\n    try:\n        open(p, encoding='utf-8')\n"
        "        return open(p, encoding='utf-8').read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "keyword hand-over",
        "import json\ndef f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        return json.load(fp=h)\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "comprehension",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        return [x for x in h]\n    except OSError:\n        return 'caught'\n",
    ),
    # --- round 3 finding 1: the bound method escapes the attribute rule -
    (
        "bound method alias",
        "def f(p):\n    try:\n        with open(p, encoding='utf-8') as h:\n"
        "            _read = h.read\n            return _read(64)\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "bound method handed over",
        "def f(p):\n    try:\n        with open(p, encoding='utf-8') as h:\n"
        "            return (lambda r: r())(h.readlines)\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "buffer rewrapped",
        "import io\ndef f(p):\n    try:\n"
        "        with open(p, encoding='utf-8') as h:\n"
        "            return io.TextIOWrapper(h.buffer, encoding='utf-8').read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    # --- round 3 finding 2: the handle crosses a scope ------------------
    (
        "closure over the handle",
        "def f(p):\n    h = open(p, encoding='utf-8')\n"
        "    def inner():\n        return h.read()\n"
        "    try:\n        return inner()\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "lambda over the handle",
        "def f(p):\n    h = open(p, encoding='utf-8')\n"
        "    try:\n        return (lambda: h.read())()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "nested def reads it",
        "def f(p):\n    h = open(p, encoding='utf-8')\n"
        "    def holder():\n        return h.read(64)\n"
        "    try:\n        return holder()\n    except OSError:\n        return 'caught'\n",
    ),
    # --- round 3 finding 3: the walrus -----------------------------------
    (
        "walrus handed over",
        "import json\ndef f(p):\n    try:\n"
        "        return json.load(h := open(p, encoding='utf-8'))\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "walrus then read",
        "def f(p):\n    try:\n        (h := open(p, encoding='utf-8'))\n"
        "        return h.read()\n    except OSError:\n        return 'caught'\n",
    ),
    # --- round 3 finding 4: a plain name beside a dotted one -------------
    (
        "plain name beside dotted",
        "class C:\n    def f(self, p):\n        try:\n"
        "            h = self.g = open(p, encoding='utf-8')\n"
        "            return self.g.read()\n"
        "        except OSError:\n            return 'caught'\n"
        "def f(p):\n    return C().f(p)\n",
    ),
    # --- the shapes the review measured HOLDING, kept so they cannot
    #     regress quietly ------------------------------------------------
    (
        "with-as read",
        "def f(p):\n    try:\n        with open(p, encoding='utf-8') as h:\n"
        "            return h.read()\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "positional hand-over",
        "import json\ndef f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        return json.load(h)\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "for-iteration",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        return [line for line in h]\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "plain alias",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        other = h\n        return other.read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "list storage",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        box = [h]\n        return box[0].read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "returned from helper",
        "def make(p):\n    return open(p, encoding='utf-8')\n"
        "def f(p):\n    try:\n        return make(p).read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "star-args",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        return (lambda x: x.read())(*[h])\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    (
        "annotated assignment",
        "import io\ndef f(p):\n    try:\n"
        "        h: io.TextIOWrapper = open(p, encoding='utf-8')\n"
        "        return h.read()\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "next on the handle",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8')\n"
        "        return next(h)\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "conditional binding",
        "def f(p):\n    try:\n        h = open(p, encoding='utf-8') if p else None\n"
        "        return h.read()\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "attribute target",
        "class C:\n    def o(self, p):\n        self.h = open(p, encoding='utf-8')\n"
        "    def u(self):\n        return self.h.read()\n"
        "def f(p):\n    c = C()\n    c.o(p)\n    try:\n        return c.u()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    # --- round 4: the swallower is not spelled `try` ---------------------
    #     #320's defect written without the word: `suppress(OSError)` eats
    #     the IO and lets the decode past, exactly as `except OSError`
    #     does. `except*` is a different NODE TYPE with the same meaning.
    #     A `with` whose __exit__ nobody has read is neither.
    (
        "suppress hides the IO only",
        "from contextlib import suppress\ndef f(p):\n    with suppress(OSError):\n"
        "        with open(p, encoding='utf-8') as h:\n            return h.read()\n"
        "    return 'caught'\n",
    ),
    (
        "except star",
        "def f(p):\n    got = 'caught'\n    try:\n"
        "        with open(p, encoding='utf-8') as h:\n            got = h.read()\n"
        "    except* OSError:\n        pass\n    return got\n",
    ),
    (
        "non-open context manager",
        "import tempfile\ndef f(p):\n    try:\n"
        "        with tempfile.TemporaryDirectory():\n"
        "            with open(p, encoding='utf-8') as h:\n                return h.read()\n"
        "    except OSError:\n        return 'caught'\n",
    ),
    # --- the compliant controls: these MUST be allowed to clear, or the
    #     safety property below is satisfied by a guard that flags
    #     everything ----------------------------------------------------
    (
        "with-as read, decode covered",
        "def f(p):\n    try:\n        with open(p, encoding='utf-8') as h:\n"
        "            return h.read()\n    except (OSError, UnicodeDecodeError):\n"
        "        return 'caught'\n",
    ),
    (
        "lenient errors",
        "def f(p):\n    try:\n"
        "        with open(p, encoding='utf-8', errors='replace') as h:\n"
        "            return h.read()\n    except OSError:\n        return 'caught'\n",
    ),
    (
        "lock file, never read",
        "import fcntl\ndef f(p):\n"
        "    with open(p, 'a+', encoding='utf-8') as fp:\n"
        "        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)\n"
        "        fp.seek(0)\n        fp.truncate()\n        fp.write('1')\n"
        "        fp.flush()\n    return 'caught'\n",
    ),
    (
        "write only",
        "def f(p):\n    with open(p, 'w', encoding='utf-8') as h:\n"
        "        h.write('x')\n    return 'caught'\n",
    ),
    (
        "suppress covers the decode",
        "from contextlib import suppress\ndef f(p):\n"
        "    with suppress(OSError, UnicodeDecodeError):\n"
        "        with open(p, encoding='utf-8') as h:\n            return h.read()\n"
        "    return 'caught'\n",
    ),
    (
        "except star covers the decode",
        "def f(p):\n    got = 'caught'\n    try:\n"
        "        with open(p, encoding='utf-8') as h:\n            got = h.read()\n"
        "    except* (OSError, UnicodeDecodeError):\n        pass\n    return got\n",
    ),
    # --- the compliant shapes the walk cannot PROVE compliant. They are
    #     controls too, and of the more interesting kind: they pin what
    #     option A actually COSTS, in rows, on a corpus where the answer
    #     is known. See UNPROVABLE.
    (
        "suppress through a name the walk cannot read",
        "from contextlib import suppress\nBOTH = (OSError, UnicodeDecodeError)\n"
        "def f(p):\n    with suppress(*BOTH):\n"
        "        with open(p, encoding='utf-8') as h:\n            return h.read()\n"
        "    return 'caught'\n",
    ),
    (
        "non-open context manager, decode covered",
        "import tempfile\ndef f(p):\n    try:\n"
        "        with tempfile.TemporaryDirectory():\n"
        "            with open(p, encoding='utf-8') as h:\n                return h.read()\n"
        "    except (OSError, UnicodeDecodeError):\n        return 'caught'\n",
    ),
]


#: The compliant shapes the walk answers ``undecided`` about, BY NAME.
#:
#: Option A's price list. Every other non-escaping row must be CLEARED,
#: or the walk has become a rubber stamp in the opposite direction and
#: its 84-row cleared inventory carries no information. These two may be
#: undecided instead - and MUST be, because a row that starts clearing is
#: the walk claiming to have read a ``__exit__`` it cannot see.
#:
#: The set is pinned rather than derived so that widening it is a diff
#: somebody reads. It is two rows, and each is a construct the walk
#: deliberately refuses to guess about: a ``suppress`` whose arguments
#: arrive through a name, and a context manager that is not ``open``.
UNPROVABLE: frozenset[str] = frozenset(
    {
        "suppress through a name the walk cannot read",
        "non-open context manager, decode covered",
    }
)


def _escapes(source: str, path: Path) -> bool:
    """Does a ``UnicodeDecodeError`` reach the caller of ``f``?

    The oracle. No AST, no re-implementation of the walk: the module is
    compiled and run against real bytes, and CPython decides.
    """
    namespace: dict[str, object] = {}
    exec(compile(source, "<shape>", "exec"), namespace)  # noqa: S102
    run = namespace["f"]
    assert callable(run)
    try:
        run(path)
    except UnicodeDecodeError:
        return True
    return False


def _cleared_without_complaint(source: str) -> bool:
    """Does the walk say every read in this module is fine?"""
    found = scan_source(source, where="shape.py", module="shape")
    return bool(found.clear) and not found.reported and not found.undecided


def _only_undecided(source: str) -> bool:
    """Does the walk decline to answer about a read in this module?

    ``undecided`` and no ``reported``: the walk found something it could
    not read rather than a defect it can name.
    """
    found = scan_source(source, where="shape.py", module="shape")
    return bool(found.undecided) and not found.reported


def _answers_for_io_only(source: str) -> bool:
    """Does this module contain a construct that swallows the IO and NOT
    the decode?

    THE PRECONDITION of the safety property, checked rather than trusted,
    for the reason the module docstring gives: a module with no handler
    at all lets the decode escape and is CORRECTLY cleared, so a row
    without one would fail the property for a reason that is not a
    defect.

    Built on the walk's own two predicates, so a row cannot satisfy this
    and be judged by a different vocabulary there. It reads ``suppress``
    as well as ``except``, because #320's defect has both spellings.
    """
    return any(
        answers_for_io(names) and not covers_the_decode(names) for names in _swallowed_names(source)
    )


def _swallowed_names(source: str) -> list[set[str]]:
    """What every swallowing construct in this module catches, by name.

    Both spellings, because #320's defect has both: an ``except`` ladder
    and a ``contextlib.suppress``. A construct whose names cannot be read
    contributes an empty set, which fails the IO-only test above and so
    sends the row to the skip list rather than into the property.
    """
    found: list[set[str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Try | ast.TryStar):
            found.append({name for clause in handler_clauses(node) for name in clause.names})
        elif isinstance(node, ast.Call) and leaf_name(node.func) == "suppress":
            found.append({got for arg in node.args if (got := leaf_name(arg)) is not None})
    return found


@pytest.fixture
def bad_file(tmp_path: Path) -> Path:
    target = tmp_path / "doc.json"
    target.write_bytes(BAD_BYTES)
    return target


class TestTheSafetyProperty:
    """The invariant, over every shape, against the interpreter."""

    @pytest.mark.parametrize(("name", "source"), SHAPES, ids=[row[0] for row in SHAPES])
    def test_a_shape_whose_decode_escapes_is_never_cleared(
        self, name: str, source: str, bad_file: Path
    ) -> None:
        """THE claim. Both halves come from one string, so a row cannot
        assert about a defect the interpreter does not commit."""
        if not _escapes(source, bad_file):
            pytest.skip(f"{name} does not let the decode escape; the control below covers it")
        if not _answers_for_io_only(source):
            pytest.skip(
                f"{name} carries no construct swallowing the IO alone, so the walk is "
                "right to clear it and the caller is the site that answers"
            )
        assert not _cleared_without_complaint(source), (
            f"{name}: a UnicodeDecodeError escapes this module's handler at run time and "
            "the walk cleared every read in it. That is the guard saying 'this site is "
            "fine' about a live escape, which is the skip direction CLAUDE.md guard-design "
            "rule 3 is about."
        )

    def test_enough_shapes_actually_escape(self, bad_file: Path) -> None:
        """The corpus control, and it is not optional.

        ``not cleared`` is also what a walk that flags everything returns,
        and ``skip`` is also what a corpus of shapes that never decode
        returns. Both failure modes are silent, so the count of shapes
        that genuinely escape is pinned. #344 round 2's M37 is the local
        precedent: five tests skipped themselves green.
        """
        escaping = [
            name
            for name, source in SHAPES
            if _escapes(source, bad_file) and _answers_for_io_only(source)
        ]
        assert len(escaping) == 30, (
            "the corpus stopped committing the defect it is built to commit, so the "
            f"property above is an assertion about nothing. Escaping: {escaping}"
        )

    def test_every_row_carries_the_handler_the_property_talks_about(self) -> None:
        """The precondition's own control.

        The property above SKIPS a row with no IO-only handler, and a
        skip is not a failure - #344 round 2's M37 is the local precedent
        for five tests skipping themselves green. So the population that
        skips is pinned at exactly the compliant controls, and a new row
        that quietly lands there fails here with its name.
        """
        without = [name for name, source in SHAPES if not _answers_for_io_only(source)]
        assert without == [
            "with-as read, decode covered",
            "lock file, never read",
            "write only",
            "suppress covers the decode",
            "except star covers the decode",
            "suppress through a name the walk cannot read",
            "non-open context manager, decode covered",
        ], (
            "a shape carries no construct swallowing the IO alone, so the safety "
            f"property skips it rather than testing it. Rows: {without}"
        )

    def test_the_compliant_shapes_are_still_cleared(self, bad_file: Path) -> None:
        """The other direction, and the reason the property above is not
        satisfied by a guard that flags everything.

        A shape whose decode does NOT escape must be CLEARED, or the walk
        has become a rubber stamp in the opposite direction and its
        cleared inventory means nothing. The two rows in
        :data:`UNPROVABLE` are the exception AND are held to the other
        half of the same rule: they must be ``undecided``, never
        ``reported`` and never ``clear``.
        """
        for name, source in SHAPES:
            if _escapes(source, bad_file):
                continue
            if name in UNPROVABLE:
                assert _only_undecided(source), (
                    f"{name} is pinned as a construct the walk cannot read. It must "
                    "answer 'undecided'. Clearing it is the walk claiming to have read "
                    "an __exit__ it cannot see; reporting it names a defect that is "
                    "not there."
                )
                continue
            assert _cleared_without_complaint(source), (
                f"{name}: no UnicodeDecodeError escapes at run time and the walk still "
                "refuses to clear it. Over-reporting is the safe direction, but a walk "
                "that reports everything has stopped carrying information."
            )


class TestTheOracleIsReal:
    """The oracle's own controls. An oracle nobody checked is a second
    guard with no guard on it."""

    def test_the_bytes_are_genuinely_undecodable(self) -> None:
        """An earlier draft of a sibling test built its bytes with
        ``str.encode``, which produces valid utf-8 and would have passed
        against every version of this walk."""
        with pytest.raises(UnicodeDecodeError):
            BAD_BYTES.decode("utf-8")
        assert json.loads(BAD_BYTES.decode("latin-1")) == {"naïve": 1}

    def test_the_oracle_says_no_when_the_decode_is_handled(self, bad_file: Path) -> None:
        """Without this, ``_escapes`` returning True for everything would
        make the property above trivially true."""
        handled = (
            "def f(p):\n    try:\n        return open(p, encoding='utf-8').read()\n"
            "    except (OSError, UnicodeDecodeError):\n        return 'caught'\n"
        )
        assert not _escapes(handled, bad_file)

    def test_the_oracle_says_yes_on_the_plainest_escape(self, bad_file: Path) -> None:
        bare = (
            "def f(p):\n    try:\n        return open(p, encoding='utf-8').read()\n"
            "    except OSError:\n        return 'caught'\n"
        )
        assert _escapes(bare, bad_file)

    def test_the_oracle_actually_runs_the_module(self, tmp_path: Path) -> None:
        """A file the module never opens would make every row ``caught``.
        This proves ``f`` reaches the path it is given."""
        target = tmp_path / "plain.txt"
        target.write_text("hello\n", encoding="utf-8")
        source = "def f(p):\n    return open(p, encoding='utf-8').read()\n"
        namespace: dict[str, object] = {}
        exec(compile(source, "<probe>", "exec"), namespace)  # noqa: S102
        run = namespace["f"]
        assert callable(run)
        assert run(target) == "hello\n"
