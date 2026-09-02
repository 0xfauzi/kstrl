"""The invariant #320's walk rests on, tested against the interpreter.

THE INVARIANT, in one sentence: an ``open`` is CLEARED only when exactly
one scope binds its handle to a plain local name, and every load of that
name lexically inside that scope is both OWNED by that scope and
classified into exactly one modelled bucket.

WHY THIS FILE EXISTS RATHER THAN A TENTH CASE. #344's review found eight
clearing shapes in round 2 and nine more in round 3, and the guard's
answer each time was another fixture. Seventeen fixtures is a collection
of cases, and a collection of cases is closed only over the shapes
somebody already thought of - CLAUDE.md's guard-design rule 1 names that
as a ledger of give-ups and says to prefer closed by construction. So
this file does not assert about shapes. It asserts the SAFETY PROPERTY
the whole guard means:

    a read whose UnicodeDecodeError escapes its handler at RUN TIME is
    never one the walk CLEARS.

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

import json
from pathlib import Path

import pytest

from tests.helpers.encodingwalk import scan_source

#: A byte no UTF-8 decoder accepts, inside a document that is otherwise
#: exactly what each reader expects. Shared with
#: ``tests/test_encoding_sites.py``'s reasoning: an unreadable-file
#: fixture would pass against every version of this walk.
BAD_BYTES = b'{"na\xefve": 1}\n'

#: ``(name, module source)``. Each defines ``f(p)`` and reads ``p``'s text
#: under a handler that answers for the IO and NOT for the decode, which
#: is #320's whole subject. The shapes are every one #344's two review
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
]


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
        escaping = [name for name, source in SHAPES if _escapes(source, bad_file)]
        assert len(escaping) >= 24, (
            "the corpus stopped committing the defect it is built to commit, so the "
            f"property above is an assertion about nothing. Escaping: {escaping}"
        )

    def test_the_compliant_shapes_are_still_cleared(self, bad_file: Path) -> None:
        """The other direction, and the reason the property above is not
        satisfied by a guard that flags everything.

        A shape whose decode does NOT escape must be CLEARED, or the walk
        has become a rubber stamp in the opposite direction and its
        cleared inventory means nothing.
        """
        for name, source in SHAPES:
            if _escapes(source, bad_file):
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
