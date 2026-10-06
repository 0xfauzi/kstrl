"""Every write into the inbox from ``kstrl/``, and what it catches (#232).

The defect this exists to catch is one the review found twice in one PR.
Every ``Inbox`` write takes the control lock, so a write can
raise ``ControlStateError``, a ``RuntimeError`` that the
``(OSError, ValueError)`` pair every inbox site was hand-written with
does not catch. Round 1 fixed the four sites it had a finding for; two
more had the identical hole and were found by reading, and a seventh
(the TUI's decide path) by this walk.

``InboxConfig.load`` casts per key, so ``[inbox] open_item_cap =
1979-05-27`` raises ``TypeError``, which is not a ``ValueError`` either.
Same shape, one call earlier, and it was missing from three of the sites
written to close the first one.

So the guard is not a list of the sites somebody remembered. It is a
census of where an ``Inbox`` is OBTAINED, which is closed by
construction: you cannot write to an inbox you did not construct, and
every construction in the package resolves. An eighth write site either
constructs one, and fails the census below, or takes one from a
constructor already pinned in that module, and fails the mutation
inventory. Both halves carry a control, because an inventory that
matches is also what a walk pointed at nothing returns.

What this does NOT see, stated rather than left implicit: a mutating
call through a receiver the resolver cannot place. That residual is what
the construction census is for; it is why the census is the first layer
and not a convenience.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import astwalk

INBOX = "kstrl.inbox.Inbox"
INBOX_CONFIG_LOAD = "kstrl.inbox.InboxConfig.load"
CONTROL_ERROR = "kstrl.statedir.ControlStateError"

#: Every method of ``Inbox`` that appends and therefore takes the control
#: lock. ``_decide`` is the shared body behind four of them,
#: so they are enumerated by their public spellings.
MUTATORS = frozenset({"add", "approve", "compact", "reject", "resolve", "snooze"})

#: Every place ``kstrl/`` constructs an ``Inbox``, line numbers dropped.
#: A new one is an unexplained delta: adding a row here is how you say
#: you have decided what the new site catches.
EXPECTED_CONSTRUCTIONS = (
    "autonomy.py kstrl.inbox.Inbox",
    "calibration_ladder.py kstrl.inbox.Inbox",
    "cli.py kstrl.inbox.Inbox",
    "decisions.py kstrl.inbox.Inbox",
    "factory.py kstrl.inbox.Inbox",
    # #639 slice 4: read_owner_answers scans for the owner's answers; reads only.
    "owner_answers.py kstrl.inbox.Inbox",
    "pipeline.py kstrl.inbox.Inbox",
    # #602: the L1 plan gate reads its item (_find) and records a decision
    # given at the prompt (_record).
    "plan_gate.py kstrl.inbox.Inbox",
    "serve.py kstrl.inbox.Inbox",
    # #696 slice 3: a [stack]'s confirmation is read (_latest_approval),
    # filed (file_stack_item) and decided at the ks factory prompt
    # (decide_at_prompt).
    "stack.py kstrl.inbox.Inbox",
    # #433: home's needs-you rows list open items. It scans and reads;
    # it mutates nothing. Moved from tui/home_data.py, whose counter it
    # replaced.
    "tui/operator_queue.py kstrl.inbox.Inbox",
    "tui/screens/inbox.py kstrl.inbox.Inbox",
    # #646: ks retry reads the approvals (approvals_at) to decide whether
    # it keeps the failed head. It scans and reads; it mutates nothing.
    "waivers.py kstrl.inbox.Inbox",
)

#: The same census BY COUNT. ``without_line_numbers`` deduplicates, so
#: the tuple above cannot see a second construction in a module that
#: already has one: ``pipeline`` has three and ``serve`` two. A pin whose
#: subject is "how many" needs its own row.
EXPECTED_CONSTRUCTION_COUNTS = {
    # #643: was 1 (apply_demotion). +1: _calibration_blockers scans the
    # inbox for an undecided calibration_drift item. It reads; it mutates nothing.
    "autonomy.py": 2,
    "calibration_ladder.py": 1,
    "cli.py": 1,
    # #644 slice 2: was 2. +1: escalation_naming scans the inbox for the
    # undecided spec_escalation row that names a poisoned item. It reads;
    # it mutates nothing.
    "decisions.py": 3,
    "factory.py": 1,
    "owner_answers.py": 1,
    # #595 (addendum): was 5, one construction per lazy-open site
    # (ComponentPipeline.snapshot_waivers's own +1 among them). All five
    # sites now build through ComponentPipeline._open_inbox, so there is
    # exactly one Inbox(...) call left in the file.
    "pipeline.py": 1,
    "plan_gate.py": 2,
    "serve.py": 2,
    "stack.py": 3,
    "tui/operator_queue.py": 1,
    "tui/screens/inbox.py": 1,
    "waivers.py": 1,
}

#: Calls whose callee has no identifier at all, which ``calls_to`` treats
#: as a candidate for every target set. Neither is an inbox, and both are
#: pinned rather than filtered so a third one has to be looked at.
EXPECTED_UNDECIDED = (
    # #632: the fixture comparison tables and the fixture runner table.
    "fixture_expect.py COMPARATORS[kind]",
    "fixture_expect.py VALIDATORS[kind]",
    "fixtures.py _RUNNERS[fixture.fixture_type]",
    "tui/app.py initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py initial_screens_for_kind(kind, observe_only=True)",
)


@dataclass(frozen=True)
class Disposition:
    """What a site is contracted to do with a control-state failure.

    ``guarded`` means an enclosing ``try`` in the same function names
    ``ControlStateError`` by ORIGIN, or catches everything
    (``astwalk.catches_everything``, the rule shared with
    ``tests/test_serve_config_reads.py`` since #364). ``propagates`` means
    the exception is the caller's answer, and the reason has to say why
    that is right there; a row without one fails.
    """

    guarded: bool
    reason: str = ""


_GUARDED = Disposition(guarded=True)

#: Every inbox mutation in ``kstrl/``, and what it is contracted to do.
#: Keyed ``module::scope::method`` so an edit above the site cannot break
#: the pin.
EXPECTED_MUTATIONS: dict[str, Disposition] = {
    "autonomy.py::apply_demotion::add": _GUARDED,
    "calibration_ladder.py::_open_drift_item::add": _GUARDED,
    "decisions.py::open_escalation_item::add": _GUARDED,
    "decisions.py::resolve_escalation_items::resolve": _GUARDED,
    "factory.py::_open_health_breach_items::add": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_add::add": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_resolve::resolve": _GUARDED,
    "pipeline.py::ComponentPipeline._inbox_resolve_component::resolve": _GUARDED,
    "plan_gate.py::_record::add": _GUARDED,
    "plan_gate.py::_record::approve": _GUARDED,
    "plan_gate.py::_record::reject": _GUARDED,
    "serve.py::_file_inbox_item::add": _GUARDED,
    # #696: each propagates to a caller that refuses on it. unconfirmed_lines
    # catches everything file_stack_item raises; cli._stack_checkpoint
    # catches SURFACE_REJECTIONS (ControlStateError is a RuntimeError).
    "stack.py::file_stack_item::add": Disposition(guarded=False, reason="the caller refuses"),
    "stack.py::decide_at_prompt::approve": Disposition(guarded=False, reason="as ::add"),
    "stack.py::decide_at_prompt::reject": Disposition(guarded=False, reason="as ::add"),
    "tui/screens/inbox.py::InboxScreen._decide::approve": _GUARDED,
    "tui/screens/inbox.py::InboxScreen._decide::reject": _GUARDED,
    "tui/screens/inbox.py::InboxScreen._decide::snooze": _GUARDED,
    "cli.py::_decide_and_report::approve": Disposition(
        guarded=False,
        reason=(
            "an operator typed this command and is watching it; a control "
            "lock it could not take is the command's answer, not a "
            "bookkeeping failure to absorb behind a transition that has "
            "already happened"
        ),
    ),
    "cli.py::_decide_and_report::reject": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::_decide_and_report::snooze": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::_decide_and_report::resolve": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::_decide_parked_merge_if_parked::approve": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::_decide_parked_merge_if_parked::reject": Disposition(
        guarded=False, reason="as _decide_and_report::approve"
    ),
    "cli.py::inbox_retry::resolve": Disposition(
        guarded=False,
        reason=(
            "the manifest reset above it has already been saved, but this "
            "is an operator command in the foreground: it must not report "
            "a requeue it could not finish"
        ),
    ),
}

#: Every ``InboxConfig.load`` call in ``kstrl/``. ``guarded`` here means
#: an enclosing ``try`` names ``TypeError``, which is what a per-key cast
#: raises on a TOML date or array.
#: NOT IN THIS TABLE, and stated rather than left as an absence: the
#: factory's run envelope resolves ``[inbox]`` by handing
#: ``InboxConfig.load`` to ``config_preflight.resolve_or_report`` as a
#: VALUE, so there is no call for this walk to find and no enclosing
#: ``try`` for it to read. The disposition is made one frame away and it
#: is not guarded here: ``resolve_or_report`` catches the ``TypeError``
#: and returns the line naming ``[inbox]`` and the key, and the factory
#: refuses the run with exit code 2 before the run directory exists
#: (#192 round 2). That the envelope still names ``[inbox]`` at all is
#: pinned by ``tests/test_config_guard.py``'s
#: ``EXPECTED_ENVELOPE_SECTIONS``, which walks references rather than
#: calls, so dropping the section fails there instead of quietly
#: shrinking this table.
EXPECTED_CONFIG_LOADS: dict[str, Disposition] = {
    # #643: the L2 entry criterion; a load it cannot make is a blocker.
    "autonomy.py::_calibration_blockers": _GUARDED,
    "autonomy.py::apply_demotion": _GUARDED,
    "calibration_ladder.py::_open_drift_item": _GUARDED,
    "decisions.py::open_escalation_item": _GUARDED,
    "decisions.py::resolve_escalation_items": _GUARDED,
    "factory.py::_open_health_breach_items": _GUARDED,
    "serve.py::_file_inbox_item": _GUARDED,
    # #696 slice 3: a load the stack check cannot make is a refusal.
    "stack.py::_refusal": _GUARDED,
    "stack.py::file_stack_item": Disposition(
        guarded=False,
        reason=(
            "every caller refuses on what it raises: unconfirmed_lines catches "
            "everything, and decide_at_prompt's caller catches TypeError"
        ),
    ),
    "stack.py::decide_at_prompt": Disposition(
        guarded=False,
        reason="cli._stack_checkpoint catches SURFACE_REJECTIONS, TypeError among them",
    ),
    # #433: home's needs-you rows; a load it cannot make renders no count.
    "tui/operator_queue.py::_open_inbox_items": _GUARDED,
    # #646: ks retry's read of the approvals; a load it cannot make keeps nothing.
    "waivers.py::approvals_at": _GUARDED,
    "serve.py::check_inbox_cap": Disposition(
        guarded=False,
        reason=(
            "a read-only admission gate. [inbox] is a preflight section, so "
            "a malformed value is reported as a configuration problem "
            "before the daemon reaches here, and a gate that cannot read "
            "its own cap must refuse rather than admit"
        ),
    ),
    "cli.py::_inbox_for": Disposition(
        guarded=False,
        reason=(
            "an operator command in the foreground, and the preflight has "
            "already named a malformed [inbox] as a configuration problem"
        ),
    ),
}


def _construction_sites() -> astwalk.Sites:
    """Every ``Inbox(...)`` in the package, with line numbers kept.

    Kept because two pins read this: one over the rows, which
    deduplicates, and one over the counts, which must not.
    """
    found = astwalk.Sites()
    for source in astwalk.package_sources():
        found += astwalk.calls_to(
            astwalk.parsed(source),
            {INBOX},
            where=astwalk.label(source),
            module=astwalk.module_name(source),
        )
    return found.sorted()


def _returns_an_inbox(scope: ast.AST, table: astwalk.Bindings) -> bool:
    """Whether this function hands an ``Inbox`` back to its caller.

    Anywhere in the returned expression, so ``return root_dir,
    Inbox(...)`` counts: ``cli._inbox_for`` returns the pair and every
    ``ks inbox`` command unpacks it.
    """
    return any(
        isinstance(inner, ast.Call) and table.resolve(inner.func) == INBOX
        for node in astwalk.own_nodes(scope)
        if isinstance(node, ast.Return) and node.value is not None
        for inner in ast.walk(node.value)
    )


def _inbox_holders(tree: ast.Module, table: astwalk.Bindings) -> frozenset[str]:
    """Names in one module that hold an ``Inbox``.

    Bound from a construction directly, or from a call to a function in
    this module that returns one. Over-matching is the safe direction
    here: an extra holder adds a row somebody has to enrol, and a missing
    one is the skip direction the construction census covers.
    """
    returning = {
        scope.name
        for scope, _ in astwalk.scopes(tree)
        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
        and _returns_an_inbox(scope, table)
    }
    holders: set[str] = set()
    for node in astwalk.all_nodes(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            continue
        value = node.value
        if value is None:
            continue
        gives_inbox = any(
            isinstance(inner, ast.Call)
            and (table.resolve(inner.func) == INBOX or astwalk.leaf_name(inner.func) in returning)
            for inner in ast.walk(value)
        )
        if gives_inbox:
            holders.update(_target_names(node))
    return frozenset(holders)


def _target_names(node: ast.Assign | ast.AnnAssign | ast.NamedExpr) -> set[str]:
    """Every dotted target one binding binds, tuple targets expanded.

    ``astwalk.assignment_parts`` answers None for a tuple target, and
    ``root_dir, box = _inbox_for(root)`` is how six ``ks inbox`` commands
    get theirs.
    """
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: set[str] = set()
    for target in targets:
        parts = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
        names.update(name for part in parts if (name := astwalk.dotted(part)) is not None)
    return names


def _mutation_calls(source: Path) -> list[tuple[str, ast.Call, ast.AST]]:
    """Every inbox mutation call in one module, keyed ``module::scope::method``.

    A LIST, one entry per call: two calls of one method in one scope share
    a key, and a dict keeps only the last of them.
    """
    tree = astwalk.parsed(source)
    table = astwalk.bindings(tree, module=astwalk.module_name(source))
    holders = _inbox_holders(tree, table)
    calls: list[tuple[str, ast.Call, ast.AST]] = []
    for scope, qualified in astwalk.scopes(tree):
        for node in astwalk.own_nodes(scope):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in MUTATORS:
                continue
            receiver = node.func.value
            on_an_inbox = (
                isinstance(receiver, ast.Call) and table.resolve(receiver.func) == INBOX
            ) or astwalk.dotted(receiver) in holders
            if on_an_inbox:
                key = f"{astwalk.label(source)}::{qualified}::{node.func.attr}"
                calls.append((key, node, scope))
    return calls


def _mutation_rows(source: Path) -> dict[str, tuple[ast.Call, ast.AST]]:
    """Every inbox mutation in one module, keyed ``module::scope::method``."""
    return {key: (call, scope) for key, call, scope in _mutation_calls(source)}


def _config_load_rows(source: Path) -> dict[str, tuple[ast.Call, ast.AST]]:
    """Every ``InboxConfig.load`` call in one module, keyed by scope."""
    tree = astwalk.parsed(source)
    table = astwalk.bindings(tree, module=astwalk.module_name(source))
    rows: dict[str, tuple[ast.Call, ast.AST]] = {}
    for scope, qualified in astwalk.scopes(tree):
        for node in astwalk.own_nodes(scope):
            if isinstance(node, ast.Call) and table.resolve(node.func) == INBOX_CONFIG_LOAD:
                rows[f"{astwalk.label(source)}::{qualified}"] = (node, scope)
    return rows


def _enclosing_tries(scope: ast.AST, call: ast.Call) -> list[ast.Try | ast.TryStar]:
    """Every ``try`` in this scope whose BODY holds the call.

    ``try_body_nodes`` rather than ``ast.walk``, so a handler is not
    credited with guarding a call in a function merely DEFINED in its
    body.
    """
    return [
        node
        for node in astwalk.own_nodes(scope)
        if isinstance(node, ast.Try | ast.TryStar) and call in astwalk.try_body_nodes(node)
    ]


def _catching(scope: ast.AST, call: ast.Call, table: astwalk.Bindings) -> list[astwalk.Clause]:
    """The clauses of every ``try`` that holds the call, in order."""
    found: list[astwalk.Clause] = []
    for node in _enclosing_tries(scope, call):
        found.extend(astwalk.handler_clauses(node, table))
    return found


def _broadly_caught(scope: ast.AST, call: ast.Call, table: astwalk.Bindings) -> bool:
    """The second way a site can clear (#364): see
    ``astwalk.catches_everything`` for the rule."""
    return any(astwalk.catches_everything(node, table) for node in _enclosing_tries(scope, call))


@functools.cache
def _all_rows(subject: str) -> dict[str, tuple[ast.Call, ast.AST, astwalk.Bindings]]:
    """Every row of one kind over the whole package, with its module's table.

    ``subject`` is ``"mutations"`` or ``"configs"``; the table travels
    with the row because a handler is resolved against the module that
    wrote it, not against the one the guard lives in. ``@functools.cache``
    because this walks the whole package and every test in this module
    calls it fresh: pure function of ``subject`` over session-constant
    sources, measured 8.3s to 1.1s for the module with it.
    """
    built: dict[str, tuple[ast.Call, ast.AST, astwalk.Bindings]] = {}
    for source in astwalk.package_sources():
        table = astwalk.bindings(astwalk.parsed(source), module=astwalk.module_name(source))
        rows = _mutation_rows(source) if subject == "mutations" else _config_load_rows(source)
        for key, (call, scope) in rows.items():
            built[key] = (call, scope, table)
    return built


CONTROL_MUTATION = """
from kstrl.inbox import Inbox, InboxConfig

def emit(root):
    box = Inbox(root, InboxConfig.load(root))
    box.add("kind", "title")
"""


class TestConstructionCensus:
    def test_every_inbox_construction_is_pinned(self) -> None:
        """The closed half: you cannot write to an inbox you did not obtain."""
        found = _construction_sites().without_line_numbers().sorted()
        astwalk.assert_sites(
            found,
            seen=EXPECTED_CONSTRUCTIONS,
            undecided=EXPECTED_UNDECIDED,
            message=(
                "kstrl/ constructs an Inbox somewhere new. Decide what that "
                "site catches, enrol its mutations in EXPECTED_MUTATIONS, "
                "and add the row here."
            ),
        )

    def test_the_construction_count_is_pinned(self) -> None:
        counts: dict[str, int] = {}
        for row in _construction_sites().seen:
            module = row.split(":", 1)[0]
            counts[module] = counts.get(module, 0) + 1
        assert counts == EXPECTED_CONSTRUCTION_COUNTS, (
            "a module gained or lost an Inbox construction. The row-based "
            f"census above deduplicates and cannot see this. Found: {counts}"
        )

    def test_the_census_net_fires(self) -> None:
        """A matching inventory is also what a switched-off net returns."""
        control = astwalk.parse(CONTROL_MUTATION)
        assert astwalk.calls_to(control, {INBOX}, where="control").seen


class TestMutationInventory:
    def test_every_mutation_is_pinned(self) -> None:
        rows = _all_rows("mutations")
        assert set(rows) == set(EXPECTED_MUTATIONS), (
            "the inbox mutation sites in kstrl/ have moved. Every one of "
            "them takes the control lock, so each needs a disposition: "
            f"found {sorted(rows)}"
        )

    def test_the_mutation_net_fires(self) -> None:
        """The control, planted through the same resolver the walk uses."""
        tree = astwalk.parse(CONTROL_MUTATION)
        table = astwalk.bindings(tree)
        holders = _inbox_holders(tree, table)
        assert "box" in holders
        found = [
            node
            for scope, _ in astwalk.scopes(tree)
            for node in astwalk.own_nodes(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATORS
            and astwalk.dotted(node.func.value) in holders
        ]
        assert len(found) == 1

    @pytest.mark.parametrize("key", sorted(EXPECTED_MUTATIONS))
    def test_each_mutation_matches_its_disposition(self, key: str) -> None:
        call, scope, table = _all_rows("mutations")[key]
        clauses = _catching(scope, call, table)
        caught = {origin for clause in clauses for origin in clause.origins}
        expected = EXPECTED_MUTATIONS[key]
        broad = _broadly_caught(scope, call, table)
        if expected.guarded:
            assert CONTROL_ERROR in caught or broad, (
                f"{key} writes to the inbox without an enclosing handler "
                f"naming {CONTROL_ERROR} by origin, and without one that "
                "catches everything. Every Inbox write takes the control "
                f"lock, so this site can raise a RuntimeError a "
                f"narrow clause does not catch. Caught: {sorted(caught)}"
            )
        else:
            assert expected.reason, f"{key} propagates and says nothing about why"
            assert CONTROL_ERROR not in caught and not broad, (
                f"{key} is enrolled as propagating and now catches "
                f"{CONTROL_ERROR}. Move it to guarded."
            )


class TestConfigLoadInventory:
    def test_the_config_load_net_fires(self) -> None:
        """This layer's own control.

        Layers 1 and 2 have one each; without this, switching off the
        resolver that decides a call IS ``InboxConfig.load`` returns an
        empty inventory, and an empty inventory compared against an empty
        expectation is what a passing guard looks like. The control is
        the same source both other layers use, so one planted string
        cannot satisfy one layer while the next reads nothing.
        """
        tree = astwalk.parse(CONTROL_MUTATION)
        table = astwalk.bindings(tree)
        found = [
            node
            for scope, _ in astwalk.scopes(tree)
            for node in astwalk.own_nodes(scope)
            if isinstance(node, ast.Call) and table.resolve(node.func) == INBOX_CONFIG_LOAD
        ]
        assert len(found) == 1

    def test_every_config_load_is_pinned(self) -> None:
        rows = _all_rows("configs")
        assert set(rows) == set(EXPECTED_CONFIG_LOADS), (
            "InboxConfig.load is read somewhere new in kstrl/. It casts per "
            "key, so a TOML date raises TypeError; decide what this site "
            f"does with that. Found {sorted(rows)}"
        )

    @pytest.mark.parametrize("key", sorted(EXPECTED_CONFIG_LOADS))
    def test_each_config_load_matches_its_disposition(self, key: str) -> None:
        call, scope, table = _all_rows("configs")[key]
        names = {name for clause in _catching(scope, call, table) for name in clause.names}
        expected = EXPECTED_CONFIG_LOADS[key]
        if expected.guarded:
            assert "TypeError" in names or _broadly_caught(scope, call, table), (
                f"{key} reads InboxConfig without catching TypeError and "
                "without catching everything. int(section['open_item_cap']) "
                "on a TOML date raises one, and it is not a ValueError. "
                "Caught: " + repr(sorted(names))
            )
        else:
            assert expected.reason, f"{key} propagates and says nothing about why"


class TestTheTwoWaysASiteClears:
    """One control per disjunct, because the rule is now a disjunction.

    ``CONTROL_ERROR in caught or broad`` stays green with either half
    deleted, so each half is proved on its own planted source. The third
    case is the one that must clear NEITHER, which is what stops a
    widening from turning into a clearing.
    """

    @staticmethod
    def _site(handler: str) -> tuple[ast.AST, ast.Call, astwalk.Bindings]:
        source = (
            "from kstrl.inbox import Inbox, InboxConfig\n"
            "from kstrl.statedir import ControlStateError\n"
            "def emit(root):\n"
            "    try:\n"
            "        box = Inbox(root, InboxConfig.load(root))\n"
            "        box.add('kind', 'title')\n"
            f"    except {handler}:\n"
            "        return ''\n"
        )
        tree = astwalk.parse(source)
        table = astwalk.bindings(tree, module="probe")
        scope = next(node for node, name in astwalk.scopes(tree) if name == "emit")
        call = next(
            node
            for node in astwalk.own_nodes(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add"
        )
        return scope, call, table

    def test_naming_the_origin_clears_and_is_not_broad(self) -> None:
        scope, call, table = self._site("ControlStateError")
        caught = {origin for clause in _catching(scope, call, table) for origin in clause.origins}
        assert CONTROL_ERROR in caught
        assert not _broadly_caught(scope, call, table)

    def test_catching_everything_clears_and_names_no_origin(self) -> None:
        scope, call, table = self._site("Exception")
        caught = {origin for clause in _catching(scope, call, table) for origin in clause.origins}
        assert CONTROL_ERROR not in caught
        assert _broadly_caught(scope, call, table)

    def test_a_narrow_handler_clears_neither_way(self) -> None:
        scope, call, table = self._site("ValueError")
        caught = {origin for clause in _catching(scope, call, table) for origin in clause.origins}
        assert CONTROL_ERROR not in caught
        assert not _broadly_caught(scope, call, table)


# --- #648: every writer decides on the row it reads inside the lock --------

#: The ``Inbox`` methods that decide an item: ``_decide`` behind each one.
DECIDERS = frozenset({"approve", "reject", "resolve", "snooze"})

#: Decisions that overwrite whatever the FRESH row says, because a person
#: typed them and their answer is the decision. Every other decision
#: passes ``only_from``, so an operator's answer that lands after the
#: caller read the item survives (#648). A new unconditional decision
#: fails until someone adds its row here with the reason.
UNCONDITIONAL_DECISIONS: dict[str, str] = {
    "cli.py::_decide_and_report::approve": "the operator typed ks inbox approve",
    "cli.py::_decide_and_report::reject": "the operator typed ks inbox reject",
    "cli.py::_decide_and_report::snooze": "the operator typed ks inbox snooze",
    "cli.py::_decide_and_report::resolve": "the operator typed the command",
    "cli.py::_decide_parked_merge_if_parked::approve": "the operator typed ks inbox approve",
    "cli.py::_decide_parked_merge_if_parked::reject": "the operator typed ks inbox reject",
    "cli.py::inbox_retry::resolve": "the operator typed ks inbox retry",
    "plan_gate.py::_record::approve": "the operator answered the plan checkpoint prompt",
    "plan_gate.py::_record::reject": "the operator answered the plan checkpoint prompt",
    "stack.py::decide_at_prompt::approve": "the operator answered the stack checkpoint prompt",
    "stack.py::decide_at_prompt::reject": "the operator answered the stack checkpoint prompt",
    "tui/screens/inbox.py::InboxScreen._decide::approve": "the operator pressed approve",
    "tui/screens/inbox.py::InboxScreen._decide::reject": "the operator chose a reason",
    "tui/screens/inbox.py::InboxScreen._decide::snooze": "the operator pressed snooze",
}

CONTROL_DECISION = """
from kstrl.inbox import UNDECIDED, Inbox

def emit(root, item):
    box = Inbox(root)
    box.resolve(item.id)
    box.resolve(item.id, only_from=UNDECIDED)
"""


def _unconditional(calls: list[tuple[str, ast.Call, ast.AST]]) -> set[str]:
    """Keys with at least one decision that names no ``only_from``.

    Flags when unsure: ``**kwargs`` or ``only_from=None`` clears nothing.
    """
    return {
        key
        for key, call, _scope in calls
        if key.rsplit("::", 1)[1] in DECIDERS
        and not any(
            kw.arg == "only_from"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
            for kw in call.keywords
        )
    }


class TestAnAutomatedDecisionKeepsTheOperatorsAnswer:
    def test_every_unconditional_decision_is_enrolled(self) -> None:
        calls = [c for source in astwalk.package_sources() for c in _mutation_calls(source)]
        found = _unconditional(calls)
        assert found == set(UNCONDITIONAL_DECISIONS), (
            "an inbox decision without only_from overwrites an operator's answer "
            "that landed after its read (#648). Pass only_from=UNDECIDED, or, "
            "if a person typed it, enrol it with the reason. "
            f"New: {sorted(found - set(UNCONDITIONAL_DECISIONS))}; "
            f"stale: {sorted(set(UNCONDITIONAL_DECISIONS) - found)}"
        )
        assert all(UNCONDITIONAL_DECISIONS.values())

    def test_the_net_fires_on_a_bare_resolve_beside_a_guarded_one(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch.py"
        scratch.write_text(CONTROL_DECISION, encoding="utf-8")
        assert _unconditional(_mutation_calls(scratch)) == {"scratch.py::emit::resolve"}


#: Calls in ``kstrl/inbox.py`` that write the log, per scope and callee.
#: Not seen: a write through a callee not named here (``path.write_text``,
#: ``open(..., "w")``); every append open in the package is pinned by
#: ``tests/test_append_opens_have_one_home.py``.
LOG_WRITES = frozenset(
    {"_append_unlocked", "append_records", "atomic_write_json", "atomic_write_text"}
)
#: The one scope that writes with no lock: its caller holds it.
APPEND_PRIMITIVE = "Inbox._append_unlocked"
EXPECTED_LOG_WRITES = {
    "Inbox._append_unlocked::append_records": 1,
    "Inbox._decide::_append_unlocked": 1,
    "Inbox.add::_append_unlocked": 2,
    "Inbox.compact::atomic_write_text": 1,
}

CONTROL_STALE_WRITER = """
class Inbox:
    def add(self, key):
        existing = self.find_by_dedupe_key(key)
        with control_lock(self.root_dir):
            self._append_unlocked(existing)
"""


def _log_writes(scope: ast.AST) -> list[ast.Call]:
    """Calls in ``scope`` whose callee's last name is in ``LOG_WRITES``, on any
    receiver: ``append_records`` and ``atomic_write_text`` are module functions."""
    return [
        node
        for node in astwalk.own_nodes(scope)
        if isinstance(node, ast.Call) and astwalk.leaf_name(node.func) in LOG_WRITES
    ]


def _self_calls(scope: ast.AST) -> list[ast.Call]:
    """Every ``self.<method>(...)`` call in ``scope``.

    Closed by construction rather than a list of the methods that fold the
    log: a writer's every call on its own inbox counts, so a read added
    through a method nobody enumerated is still inside or outside the hold.
    """
    return [
        node
        for node in astwalk.own_nodes(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and astwalk.dotted(node.func.value) == "self"
    ]


def _is_control_lock_with(node: ast.AST) -> bool:
    """Whether ``node`` is a ``with control_lock(...):`` block."""
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call)
        and astwalk.leaf_name(item.context_expr.func) == "control_lock"
        for item in node.items
    )


def _held_node_ids(with_node: ast.With) -> set[int]:
    """``id()`` of every node inside ``with_node``'s body."""
    return {id(n) for stmt in with_node.body for n in ast.walk(stmt)}


def _outside_one_hold(scope: ast.AST) -> list[str]:
    """Calls on ``self`` and log writes in ``scope`` not all inside ONE ``with control_lock``."""
    touched = _self_calls(scope)
    touched += [call for call in _log_writes(scope) if call not in touched]
    for node in astwalk.own_nodes(scope):
        if not _is_control_lock_with(node):
            continue
        held = _held_node_ids(node)
        if all(id(call) in held for call in touched):
            return []
    return [ast.unparse(call.func) for call in touched]


def _log_writers(tree: ast.Module) -> dict[str, tuple[ast.AST, int]]:
    """``scope::callee`` -> (scope, count) for every log write in one tree."""
    found: dict[str, tuple[ast.AST, int]] = {}
    for scope, qualified in astwalk.scopes(tree):
        for call in _log_writes(scope):
            key = f"{qualified}::{astwalk.leaf_name(call.func)}"
            found[key] = (scope, found.get(key, (scope, 0))[1] + 1)
    return found


class TestTheInboxFoldsAndWritesInOneHold:
    def test_the_log_writes_are_pinned(self) -> None:
        tree = astwalk.parsed(astwalk.KSTRL_PACKAGE / "inbox.py")
        counts = {key: count for key, (_scope, count) in _log_writers(tree).items()}
        assert counts == EXPECTED_LOG_WRITES, counts

    def test_every_writer_reads_and_writes_inside_one_lock_hold(self) -> None:
        tree = astwalk.parsed(astwalk.KSTRL_PACKAGE / "inbox.py")
        loose = {
            key: _outside_one_hold(scope)
            for key, (scope, _count) in _log_writers(tree).items()
            if not key.startswith(f"{APPEND_PRIMITIVE}::")
        }
        assert all(not calls for calls in loose.values()), (
            "an inbox writer folds or writes outside the control_lock hold it "
            f"decides in, so a concurrent writer's row is lost (#648): {loose}"
        )

    def test_the_net_fires_on_a_read_taken_before_the_lock(self) -> None:
        tree = astwalk.parse(CONTROL_STALE_WRITER)
        ((scope, _count),) = _log_writers(tree).values()
        assert _outside_one_hold(scope) == ["self.find_by_dedupe_key", "self._append_unlocked"]

    def test_the_net_fires_on_a_read_through_a_method_no_list_names(self) -> None:
        tree = astwalk.parse(CONTROL_STALE_WRITER.replace("find_by_dedupe_key", "newest"))
        ((scope, _count),) = _log_writers(tree).values()
        assert _outside_one_hold(scope) == ["self.newest", "self._append_unlocked"]
