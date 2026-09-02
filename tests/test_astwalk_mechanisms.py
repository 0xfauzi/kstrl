"""The two limits no signature in ``tests/helpers/astwalk`` can reach.

The package's claim is that the skip direction is made LOUD in places the
API will not let a caller leave out. Round 2 of #324 audited that claim
and found two places where no signature could carry it, so each is held by
a static guard over ``tests/`` instead. Both are here rather than beside
the feature they guard, because they are checks on how the SUITE uses the
helper rather than on what the helper computes, and a guard on usage fails
for a different reason and with a different message.

:func:`astwalk.resolved_calls` returns the seen half as NODES, for a guard
that has to read a call's arguments. Measured:
``resolved_calls(parse("x.Popen(argv)"), {"subprocess.Popen"})`` returns
``[]``, which is exactly what a module with no spawn in it returns, while
``calls_to`` reports ``1 x.Popen`` undecided.

:func:`astwalk.blind_spot` needs ``@pytest.mark.xfail(strict=True,
raises=AssertionError)`` on its CALLER, and a helper cannot apply a marker
to the function that calls it. #328 measured an open hole, a closed hole
and a resolver raising on entry all passing green without both keywords.

Twenty-one disclosure sites and two ``resolved_calls`` callers today, all
of them correct, every one of them hand-written. Correct-by-briefing is
the structure #324 exists to end.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers import astwalk

TARGET = "os.getpgid"


def calls(source: str, targets: frozenset[str] = frozenset({TARGET})) -> astwalk.Sites:
    return astwalk.calls_to(astwalk.parse(source), targets)


# --- the one function that hands back half an answer ----------------------


#: How ``tests/`` spells the function that returns the SEEN half alone.
#: Resolved rather than matched by name, so a module importing it as
#: ``from tests.helpers.astwalk import resolved_calls as rc`` is covered.
#:
#: BOTH spellings, and the second one is the point. The package re-exports
#: the name, so ``from tests.helpers.astwalk import resolved_calls``
#: resolves to the first row, while
#: ``from tests.helpers.astwalk.resolve import resolved_calls`` resolves to
#: the second. Measured while writing this guard: with the package row
#: alone the submodule spelling was a SILENT MISS, decided as somebody
#: else's function and never reported. That is #324's own defect class
#: inside #324's own fix, which is the third time this issue has recorded
#: one, and it is why the row below is a measurement rather than a guess.
#: ``test_it_sees_every_way_the_suite_can_spell_the_import`` is the pin.
RESOLVED_CALLS = frozenset(
    {
        "tests.helpers.astwalk.resolved_calls",
        "tests.helpers.astwalk.resolve.resolved_calls",
    }
)


def _asserts_the_undecided_half(node: ast.AST) -> bool:
    """A STRUCTURAL mention of the undecided half, not a textual one.

    ``found.undecided`` or an ``undecided=`` keyword. Deliberately not
    :func:`astwalk.spells`, which would also fire on the word in a
    docstring, and prose is not a check: a net that a comment can satisfy
    fails in the silent direction, which is this issue's whole subject.
    """
    if isinstance(node, ast.Attribute):
        return node.attr == "undecided"
    return isinstance(node, ast.keyword) and node.arg == "undecided"


def _half_answer_sites(sources: list[Path]) -> tuple[astwalk.Sites, list[str]]:
    """Where ``resolved_calls`` is called, and which modules take it alone."""
    found = astwalk.Sites()
    offenders: list[str] = []
    for source_file in sources:
        tree = astwalk.parsed(source_file)
        here = astwalk.calls_to(
            tree,
            RESOLVED_CALLS,
            where=astwalk.label(source_file, astwalk.REPO_ROOT),
            module=astwalk.module_name(source_file),
        )
        found = found + here
        if here.seen and not any(_asserts_the_undecided_half(n) for n in ast.walk(tree)):
            offenders.append(astwalk.label(source_file, astwalk.REPO_ROOT))
    return found, offenders


def _no_identifier_rows(sources: list[Path]) -> tuple[str, ...]:
    """Every call in the corpus whose callee holds no identifier at all.

    ``TABLE[key](...)`` and ``helper()(...)``. Undecidable against ANY
    target set, because there is nothing to compare, so every guard built
    on :func:`astwalk.calls_to` inherits the whole list whatever it is
    looking for. Built in the same walk order as ``calls_to``, so the two
    answers are comparable row for row.
    """
    rows: list[str] = []
    for source_file in sources:
        where = astwalk.label(source_file, astwalk.REPO_ROOT)
        for node in ast.walk(astwalk.parsed(source_file)):
            if isinstance(node, ast.Call) and astwalk.leaf_name(node.func) is None:
                rows.append(f"{where}:{node.lineno} {ast.unparse(node.func)}")
    return tuple(rows)


class TestResolvedCallsIsNotUsableOnItsOwn:
    """The exception to the rule the package claims, and its mechanism.

    ``astwalk/__init__.py`` says the skip direction is made loud in places
    the API will not let a caller leave out. :func:`astwalk.resolved_calls`
    is not one of them: it answers "which calls ARE the target" as NODES,
    because a guard reading a call's arguments needs the node, and there
    is no signature that hands back a node without also handing back a
    seen half a caller can use alone. Round 2 of #324 measured that
    exactly: ``resolved_calls(parse("x.Popen(argv)"), {"subprocess.Popen"})``
    returns ``[]``, which is indistinguishable from a module with no
    spawn in it, while ``calls_to`` reports ``1 x.Popen`` undecided.

    So the mechanism is a static guard rather than a signature, in the
    house style of the other AST guards in this suite: a module that calls
    it must name the other half somewhere. That is weaker than
    :func:`astwalk.assert_sites`, and the two tests at the bottom say by
    how much rather than leaving the reader to guess.
    """

    def test_the_seen_half_alone_reads_clean_where_the_partition_does_not(self) -> None:
        """The hole, executed. This is why the guard below exists."""
        source = "x.Popen(argv)\n"
        spawn = frozenset({"subprocess.Popen"})

        assert astwalk.resolved_calls(astwalk.parse(source), spawn) == []
        assert calls(source, spawn).undecided == ("1 x.Popen",)

    def test_it_returns_the_node_and_the_origin_it_resolved_to(self) -> None:
        """The seen half itself, which nothing else here covers: until
        #324 round 2 this function's only exercise was indirect, through
        ``tests/test_timeout_enforcement.py``."""
        got = astwalk.resolved_calls(astwalk.parse("import os\npgid = os.getpgid(1)\n"), {TARGET})

        assert [(node.lineno, origin) for node, origin in got] == [(2, TARGET)]

    def test_it_resolves_through_a_rebind_the_way_calls_to_does(self) -> None:
        """The two must not diverge: a caller picking the node form must
        not silently get a narrower resolver."""
        source = "import os\nlookup = os.getpgid\npgid = lookup(1)\n"
        got = astwalk.resolved_calls(astwalk.parse(source), {TARGET})

        assert [origin for _node, origin in got] == [TARGET]
        assert calls(source).seen == ("3 os.getpgid",)

    def test_every_caller_in_the_suite_names_the_other_half(self) -> None:
        """The guard. A module that takes the node form must say what the
        walk could not decide, somewhere in the same file.

        ``tests/test_timeout_enforcement.py`` is the only caller today: it
        unions the undecided ``Popen`` candidates into its spawn set by
        hand, and pins the package-wide undecided rows in
        ``test_the_walk_reports_what_it_could_not_decide``. A second
        caller that does neither fails here.
        """
        _found, offenders = _half_answer_sites(astwalk.test_sources())

        assert offenders == [], (
            f"{offenders} call astwalk.resolved_calls, which answers the SEEN half "
            "only, and never name the other half. An unresolvable callee is absent "
            "from that answer and absence reads as cleanliness, which is the defect "
            "#324 exists to end. Assert astwalk.calls_to(...).undecided over the same "
            "corpus, or use astwalk.assert_sites."
        )

    def test_the_guard_is_blind_to_nothing_a_target_set_could_reach(self) -> None:
        """Dogfooding: this guard is itself a walk, so it owes its own
        undecided half.

        Compared against the corpus's hard undecidable rather than pinned
        as a list. Over ``tests/`` there are 18 calls whose callee holds no
        identifier at all, most of them a call on the result of a call, and
        every ``calls_to`` guard over this corpus inherits all 18 whatever
        it is looking for. What must not appear is a NINETEENTH row: a
        callee spelled ``...resolved_calls`` that the walk could not
        resolve, which would be a module the guard above cannot see. The
        comparison does not churn, because a new call on a call lands on
        both sides of it, which a pinned list would not.
        """
        sources = astwalk.test_sources()
        found, _offenders = _half_answer_sites(sources)

        assert found.undecided == _no_identifier_rows(sources), (
            "the guard's undecided half is no longer exactly the calls that hold no "
            "identifier at all. The extra rows are callees spelled like this "
            "function that the walk could not resolve, so those modules go "
            "unchecked."
        )
        assert found.seen != (), "the guard resolved no call at all, so it measures nothing"

    def test_a_planted_caller_that_ignores_the_other_half_is_caught(self, tmp_path: Path) -> None:
        """The positive control. Without it the assertion above is also
        what a guard that resolved nothing returns."""
        planted = tmp_path / "greedy.py"
        planted.write_text(
            "from tests.helpers import astwalk\n\n\n"
            "def sites(tree):\n"
            '    return astwalk.resolved_calls(tree, {"subprocess.Popen"})\n',
            encoding="utf-8",
        )

        _found, offenders = _half_answer_sites([planted])

        assert offenders == ["greedy.py"]

    @pytest.mark.parametrize(
        "body",
        [
            "from tests.helpers import astwalk\nastwalk.resolved_calls(tree, T)\n",
            "from tests.helpers.astwalk import resolved_calls\nresolved_calls(tree, T)\n",
            "from tests.helpers.astwalk.resolve import resolved_calls\nresolved_calls(tree, T)\n",
            "import tests.helpers.astwalk as aw\naw.resolved_calls(tree, T)\n",
            "from tests.helpers.astwalk import resolved_calls as rc\nrc(tree, T)\n",
        ],
        ids=["package attribute", "package import", "submodule import", "module alias", "aliased"],
    )
    def test_it_sees_every_way_the_suite_can_spell_the_import(self, body: str) -> None:
        """The guard's own reach, measured on every spelling in the suite.

        The third row is the one that was found rather than reasoned
        about: with only the package's re-export in the target set, a
        ``from tests.helpers.astwalk.resolve import resolved_calls``
        resolved to a name the walk decided was somebody else's, and the
        module went unchecked with nothing reported. #324's defect class,
        inside #324's fix, caught by running it instead of reading it.
        """
        found = astwalk.calls_to(astwalk.parse(body), RESOLVED_CALLS)

        assert found.seen != (), f"this guard cannot see {body!r}"
        assert found.undecided == ()

    def test_a_planted_caller_that_names_the_other_half_passes(self, tmp_path: Path) -> None:
        """The negative control, so the guard is not simply flagging every
        caller."""
        planted = tmp_path / "careful.py"
        planted.write_text(
            "from tests.helpers import astwalk\n\n\n"
            "def sites(tree):\n"
            '    found = astwalk.calls_to(tree, {"subprocess.Popen"})\n'
            "    assert found.undecided == ()\n"
            '    return astwalk.resolved_calls(tree, {"subprocess.Popen"})\n',
            encoding="utf-8",
        )

        _found, offenders = _half_answer_sites([planted])

        assert offenders == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="per module, not per call")
def test_the_guard_is_per_module_not_per_target_set(tmp_path: Path) -> None:
    """The reach of the guard above, disclosed and pinned rather than
    implied.

    It asks whether the MODULE names the undecided half at all. A module
    that pins the undecided rows of one target set and then takes the
    seen half of a different one satisfies it. Closing that needs the walk
    to tie an assertion to a target set, which is dataflow this does not
    do. ``strict=True`` means the day somebody closes it, this row XPASSes
    and the paragraph above has to be edited in the same diff.
    """

    def flags(text: str) -> object:
        path = tmp_path / "mixed.py"
        path.write_text(text, encoding="utf-8")
        _found, offenders = _half_answer_sites([path])
        return offenders

    astwalk.blind_spot(
        flags,
        "from tests.helpers import astwalk\n\n\n"
        "def sites(tree):\n"
        '    other = astwalk.calls_to(tree, {"os.getpgid"})\n'
        "    assert other.undecided == ()\n"
        '    return astwalk.resolved_calls(tree, {"subprocess.Popen"})\n',
    )


# --- the other limit no signature reaches ---------------------------------


#: Both spellings, for the same reason ``RESOLVED_CALLS`` has both.
BLIND_SPOT = frozenset(
    {
        "tests.helpers.astwalk.blind_spot",
        "tests.helpers.astwalk.disclose.blind_spot",
    }
)

#: The call sites that are NOT disclosures: ``blind_spot``'s own
#: meta-tests, which call it to assert that it fails and that it passes.
#: Named one by one rather than skipped by a pattern, so a third
#: exemption is a diff somebody has to justify.
NOT_A_DISCLOSURE = frozenset(
    {
        (
            "tests/test_astwalk_nets.py",
            "TestBlindSpotHasAFailingState.test_it_fails_when_the_walk_still_cannot_see",
        ),
        (
            "tests/test_astwalk_nets.py",
            "TestBlindSpotHasAFailingState.test_it_passes_when_the_walk_can",
        ),
    }
)


def _marks_a_strict_xfail(scope: ast.AST) -> bool:
    """Does this function carry ``xfail(strict=True, raises=AssertionError)``?

    Read off the decorator by leaf name and keyword, so
    ``@pytest.mark.xfail(...)`` and a ``from pytest import mark`` spelling
    both count. A marker assembled elsewhere and applied by name is not
    read, and this answers False for it: unread is not the same as absent,
    so the site is reported and somebody spells the marker out.
    """
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    for decorator in scope.decorator_list:
        if not isinstance(decorator, ast.Call) or astwalk.leaf_name(decorator.func) != "xfail":
            continue
        keywords = {word.arg: word.value for word in decorator.keywords}
        strict = keywords.get("strict")
        raises = keywords.get("raises")
        if not isinstance(strict, ast.Constant) or strict.value is not True:
            continue
        if raises is not None and astwalk.leaf_name(raises) == "AssertionError":
            return True
    return False


def _offenders_in(source_file: Path) -> list[str]:
    """The disclosures in one module whose function is missing the marker."""
    rel = astwalk.label(source_file, astwalk.REPO_ROOT)
    module = astwalk.module_name(source_file)
    tree = astwalk.parsed(source_file)
    table = astwalk.bindings(tree, module=module)
    found: list[str] = []
    for scope, qualified in astwalk.scopes(tree):
        if (rel, qualified) in NOT_A_DISCLOSURE:
            continue
        discloses = any(
            isinstance(node, ast.Call) and table.resolve(node.func) in BLIND_SPOT
            for node in astwalk.own_nodes(scope)
        )
        if discloses and not _marks_a_strict_xfail(scope):
            found.append(f"{rel} {qualified}")
    return found


def _disclosure_sites() -> tuple[list[str], astwalk.Sites]:
    """Every ``blind_spot`` call in ``tests/``: the offenders, and the walk.

    The :class:`astwalk.Sites` is this guard's own answer about the
    corpus, so it owes an undecided half like any other walk here.
    """
    offenders: list[str] = []
    found = astwalk.Sites()
    for source_file in astwalk.test_sources():
        here = astwalk.calls_to(
            astwalk.parsed(source_file),
            BLIND_SPOT,
            where=astwalk.label(source_file, astwalk.REPO_ROOT),
            module=astwalk.module_name(source_file),
        )
        found = found + here
        if here.seen:
            offenders += _offenders_in(source_file)
    return offenders, found


def _planted_disclosure(tmp_path: Path, decorator: str) -> list[str]:
    """One planted module with ``decorator`` above a ``blind_spot`` call."""
    planted = tmp_path / "disclosed.py"
    planted.write_text(
        "import pytest\n\nfrom tests.helpers import astwalk\n\n\n"
        f"{decorator}\n"
        "def test_a_limit() -> None:\n"
        '    astwalk.blind_spot(lambda text: False, "x = 1\\n")\n',
        encoding="utf-8",
    )
    return _offenders_in(planted)


class TestEveryDisclosedLimitCanFail:
    """``blind_spot`` needs a marker it cannot apply for itself.

    Its body asserts that the walk DOES see the source; the marker says it
    is expected not to. Both halves are load-bearing and neither is in the
    helper: ``strict=True`` is what turns a widened walk's XPASS into a
    failure, so the disclosure has to be edited in the same diff, and
    ``raises=AssertionError`` is what makes a resolver that CRASHES on the
    input fail rather than xfail. #328 measured an open hole, a closed
    hole and a resolver raising on entry all passing green without both.

    Twenty-one call sites, and every one of them hand-writes the marker.
    That is correct-by-briefing, which is the structure #324 exists to
    end: two of its eleven originals were holed a second time after being
    fixed once, and one was written by an author explicitly briefed on the
    pattern. So this is the check, in the same shape as
    ``TestResolvedCallsIsNotUsableOnItsOwn`` above.
    """

    def test_no_disclosure_is_missing_either_half_of_the_marker(self) -> None:
        offenders, _found = _disclosure_sites()

        assert offenders == [], (
            f"{offenders} call astwalk.blind_spot without "
            "@pytest.mark.xfail(strict=True, raises=AssertionError). Without "
            "strict=True a hole that closes stays green and the disclosure rots; "
            "without raises=AssertionError a walk that crashes on the input is "
            "indistinguishable from one that cannot see it."
        )

    def test_the_walk_finds_the_disclosures_it_is_checking(self) -> None:
        """Anti-vacuity: an empty offender list is also what a walk that
        resolved nothing returns.

        A FLOOR, not a pin. Adding a disclosure is the healthy direction
        and should not fail a guard in another file; a walk that stopped
        resolving drops to zero, which is what this catches.
        """
        _offenders, found = _disclosure_sites()

        assert len(found.seen) >= 20, (
            f"only {len(found.seen)} blind_spot calls resolved, so this guard is "
            "measuring almost nothing. There were 21 when it was written."
        )

    def test_it_is_blind_to_nothing_a_target_set_could_reach(self) -> None:
        """Its own undecided half, compared against the corpus's hard
        undecidable rather than pinned, for the reason given above it."""
        sources = astwalk.test_sources()
        _offenders, found = _disclosure_sites()

        assert found.undecided == _no_identifier_rows(sources)

    def test_a_marker_missing_strict_is_caught(self, tmp_path: Path) -> None:
        assert _planted_disclosure(tmp_path, "@pytest.mark.xfail(raises=AssertionError)")

    def test_a_marker_missing_raises_is_caught(self, tmp_path: Path) -> None:
        assert _planted_disclosure(tmp_path, "@pytest.mark.xfail(strict=True)")

    def test_a_marker_with_strict_false_is_caught(self, tmp_path: Path) -> None:
        assert _planted_disclosure(
            tmp_path, "@pytest.mark.xfail(strict=False, raises=AssertionError)"
        )

    def test_no_marker_at_all_is_caught(self, tmp_path: Path) -> None:
        assert _planted_disclosure(tmp_path, "")

    def test_a_marker_built_elsewhere_is_caught(self, tmp_path: Path) -> None:
        """The shape ``_marks_a_strict_xfail`` cannot read, and it fails
        CLOSED: the offender row is where somebody spells it out."""
        assert _planted_disclosure(tmp_path, "@_SHARED_MARKER")

    def test_both_halves_present_passes(self, tmp_path: Path) -> None:
        """The negative control, so the guard is not simply flagging every
        call site."""
        assert not _planted_disclosure(
            tmp_path, "@pytest.mark.xfail(strict=True, raises=AssertionError)"
        )
