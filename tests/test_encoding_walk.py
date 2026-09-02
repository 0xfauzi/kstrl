"""What the #320 encoding walk can and cannot see, one fixture per claim.

``tests/helpers/encodingwalk.py`` holds the walk and
``tests/test_encoding_readers.py`` its four package inventories.
Those inventories are assertions about ``kstrl/``, and an
inventory whose walk has stopped looking reports a clean package - which
is #324's subject and the reason this file exists. Every claim the walk's
docstring makes is planted here as source and watched being answered.

The fixtures are SOURCE STRINGS rather than files, so planting one cannot
leave an importable module behind in ``kstrl/``, and so both halves of
each shape are built from one row of one table. #318 round 3 records what
two hand-written lists cost: they drifted, the unguarded half covered 3 of
6 forms, and nothing made that visible.
"""

from __future__ import annotations

import ast

import pytest

from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    all_nodes,
    assert_census,
    blind_spot,
    census,
    label,
    leaf_name,
    package_sources,
    parsed,
    spells,
)
from tests.helpers.encodingwalk import (
    HANDLE_READS,
    HANDLE_SAFE,
    Scan,
    scan_source,
    spells_a_token,
)
from tests.test_encoding_readers import EXPECTED_READ_SPELLINGS


def _scan(source: str) -> Scan:
    return scan_source(source, where="probe.py", module="probe")


def _reported(source: str) -> tuple[str, ...]:
    return _scan(source).reported


def _cleared(source: str) -> tuple[str, ...]:
    return _scan(source).clear


# --------------------------------------------------------------------------
# The shapes a read takes, both halves built from one row.
# --------------------------------------------------------------------------

#: ``(name, the read expression, the statement that consumes it)``.
#: The consumer matters for the ``open`` forms: ``open`` does not decode,
#: the read through the handle does, so a form whose consumer is missing
#: would silently exercise only the encoding rule.
READ_FORMS: list[tuple[str, str, str]] = [
    ("read_text", "p.read_text({kw})", ""),
    ("read_text_on_expression", "(root / 'a.md').read_text({kw})", ""),
    ("path_open", "p.open({kw})", "        h.read()\n"),
    ("builtin_open", "open(p, {kw})", "        h.read()\n"),
    ("builtin_open_mode_kw", "open(p, mode='r', {kw})", "        h.readlines()\n"),
    ("open_read_plus", "open(p, 'r+', {kw})", "        h.read()\n"),
    ("open_append_plus", "open(p, 'a+', {kw})", "        h.read(64)\n"),
    ("open_handed_over", "open(p, {kw})", "        json.load(h)\n"),
    ("open_iterated", "open(p, {kw})", "        for line in h:\n            pass\n"),
]

#: The keyword text for a read that satisfies the encoding rule, and for
#: one that does not. Both halves of every form are built from these, so
#: a form cannot be covered in one direction only.
GOOD_KW = "encoding='utf-8'"
BAD_KW = ""


def _module(read: str, consumer: str, kw: str, handler: str) -> str:
    """One module: the read inside a ``try`` whose handler is *handler*."""
    return (
        "import json\n"
        "def f(p, root):\n"
        "    try:\n"
        f"        h = {read.format(kw=kw)}\n"
        f"{consumer}"
        f"    except {handler}:\n"
        "        return None\n"
    )


class TestTheWalkSeesEveryShapeOfRead:
    """Nine shapes, each planted twice. The compliant half must be
    CLEARED and the defective half must be REPORTED, because a walk that
    reports nothing and a walk that sees nothing are the same answer when
    only one direction is checked."""

    @pytest.mark.parametrize(("name", "read", "consumer"), READ_FORMS)
    def test_a_compliant_read_is_cleared(self, name: str, read: str, consumer: str) -> None:
        found = _scan(_module(read, consumer, GOOD_KW, "(OSError, UnicodeDecodeError)"))
        assert found.clear, f"{name}: a compliant read was not seen at all"
        assert not found.reported, f"{name}: a compliant read was reported: {found.reported}"
        assert not found.undecided, f"{name}: {found.undecided}"

    @pytest.mark.parametrize(("name", "read", "consumer"), READ_FORMS)
    def test_a_read_that_names_no_encoding_is_reported(
        self, name: str, read: str, consumer: str
    ) -> None:
        found = _reported(_module(read, consumer, BAD_KW, "(OSError, UnicodeDecodeError)"))
        assert any("names no encoding" in row for row in found), f"{name}: not reported: {found}"

    @pytest.mark.parametrize(("name", "read", "consumer"), READ_FORMS)
    def test_a_read_whose_handler_misses_the_decode_is_reported(
        self, name: str, read: str, consumer: str
    ) -> None:
        found = _reported(_module(read, consumer, GOOD_KW, "OSError"))
        assert any("UnicodeDecodeError" in row for row in found), f"{name}: not reported: {found}"


class TestTheEncodingRule:
    """Rule E: the read must name utf-8, and the walk must be able to
    prove it rather than assume it."""

    def test_a_non_utf8_encoding_is_reported(self) -> None:
        found = _reported("def f(p):\n    return p.read_text(encoding='latin-1')\n")
        assert found and "latin-1" in found[0], found

    def test_an_encoding_it_cannot_fold_is_reported_not_cleared(self) -> None:
        """The clearing direction is the dangerous one. ``encoding=DEFAULT``
        may well be utf-8; this walk cannot see that, so it says so."""
        found = _reported("def f(p, DEFAULT):\n    return p.read_text(encoding=DEFAULT)\n")
        assert found and "cannot fold" in found[0], found

    def test_a_folded_encoding_expression_is_cleared(self) -> None:
        """``folded_str`` reaches implicit concatenation and f-strings, so
        a spelling built out of pieces is still proven."""
        assert _cleared("def f(p):\n    return p.read_text(encoding='utf' '-8')\n")

    @pytest.mark.parametrize(
        "spelling",
        # The last three are here because #344's review measured the
        # hand-written set this predicate replaced: it held the first
        # four, so reverting to it left the suite green. A control has
        # to be able to fail for the reason it names.
        ["utf-8", "utf8", "UTF-8", "utf_8", "UTF_8", "cp65001", "utf"],
    )
    def test_every_accepted_spelling_of_utf8_clears(self, spelling: str) -> None:
        assert _cleared(f"def f(p):\n    return p.read_text(encoding='{spelling}')\n")


class TestTheDecodeRule:
    """Rule V: a handler that answers for the IO must answer for the
    decode too, and the walk must not clear a site it cannot read."""

    @pytest.mark.parametrize(
        "handler", ["UnicodeDecodeError", "ValueError", "Exception", "(OSError, ValueError)"]
    )
    def test_a_handler_that_covers_the_decode_clears(self, handler: str) -> None:
        source = (
            "def f(p):\n    try:\n        return p.read_text(encoding='utf-8')\n"
            f"    except {handler}:\n        return None\n"
        )
        assert _cleared(source) and not _reported(source)

    @pytest.mark.parametrize(
        "handler",
        # ``TimeoutError`` and ``BlockingIOError`` are two of the
        # eleven OSError subclasses the hand-written set this
        # predicate replaced did not hold. Without them, reverting to
        # that set left the suite green while a strict utf-8 read
        # under ``except TimeoutError`` went from reported to CLEARED.
        [
            "OSError",
            "FileNotFoundError",
            "PermissionError",
            "TimeoutError",
            "BlockingIOError",
            "(OSError, KeyError)",
        ],
    )
    def test_a_handler_that_answers_only_for_the_io_is_reported(self, handler: str) -> None:
        source = (
            "def f(p):\n    try:\n        return p.read_text(encoding='utf-8')\n"
            f"    except {handler}:\n        return None\n"
        )
        assert _reported(source), handler

    def test_a_read_with_no_handler_at_all_is_cleared(self) -> None:
        """Not an offender, and saying so is a decision rather than an
        oversight: a read nothing guards claims nothing, so nothing
        escapes a promise it did not make. Twelve of ``kstrl/``'s 52 are
        this shape."""
        assert _cleared("def f(p):\n    return p.read_text(encoding='utf-8')\n")

    def test_a_handler_this_walk_cannot_name_is_reported(self) -> None:
        """``Clause.decided`` is False, and an empty name set reads
        exactly like "catches nothing", which is the worst possible
        misreading of "catches something I could not see"."""
        source = (
            "def f(p):\n    try:\n        return p.read_text(encoding='utf-8')\n"
            "    except shim.Whatever:\n        return None\n"
        )
        found = _reported(source)
        assert found and "cannot name" in found[0], found

    def test_a_handler_named_through_an_import_alias_is_resolved(self) -> None:
        """A name is not an identity. ``from json import JSONDecodeError as
        ValueError`` reads as compliant to anything matching on spelling;
        the resolver places it and this stays reported."""
        source = (
            "from json import JSONDecodeError as ValueError\n"
            "def f(p):\n    try:\n        return p.read_text(encoding='utf-8')\n"
            "    except (OSError, ValueError):\n        return None\n"
        )
        assert _reported(source), "a rebound ValueError cleared the site"

    def test_the_first_handler_that_answers_decides_and_not_the_innermost(self) -> None:
        """Outward from the innermost. An inner ``try`` that says nothing
        about either fault does not stop the decode reaching an outer
        ``except OSError``, so the outer one is the escape."""
        source = (
            "import json\n"
            "def f(p):\n"
            "    try:\n"
            "        try:\n"
            "            return json.loads(p.read_text(encoding='utf-8'))\n"
            "        except json.JSONDecodeError:\n"
            "            return None\n"
            "    except OSError:\n"
            "        return None\n"
        )
        assert _reported(source)

    def test_an_inner_handler_that_covers_the_decode_clears_the_outer_one(self) -> None:
        """The other direction of the same rule: the decode never reaches
        the outer ``except OSError``, so demanding a clause there would
        be demanding a handler that can never fire."""
        source = (
            "def f(p):\n"
            "    try:\n"
            "        try:\n"
            "            return p.read_text(encoding='utf-8')\n"
            "        except UnicodeDecodeError:\n"
            "            return None\n"
            "    except OSError:\n"
            "        return None\n"
        )
        assert _cleared(source) and not _reported(source)

    def test_a_read_in_a_def_written_inside_a_try_is_not_credited_to_it(self) -> None:
        """``own_nodes`` stops at a nested function, so a helper DEFINED
        in a ``try`` body and CALLED elsewhere is not credited to a
        handler that will never see its exception. Here that means the
        read has no guard at all, which is not an offender."""
        source = (
            "def outer(p):\n"
            "    try:\n"
            "        def inner():\n"
            "            return p.read_text(encoding='utf-8')\n"
            "        return inner\n"
            "    except OSError:\n"
            "        return None\n"
        )
        assert not _reported(source)


class TestTheErrorsKeyword:
    """A read naming a lenient ``errors=`` cannot raise
    ``UnicodeDecodeError`` at all, measured on CPython 3.12 against a real
    latin-1 byte. Six of ``kstrl/``'s 52 reads are that shape, and
    demanding a decode clause from them would be demanding a handler that
    can never fire."""

    @pytest.mark.parametrize("errors", ["replace", "ignore", "surrogateescape"])
    def test_a_lenient_read_under_an_os_only_handler_is_cleared(self, errors: str) -> None:
        source = (
            "def f(p):\n    try:\n"
            f"        return p.read_text(encoding='utf-8', errors='{errors}')\n"
            "    except OSError:\n        return None\n"
        )
        assert _cleared(source) and not _reported(source)

    @pytest.mark.parametrize("errors", ["strict", "xmlcharrefreplace", "namereplace"])
    def test_an_errors_that_does_not_spare_the_decode_is_still_strict(self, errors: str) -> None:
        """``strict`` obviously. The other two are ENCODE-only handlers:
        on a decode CPython raises ``TypeError``, so a read naming one
        does not survive a bad byte either.

        They are here because the hand-written set this predicate
        replaced listed ``xmlcharrefreplace`` as lenient, and #344's
        review measured that reverting to it left the suite green while
        the site went from reported to CLEARED on a value that cannot be
        used at all.
        """
        source = (
            "def f(p):\n    try:\n"
            f"        return p.read_text(encoding='utf-8', errors='{errors}')\n"
            "    except OSError:\n        return None\n"
        )
        assert _reported(source)

    def test_an_errors_it_cannot_fold_is_treated_as_strict(self) -> None:
        """The reporting direction. A site is cleared on a lenient value
        only when the value was actually read."""
        source = (
            "def f(p, how):\n    try:\n"
            "        return p.read_text(encoding='utf-8', errors=how)\n"
            "    except OSError:\n        return None\n"
        )
        assert _reported(source)


class TestWhatIsDecidedOutAndWhy:
    """Every place the walk says "not a candidate", with the reason."""

    def test_a_binary_open_is_not_a_text_read(self) -> None:
        source = (
            "def f(p):\n    try:\n        h = open(p, 'rb')\n        return h.read()\n"
            "    except OSError:\n        return None\n"
        )
        assert not _reported(source) and not _cleared(source)

    def test_a_write_only_open_answers_for_the_encoding_but_not_the_decode(
        self,
    ) -> None:
        """A write-mode text stream ENCODES, so the encoding rule reaches
        it; it never decodes, so the handler rule does not.

        Measured before this was widened: ``path.open("a")`` under
        ``LC_ALL=C PYTHONUTF8=0`` raises ``UnicodeEncodeError`` on one
        accented character, and ``kstrl/agents/logging.py`` was appending
        raw agent output through exactly that call. Both variables: a C
        locale on its own turns PEP 540 UTF-8 mode on and the write then
        succeeds.
        """
        good = "def f(p):\n    with open(p, 'w', encoding='utf-8') as h:\n        h.write('x')\n"
        assert _cleared(good) and not _reported(good)
        bad = "def f(p):\n    with open(p, 'w') as h:\n        h.write('x')\n"
        assert _reported(bad) and not _cleared(bad)

    def test_a_write_only_open_is_not_charged_the_decode_rule(self) -> None:
        """Under a fail-closed ``except OSError`` with nothing covering
        ``UnicodeDecodeError``: still cleared, because no byte is decoded
        here and a handler that can never fire is not a fix."""
        source = (
            "def f(p):\n    try:\n"
            "        with open(p, 'w', encoding='utf-8') as h:\n            h.write('x')\n"
            "    except OSError:\n        return None\n"
        )
        assert _cleared(source) and not _reported(source)

    def test_a_mode_it_cannot_fold_is_undecided_rather_than_skipped(self) -> None:
        """The whole subject of #324 in one row: a walk that cannot read
        the mode must not conclude "binary, move on"."""
        found = _scan("def f(p, mode):\n    return open(p, mode, encoding='utf-8')\n")
        assert found.undecided and "does not fold" in found.undecided[0], found

    def test_a_callee_with_no_identifier_is_undecided(self) -> None:
        found = _scan("def f(p, TABLE):\n    open(p)\n    return TABLE['read'](p)\n")
        assert any("TABLE['read']" in row for row in found.undecided), found

    def test_somebody_elses_open_is_decided_out_through_the_resolver(self) -> None:
        """``os.open`` returns an integer file descriptor and decodes
        nothing. Resolved, not matched on spelling."""
        source = "import os\ndef f(p):\n    return os.open(p, os.O_RDONLY)\n"
        assert not _reported(source) and not _cleared(source)

    def test_a_guessed_origin_does_not_decide_anything(self) -> None:
        """The one step that CLEARS on a resolution refuses a guess. The
        bare-name over-match answers for any receiver in the module, so
        four innocuous lines would otherwise make every ``x.open(p)`` in
        the file vanish. #324 round 3 measured that against three guards."""
        source = (
            "import shutil\n"
            "class _M:\n    open = shutil.copy\n"
            "def f(mod, p):\n    try:\n        h = mod.open(p)\n        return h.read()\n"
            "    except OSError:\n        return None\n"
        )
        found = _scan(source)
        assert not found.clear, "a class-body binding cleared an unrelated open()"
        # It lands UNDECIDED rather than reported, and for a second
        # reason worth naming: ``mod.open(p)`` puts ``p`` in the slot
        # ``Path.open`` uses for the mode, so the mode does not fold
        # either. Both roads lead away from clearing it, which is the
        # only property this test is about.
        assert found.undecided or found.reported, found


class TestTheTwoLayersTogether:
    """Layer 1 is not decoration: it is the half that answers for what
    layer 2 provably cannot resolve."""

    ALIASED_READ = (
        "def f(p):\n"
        "    reader = p.read_text\n"
        "    try:\n        return reader()\n"
        "    except OSError:\n        return None\n"
    )

    def test_layer_two_cannot_see_a_read_through_an_aliased_bound_method(self) -> None:
        """Measured, not assumed. ``p`` is a local the AST cannot type, so
        ``reader`` resolves to nothing and its leaf is ``reader``."""
        assert not _reported(self.ALIASED_READ)
        assert not _cleared(self.ALIASED_READ)

    def test_layer_one_does_see_it(self) -> None:
        hits = sum(1 for node in all_nodes(ast.parse(self.ALIASED_READ)) if spells_a_token(node))
        assert hits == 1, "layer 1 missed the alias layer 2 cannot follow"

    def test_layer_one_sees_a_rebound_builtin_open(self) -> None:
        source = "_o = open\ndef f(p):\n    return _o(p)\n"
        hits = sum(1 for node in all_nodes(ast.parse(source)) if spells_a_token(node))
        assert hits == 1, "layer 1 missed a rebound open()"

    def test_the_two_layers_agree_about_which_modules_hold_a_read(self) -> None:
        """Layer 2's gate IS layer 1's net, so the population cannot
        drift. A module layer 1 counts must be one layer 2 walks."""
        walked = census(package_sources(), spells_a_token)
        assert set(walked) == set(EXPECTED_READ_SPELLINGS)

    def test_a_module_that_names_neither_token_is_not_walked(self) -> None:
        """The gate's cost, stated. A module that obtains no file text is
        not walked, and layer 1 is what answers for it."""
        assert _scan("import json\ndef f(x):\n    return json.dumps(x)\n") == Scan()


class TestTheReadBytesExclusion:
    """``read_bytes`` then ``bytes.decode`` is deliberately out of scope,
    and the exclusion is pinned so it cannot silently grow.

    All five sites guard the decode separately today, which is the shape
    ``config.load_toml_document`` argues for: do the I/O outside the
    guard so no widening can reach an ``OSError``. A sixth appearing is a
    reason to look, so this fails rather than absorbing it.
    """

    EXPECTED_READ_BYTES: dict[str, int] = {
        "breaker.py": 1,
        "config.py": 1,
        "inbox.py": 1,
        "safemode.py": 1,
        "verify.py": 1,
    }

    def test_the_read_bytes_sites_are_the_five_measured(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells("read_bytes"),
            expected=self.EXPECTED_READ_BYTES,
            control="p.read_bytes()",
            message="the read_bytes sites this walk excludes moved.",
        )

    #: The shape the exclusion says never happens, as source. It is the
    #: control for the walk below: neutering the matcher to ``set()``
    #: left that walk green at 69 passed, which is the ``assert hits ==
    #: []`` shape #344's review named.
    VIOLATION = (
        "def f(p):\n    try:\n        raw = p.read_bytes()\n"
        "        return raw.decode('utf-8')\n"
        "    except OSError:\n        return None\n"
    )

    @staticmethod
    def _decodes_under_its_own_read(tree: ast.Module) -> list[int]:
        """The lines where a ``read_bytes`` and a ``decode`` share one
        ``try`` body."""
        return [
            node.lineno
            for node in all_nodes(tree)
            if isinstance(node, ast.Try)
            for names in [{leaf_name(n.func) for n in all_nodes(node) if isinstance(n, ast.Call)}]
            if "read_bytes" in names and "decode" in names
        ]

    def test_the_matcher_fires_on_the_shape_it_is_looking_for(self) -> None:
        """The control. Without it the walk below is also what a matcher
        that stopped matching returns."""
        assert self._decodes_under_its_own_read(ast.parse(self.VIOLATION)) == [2]

    def test_every_read_bytes_site_decodes_under_its_own_guard(self) -> None:
        """The claim the exclusion rests on, checked rather than asserted:
        no ``bytes.decode`` in ``kstrl/`` sits in the same ``try`` body as
        the ``read_bytes`` that produced its bytes."""
        corpus = package_sources()
        assert corpus, "the corpus derivation is wrong, so this asserts nothing"
        for source in corpus:
            found = self._decodes_under_its_own_read(parsed(source))
            assert not found, (
                f"{label(source)}:{found[0]} reads bytes and decodes them under one "
                "handler, which is the shape tests/helpers/encodingwalk.py excludes "
                "on the grounds that it never happens. Widen that walk or fix the site."
            )


class TestAnOpenTheWalkCannotFollowIsNotCleared:
    """#344 F1. The handler rule is charged at the DECODE, and for an
    ``open`` the decode is at the read through the handle. So an ``open``
    whose handle this walk cannot follow to a name is a site whose decode
    it cannot find, and the module's own rule then applies: an
    undecidable REPORTS, it does not clear.

    Every row here was measured CLEARED before the fix, with both faults
    ``None``, which is the skip direction this guard exists to close.
    """

    HANDLER = "    except OSError:\n        return None\n"

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("chained builtin", "        return open(p, encoding='utf-8').read()\n"),
            ("chained Path.open", "        return p.open(encoding='utf-8').read()\n"),
            ("handed over inline", "        return json.load(open(p, encoding='utf-8'))\n"),
            ("two targets", "        h = g = open(p, encoding='utf-8')\n        return h.read()\n"),
            ("bare statement", "        open(p, encoding='utf-8')\n"),
        ],
    )
    def test_an_unfollowable_handle_is_undecided(self, name: str, body: str) -> None:
        source = f"import json\ndef f(p):\n    try:\n{body}{self.HANDLER}"
        found = _scan(source)
        assert not found.clear, f"{name} was CLEARED"
        assert found.undecided and "never bound to a name" in found.undecided[0], found

    def test_a_handle_stored_on_an_attribute_is_undecided(self) -> None:
        """``self.h = open(...)`` binds a DOTTED target. The walk matches
        uses by ``ast.Name.id``, so tracking it under the key ``"self.h"``
        would track a name no use can ever equal - which is what it did,
        while ``self.h.read()`` went unseen and the ``open`` cleared."""
        source = (
            "class C:\n"
            "    def open_it(self, p):\n        self.h = open(p, encoding='utf-8')\n"
            "    def use(self):\n        try:\n            return self.h.read()\n"
            "        except OSError:\n            return None\n"
        )
        found = _scan(source)
        assert not found.clear and found.undecided, found

    def test_a_handle_returned_out_of_its_function_is_undecided(self) -> None:
        """The limit that used to be a disclosure. It is now a row."""
        source = (
            "def make(p):\n    return open(p, encoding='utf-8')\n"
            "def use(p):\n    try:\n        return make(p).read()\n"
            "    except OSError:\n        return None\n"
        )
        found = _scan(source)
        assert not found.clear and found.undecided, found

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("keyword hand-over", "        return json.load(fp=h)\n"),
            ("comprehension", "        return [x for x in h]\n"),
        ],
    )
    def test_a_modelled_use_of_a_bound_handle_is_charged_the_decode_rule(
        self, name: str, body: str
    ) -> None:
        """The other half of F1: binding the handle is not enough, the USE
        has to be one the walk models. These two decode, and both cleared
        before the widening."""
        source = (
            "import json\ndef f(p):\n    try:\n"
            f"        h = open(p, encoding='utf-8')\n{body}{self.HANDLER}"
        )
        assert _reported(source), name

    def test_a_lock_file_that_never_reads_still_clears(self) -> None:
        """The cost of the rule, stated. Six ``fcntl`` lock files bind a
        handle and touch it only through members that decode nothing, and
        demanding a ``UnicodeDecodeError`` clause from them would be
        demanding a handler that can never fire."""
        source = (
            "import fcntl\ndef f(p):\n"
            "    with open(p, 'a+', encoding='utf-8') as fp:\n"
            "        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)\n"
            "        fp.seek(0)\n        fp.truncate()\n        fp.write('1')\n"
            "        fp.flush()\n"
        )
        found = _scan(source)
        assert found.clear and not found.reported and not found.undecided, found

    def test_a_use_of_the_handle_the_walk_does_not_model_is_undecided(self) -> None:
        """The third bucket, and there is no fourth. ``HANDLE_SAFE`` is
        derived from ``io.TextIOWrapper``, so a member that is on neither
        set is a use nobody has classified rather than one presumed
        harmless."""
        source = (
            "def f(p):\n    h = open(p, encoding='utf-8')\n    return h.readinto(bytearray())\n"
        )
        found = _scan(source)
        assert found.undecided and "does not model" in found.undecided[0], found

    def test_the_safe_members_come_from_the_type_and_not_from_a_list(self) -> None:
        """The derivation, checked. A hand-written list is the clearing
        mechanism #344 F2 is about, and this one would be 20 rows long."""
        assert "fileno" in HANDLE_SAFE and "write" in HANDLE_SAFE
        assert not (HANDLE_SAFE & HANDLE_READS)
        assert "readinto" not in HANDLE_SAFE, "not a TextIOWrapper member"


class TestTheDisclosedLimits:
    """Each limit the walk's docstring names, planted and watched failing.

    ``blind_spot`` asserts the walk DOES see the source; the strict xfail
    says it is expected not to. The row passes only while the limit
    holds, and the day somebody widens the walk it XPASSes, which
    ``strict=True`` makes a failure and the disclosure has to be edited in
    the same diff. #328 measured a plain non-strict xfail passing green
    for an open hole, a closed hole and a resolver crashing on entry
    alike, which is why ``raises=`` is here too.

    Two limits that used to live here are gone, because #344 F1 turned
    them into rows: a handle returned out of its function and a handle
    stored on ``self`` are now ``undecided``, tested in the class above.
    What is left is the one layer 1 answers for.
    """

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_read_through_an_aliased_bound_method(self) -> None:
        blind_spot(
            lambda source: bool(_reported(source)),
            "def f(p):\n"
            "    reader = p.read_text\n"
            "    try:\n        return reader()\n"
            "    except OSError:\n        return None\n",
        )


class TestTheGuardIsPointedAtSomething:
    """The corpus half, which no fixture can speak for: a control fires
    on a string whether the package holds 128 modules or none."""

    def test_the_package_is_not_empty(self) -> None:
        assert len(package_sources()) > 100, "the corpus derivation is wrong"

    def test_the_package_root_is_the_real_one(self) -> None:
        assert (KSTRL_PACKAGE / "config.py").is_file()
