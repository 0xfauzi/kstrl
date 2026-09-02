"""Positive controls for the check-name guard: source it MUST read.

The guard lives in ``tests/test_check_name_enrolment.py``. Its census
answers with a set of names and a ledger of the sites it could not read,
and BOTH of those are also what a switched-off matcher produces: a
producer shape nobody enumerated yields no name and no ledger row, so it
is silence rather than a red test.

That is not hypothetical. ``BLIND_SITES`` inventories the walk's own
resolution FAILURES, which is a strictly smaller set than the producer
shapes that exist, and its precedent ``EXPECTED_JOURNAL_PATH_SITES`` is
closed by construction where this is not: that one enumerates every place
a resource is obtained, so a new way of obtaining it lands in the list,
while a new way of writing a check name lands nowhere. #339 review found
``pipeline._fail_pr_flow`` sitting in exactly that gap - a positional
call to the recording funnel, no ``signatures=``, and no colon in
``"pr"`` for the other layer's net to catch either. Mutating it to
``"bogus_flow"`` left 4737 tests green, and neither the census nor the
ledger moved.

So this file feeds the two matchers a snippet per shape and pins what
they SAY, including the shapes they say nothing about. A matcher branch
deleted here is a red test rather than a quieter walk.

Separate from the guard for the reason ``tests/test_event_name_shapes.py``
gives about its own sibling on main: that file walks the package and pins
what is there, this one feeds the matchers snippets and pins what they
answer.

Every shape here was measured against the matchers under the
``__pycache__`` discipline: CPython invalidates cached bytecode on
``(mtime_seconds, size)``, so two same-size edits inside one second reuse
stale bytecode and hand back a false pass.
"""

from __future__ import annotations

import ast

from tests.helpers.astfold import parameter_index
from tests.test_check_name_enrolment import _producer_sites
from tests.test_signature_spellings import signature_heads


def read(source: str, own: dict[str, str] | None = None) -> list[tuple[str, list[str]]]:
    """``(expression, names)`` for every producer site in one snippet.

    A list rather than a set: a site the matchers DETECT and cannot read
    answers with an empty name list, and that is a different fact from
    no site at all. Collapsing the two is the exact mistake this file
    exists to make visible.
    """
    return [
        (ast.unparse(expr), sorted(names))
        for _scope, expr, names in _producer_sites(ast.parse(source), own or {})
    ]


def names(source: str, own: dict[str, str] | None = None) -> list[str]:
    """Every check name the matchers read out of one snippet."""
    return sorted({name for _expr, found in read(source, own) for name in found})


def net(source: str, own: dict[str, str] | None = None) -> set[str]:
    """What layer 1 counts in the same snippet."""
    return signature_heads(ast.parse(source), own or {})


class TestEveryShapeThatGivesACheckName:
    """One per producer, and one per spelling within a producer.

    Deleting any branch of ``_name_sites`` or ``_signature_sites`` turns
    one of these red. Before this file, the same deletions turned nothing
    red as long as some OTHER site in the package still spelled the same
    name, which every one of them did.
    """

    def test_a_check_result_names_its_gate_positionally(self) -> None:
        assert names('CheckResult("linter", passed=False)\n') == ["linter"]

    def test_a_check_result_names_its_gate_by_keyword(self) -> None:
        assert names('CheckResult(name="typecheck", passed=False)\n') == ["typecheck"]

    def test_the_shared_gate_builder_counts_too(self) -> None:
        """``verify._failed_gate_result`` is the second CHECK_NAME_CALL:
        the three subprocess gates package their failures through it and
        never construct a ``CheckResult`` themselves."""
        assert names('_failed_gate_result("fixtures", "boom")\n') == ["fixtures"]

    def test_a_signatures_argument(self) -> None:
        assert names('self.fail(comp, err, signatures=["token_budget:exceeded"])\n') == [
            "token_budget"
        ]

    def test_a_direct_assignment_into_the_container(self) -> None:
        """The producer that is not a call at all. Both spellings of the
        mapping, because ``factory`` passes its own local in and
        ``SIGNATURE_CONTAINER_SUFFIX`` is what joins them."""
        assert names('component_failure_signatures[c] = ["contract:tier-failed"]\n') == ["contract"]
        assert names('self.component_failure_signatures[c] = ["engineer:stall"]\n') == ["engineer"]

    def test_a_dynamic_tail_still_gives_the_head(self) -> None:
        """``f"contract:tier_{n}"`` is the real shape of that producer."""
        assert names('component_failure_signatures[c] = [f"contract:tier_{n}"]\n') == ["contract"]

    def test_a_conditional_gives_both_arms(self) -> None:
        """``_setpoint_failure`` picks its signature with an ``IfExp``.
        Both arms are read, because the walk descends the SUBTREE rather
        than enumerating expression types."""
        source = 'self.fail(c, e, signatures=["review:a" if x else "security:b"])\n'
        assert names(source) == ["review", "security"]

    def test_a_module_constant_resolves(self) -> None:
        """#306's shape: the name is a constant in the defining module,
        not a literal at the call site."""
        source = "CheckResult(name=MUTATION, passed=False)\n"
        assert names(source, {"MUTATION": "mutation_testing"}) == ["mutation_testing"]


class TestTheEntryPointsAndTheFunnel:
    """The phase fallback, which is where #339 review found the hole.

    ``pipeline._record_failure_signatures`` files a failure under its
    PHASE whenever no truthy ``signatures`` reaches it, and it has three
    callers: ``fail``, ``retry_or_fail`` and ``_fail_pr_flow``. Keying
    the matcher on the first two named the phase at their call sites and
    said nothing at all about the third.
    """

    def test_the_two_entry_points_by_keyword(self) -> None:
        assert names('self.fail(comp, err, phase="provisioning")\n') == ["provisioning"]
        assert names('self.retry_or_fail(comp, err, ctx, phase="verify")\n') == ["verify"]

    def test_the_funnel_called_directly_and_positionally(self) -> None:
        """THE REGRESSION. This is ``_fail_pr_flow`` as it was written:
        no ``phase=`` keyword, no ``signatures=``, and a phase with no
        colon in it. Every layer was silent, and mutating the string left
        the whole suite green."""
        assert names('self._record_failure_signatures(comp, "pr", err, None)\n') == ["pr"]
        assert net('self._record_failure_signatures(comp, "pr", err, None)\n') == set()

    def test_the_funnel_called_by_keyword(self) -> None:
        """How ``_fail_pr_flow`` is written now. Both spellings are read,
        so the fix is not load-bearing on the call staying keyword."""
        source = 'self._record_failure_signatures(comp, phase="pr", error=e, signatures=None)\n'
        assert names(source) == ["pr"]

    def test_every_keyed_name_is_read_positionally_too(self) -> None:
        """THE INDEX, pinned by behaviour rather than by an integer.

        ``_phase_argument`` falls back to the position of ``phase`` in
        the callable's own definition, and #339 review measured why that
        has to be tested here: when the four indices were written by
        hand, six of eight off-by-one mutations to them left every guard
        green, because 25 of the 27 call sites in ``kstrl/`` pass
        ``phase`` by keyword and the two that do not resolve to nothing.
        One positional snippet per keyed name is what makes a wrong
        index a red test."""
        assert names('self.fail(comp, err, "provisioning")\n') == ["provisioning"]
        assert names('self.retry_or_fail(comp, err, ctx, "verify")\n') == ["verify"]
        assert names('self._record_failure_signatures(comp, "pr", err, None)\n') == ["pr"]
        assert names('PhaseFailure(action, err, "deploy")\n') == ["deploy"]

    def test_a_definition_without_a_phase_contributes_no_index(self) -> None:
        """The other end of the derivation, and the special case it
        replaced. ``feature_cmd`` has a one-argument local ``fail``;
        ``pipeline.fail`` is what puts ``phase`` at index 2. Reading the
        index off the DEFINITION means a name with no such parameter
        anywhere answers ``None`` and is skipped, instead of needing a
        rule that says so."""
        assert parameter_index("fail", "phase") == 2
        assert parameter_index("fail", "not_a_parameter") is None
        assert parameter_index("no_such_callable_anywhere", "phase") is None

    def test_a_typed_carrier_names_its_phase(self) -> None:
        """``PhaseFailure`` records nothing itself; ``_route_failure``
        unpacks it into ``fail`` / ``retry_or_fail`` and hands on
        ``failure.phase``, which the walk cannot read. Reading the phase
        where it is WRITTEN is what closes that."""
        assert names('PhaseFailure(action=a, error=e, phase="deploy")\n') == ["deploy"]

    def test_a_truthy_signatures_argument_retires_the_phase(self) -> None:
        """The recorder branches on ``if signatures:``, so a phase that
        cannot be reached is not a check name. ``factory`` depends on
        this: its scope refusal passes ``phase="scope"`` with a real
        signature, and reading that phase would put an unenrollable
        name in the census."""
        source = 'self.fail(c, e, phase="scope", signatures=["scope_unreadable:no-scope"])\n'
        assert names(source) == ["scope_unreadable"]

    def test_an_empty_signatures_argument_does_not(self) -> None:
        """The other end of the same predicate, and the wrinkle that made
        the first fix wrong: the previous test was ``"signatures" in
        keywords``, so a literal ``None`` counted as signatures and hid
        the phase."""
        for empty in ("None", "[]", '""'):
            source = f'self.fail(c, e, phase="pr", signatures={empty})\n'
            assert names(source) == ["pr"], source

    def test_an_undecidable_signatures_argument_does_not_either(self) -> None:
        """``_route_failure`` hands on ``failure.signatures``, which is
        ``None`` at run time for any ``PhaseFailure`` built without one.
        Unknown is not evidence that the phase is unreachable, so both
        expressions are reported: the phase, and the signatures."""
        source = "self.fail(c, e, phase=failure.phase, signatures=failure.signatures)\n"
        assert read(source) == [("failure.phase", []), ("failure.signatures", [])]


class TestTheShapesThatSayNothing:
    """Pinned because silence is what this guard fails INTO.

    Each of these is either a site the matchers detect and cannot read -
    which must appear as an empty-name row, and therefore in
    ``BLIND_SITES`` - or a shape that is deliberately not a producer.
    Without this class, both look identical from outside.
    """

    def test_a_pass_through_is_detected_and_unread(self) -> None:
        """The property, stated on a fixture. A producer that moves out
        of reach must not look like a producer that went away."""
        assert read('component_failure_signatures[c] = [f"{whatever}:code"]\n') == [
            ("f'{whatever}:code'", [])
        ]

    def test_the_funnels_own_callers_pass_a_parameter(self) -> None:
        """``fail`` and ``retry_or_fail`` hand their own ``phase``
        parameter to the funnel. Detected, unreadable here, and read at
        THEIR call sites instead, which is why all three names are keyed
        rather than only the funnel."""
        assert read("self._record_failure_signatures(comp, phase, error, signatures)\n") == [
            ("phase", [])
        ]

    def test_a_same_named_local_helper_is_not_a_recorder(self) -> None:
        """``feature_cmd`` has a one-argument helper also called
        ``fail``. Matching on the bare callable name once credited six of
        its call sites with the check name ``"unknown"``; the positional
        index is what keeps that from happening, and it is asserted here
        because no other test can tell the two apart."""
        assert read("fail(detail)\n") == []
        assert read('fail("understand phase exited 2")\n') == []

    def test_a_carrier_with_a_real_signature_is_not_read_twice(self) -> None:
        """``PhaseFailure`` with signatures gives the signature and not
        the phase, so enrolment is never asked about a phase the
        recorder will not use."""
        source = (
            'PhaseFailure(action=a, error=e, phase="review", signatures=["review:divergence"])\n'
        )
        assert names(source) == ["review"]

    def test_an_absent_signatures_argument_is_not_a_blind_site(self) -> None:
        """``signatures=None`` is the ABSENCE of a signature, not one the
        walk failed to read. Enumerating it would put a row in
        ``BLIND_SITES`` that no reader can act on."""
        detected = [expr for expr, _found in read('self.fail(c, e, phase="pr", signatures=None)\n')]
        assert detected == ["'pr'"]

    def test_prose_and_an_unrelated_call_are_not_producers(self) -> None:
        """The false-positive side. Without it, "detect everything"
        passes every test above."""
        assert read('self.ui.warn("Review: the gate failed")\n') == []
        assert read('log = {"check": "linter"}\n') == []
        assert read("CheckResult()\n") == []


class TestLayerOneCatchesWhatLayerTwoCannot:
    """The two-layer claim, asserted rather than described.

    Each case pins BOTH halves: the matchers say nothing, and the net in
    ``tests/test_signature_spellings.py`` counts the spelling. Asserting
    only the first would pass on a matcher nobody had ever narrowed;
    asserting only the second would pass on a net that had been switched
    off.
    """

    def check(self, source: str, expected: set[str]) -> None:
        assert names(source) == [], "the matchers were expected to be silent here"
        assert net(source) == expected

    def test_a_signature_returned_straight_out_of_a_function(self) -> None:
        """The #299 shape, in this vocabulary: a body never bound to
        anything the matcher keys on."""
        self.check('def sig():\n    return "engineer:no-progress-stall"\n', {"engineer"})

    def test_a_signature_in_a_dispatch_table(self) -> None:
        self.check('TABLE = {"stall": "engineer:no-progress-stall"}\n', {"engineer"})

    def test_a_signature_appended_rather_than_assigned(self) -> None:
        self.check('sigs.append("token_budget:exceeded")\n', {"token_budget"})

    def test_a_signature_in_a_default_argument(self) -> None:
        self.check('def record(sig="diff:fetch-failed"):\n    pass\n', {"diff"})

    def test_a_signature_built_by_a_comprehension(self) -> None:
        self.check('sigs = [f"contract:{c}" for c in cs]\n', {"contract"})

    def test_the_net_is_not_simply_counting_every_colon(self) -> None:
        """The false-positive side of layer 1, or a net that returned
        every string would pass all five above."""
        assert net('msg = "Note: the gate failed"\n') == set()
        assert net('url = "https://example.test/x"\n') == set()


class TestWhatNEITHERLAYERSEES:
    """The residual, asserted so the disclosure stays true.

    A string the interpreter has to BUILD answers nothing to folding, so
    the net cannot count it and the matchers cannot read it. The bound on
    it: such a site still has to reach ``component_failure_signatures``
    through one of the four producers, so it appears in ``BLIND_SITES``
    as an unread row rather than vanishing - which is the difference
    between a disclosed limit and the silence ``_fail_pr_flow`` sat in.
    """

    def test_a_runtime_built_signature_is_seen_but_not_read(self) -> None:
        for built in (
            '"%s:code" % phase',
            '":".join((phase, "code"))',
            '"{}:code".format(phase)',
        ):
            source = f"self.fail(c, e, signatures=[{built}])\n"
            assert names(source) == [], built
            assert net(source) == set(), built
            # Through ``ast.unparse`` on both sides: the ledger stores
            # unparsed text, which normalises quoting, and pinning the
            # source spelling instead would fail on a re-quote rather
            # than on a change of behaviour.
            unparsed = ast.unparse(ast.parse(built, mode="eval").body)
            assert read(source) == [(unparsed, [])], built
