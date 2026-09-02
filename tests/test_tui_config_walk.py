"""The mechanism that stops #289's defect class coming back.

Split out of ``test_tui_config_guard.py`` when the file-length ratchet
fired, and the ratchet was right: that file tests what the screens DO
with a broken kstrl.toml, and this one tests a static property of the
source. Two jobs, two files.

The property: inside ``kstrl/tui/``, a call that resolves a config
section must either go through the banner (which routes to
``config_preflight.load_or_report``) or sit in the body of a ``try``
that cannot let the rejection escape to the Textual event loop.

Why a walk and not a review checklist. Every site the #289 survey found
was a hand-written exception tuple that had drifted one exception
narrower than the entry check. Two were missed on the first pass of the
fix. A third, the largest, was missed by the FIRST VERSION OF THIS WALK,
which matched only ``build_config_report(...)`` and
``<Name>Config.load(...)`` and so could not see the seven sections
``assemble_factory_configs`` loads behind a plain name. Reverting that
site left the walk passing. Hence the derivation below, which names no
function at all.

WHERE THE RESOLUTION LIVES NOW
------------------------------
In ``tests/helpers/astwalk.py``, along with the ten other guards' worth
that #324 records. The three-map ``Bindings`` this file used to carry
was the eleventh, and it was holed in the way #324 is a record of: it
never read ``ImportFrom.level`` and its ``ImportFrom`` arm required a
``node.module``, so ``from . import x`` was discarded outright.
Measured on this tree, with the old resolver and a helper set holding
``("kstrl.config_report", "build_config_report")``:

    from .config_report import build_config_report   ->  no offender
    from . import config_report                      ->  no offender

Both are genuine unguarded loads, and both were reported clean. It is
latent rather than live because ``kstrl/`` has no relative import today,
which is exactly how a hole of this class survives.
``test_a_relative_import_resolves_to_the_package_it_names`` is the
control that keeps it closed.

WHAT THE WALK SEES
------------------
``evolution.EvolutionConfig.load(root)`` and any other dotted owner;
``IC.load(root)`` where ``IC`` is an alias for a ``*Config`` class,
resolved through the importing file's own bindings;
``config_report.build_config_report(root)`` and any other helper reached
through a module attribute; a relative import of either; and a load at
MODULE level, which the first version could not report at all because it
only ever walked into function bodies.

Not guessed at: the helper set was once matched by bare name and
contained thirty of them, including ``run``, ``serve``, ``evolve``,
``factory`` and ``__init__``, so any call to a function that happened to
share a name with one was a config load as far as this walk was
concerned. Helpers are keyed by their DOTTED origin now and a call site
resolves through its own imports, so ``run(...)`` counts only when
``run`` is the one kstrl defines and loads config in.

STILL NOT SEEN, DELIBERATELY
----------------------------
``self._cfg_cls.load(root)`` - a loader held in an attribute - needs
type inference, not name resolution, and nothing in kstrl writes that
shape today. Matching every ``.load(`` instead would sweep in
``Manifest.load`` and ``json.load``, which is a rule nobody keeps. The
limit is pinned by
``test_a_loader_held_in_an_attribute_is_a_disclosed_limit``, which runs
under ``xfail(strict=True)``: the day somebody teaches the walk to
resolve it, that row XPASSes and this paragraph has to be edited in the
same diff.

What the walk cannot decide it says so about rather than dropping.
``initial_screens_for_kind(kind, ...)()`` calls whatever that returns,
and the AST holds no name to read, so it is an UNDECIDED row in the
inventory rather than an absence from it.

This is the house pattern: ``test_atomicio`` walks for ``mkstemp``,
``test_process_scoping`` for ``pgrep``, ``test_config_preflight`` for
unenrolled config dataclasses, ``test_prompt_versions`` for unenrolled
``*_PROMPT``.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.helpers import astwalk

# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

#: Handler types that cannot let a config rejection reach the event loop.
#: ``SURFACE_REJECTIONS`` is the rule; a bare ``except Exception`` also
#: satisfies the PROPERTY being enforced, and ``app._safe_mode_worker``
#: uses one deliberately with its reason written next to it.
_NEUTRALISING = frozenset({"SURFACE_REJECTIONS", "Exception", "BaseException"})


def guarding_handler(node: ast.Try) -> bool:
    """Whether this try cannot let a config rejection escape.

    A clause ``astwalk`` could not name yields an empty name set and so
    does NOT neutralise, which reports the load rather than clearing it.
    That is the direction this guard is allowed to be wrong in: a
    spurious offender is a line somebody reads, a missing one is #289.
    """
    return any(clause.names & _NEUTRALISING for clause in astwalk.handler_clauses(node))


def _load_owner(call: ast.Call, table: astwalk.Bindings) -> str | None:
    """What this call's ``load`` is a method of, resolved through imports.

    Falls back to the owner AS WRITTEN when the resolver cannot follow
    it, so ``evolution.EvolutionConfig.load(root)`` in a file that never
    imported ``evolution`` is still recognised by its last segment.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "load":
        return None
    return table.resolve(func.value) or astwalk.dotted(func.value)


def is_config_class_load(call: ast.Call, table: astwalk.Bindings) -> bool:
    """``<anything naming a config class>.load(...)``.

    Resolved first, so ``import InboxConfig as IC`` makes ``IC.load`` a
    config load even though no name-shape rule can see it.
    """
    owner = _load_owner(call, table)
    return owner is not None and owner.rsplit(".", 1)[-1].endswith("Config")


def call_origin(call: ast.Call, table: astwalk.Bindings, module: str) -> str | None:
    """The dotted function a call refers to, as best a file can say.

    A bare name the file never imported is read as this module's own,
    which is what lets a helper call its neighbour without an import.
    """
    origin = table.resolve(call.func)
    if origin is not None:
        return origin
    if isinstance(call.func, ast.Name):
        return f"{module}.{call.func.id}"
    return None


def config_loads(
    nodes: Iterable[ast.AST],
    helpers: frozenset[str],
    table: astwalk.Bindings,
    module: str,
) -> list[ast.Call]:
    """Calls that resolve a kstrl.toml section, directly or via a helper."""
    return [
        node
        for node in nodes
        if isinstance(node, ast.Call)
        and (is_config_class_load(node, table) or call_origin(node, table, module) in helpers)
    ]


def _guarded_call_ids(
    scope: ast.AST,
    helpers: frozenset[str],
    table: astwalk.Bindings,
    module: str,
) -> set[int]:
    """Ids of the config loads a handler in this scope actually covers.

    Only ``Try.body``. Walking the whole ``Try`` node counted a load in
    an ``except`` or ``finally`` block as guarded, which is backwards:
    a fallback of the shape ``try: primary() except SURFACE_REJECTIONS:
    cfg = InboxConfig.load(root)`` runs OUTSIDE any handler, and that
    is precisely the retry shape this rule should police.
    """
    guarded: set[int] = set()
    for node in astwalk.own_nodes(scope):
        if not isinstance(node, ast.Try) or not guarding_handler(node):
            continue
        for statement in node.body:
            guarded.update(
                id(call) for call in config_loads(ast.walk(statement), helpers, table, module)
            )
    return guarded


def unguarded_config_loads(
    tree: ast.Module,
    helpers: frozenset[str],
    module: str = "<test>",
) -> list[tuple[int, str]]:
    """(line, qualified scope) for every unguarded config load."""
    table = astwalk.bindings(tree, module=module)
    found: list[tuple[int, str]] = []
    for scope, qualified in astwalk.scopes(tree):
        guarded = _guarded_call_ids(scope, helpers, table, module)
        found += [
            (call.lineno, qualified)
            for call in config_loads(astwalk.own_nodes(scope), helpers, table, module)
            if id(call) not in guarded
        ]
    return found


def undecided_calls(tree: ast.Module, where: str) -> tuple[str, ...]:
    """Calls whose callee the AST holds no name for, so nothing can decide.

    ``initial_screens_for_kind(kind, ...)()`` is the live shape: the
    thing being called is whatever another call returned. These are
    neither cleared nor flagged, which is why they are pinned as the
    second half of the inventory rather than left out of it.

    No line number, deliberately, unlike the offender rows: an
    unresolvable callee is pinned for as long as it stays unresolvable,
    and a pinned line makes every edit ABOVE one of them a failure of
    this guard. The unparsed callee is what locates it.
    """
    return tuple(
        f"{where} {ast.unparse(node.func)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and astwalk.leaf_name(node.func) is None
    )


# --------------------------------------------------------------------------
# The mechanism, not the memory
# --------------------------------------------------------------------------
# Round one of this walk matched only `build_config_report(...)` and
# `<Name ending in Config>.load(...)`, and review measured the hole:
# reverting kstrl/tui/session.py alone left it PASSING, because that
# site loads seven sections through `assemble_factory_configs`, a plain
# name the pattern could not see. Naming that function in a list would
# be the memory this test exists to replace, so the set of
# config-loading helpers is DERIVED from kstrl/ instead: any function
# whose own body resolves a section is one, and a call to it from
# kstrl/tui/ counts as a config load.


@functools.cache
def config_loading_helpers() -> frozenset[str]:
    """Functions in ``kstrl/`` that can PROPAGATE a config rejection.

    Derived, not listed. A function qualifies when it resolves a
    section and does not guard it, so the rejection reaches its caller.
    `assemble_factory_configs` (seven sections, no try) and
    `build_config_report` both fall out of this; so does whatever the
    next one is called.

    "and does not guard it" is what keeps the rule at the right depth.
    A first version asked only "does it load", which flagged
    `init_wizard.on_mount` and `session.start_run_session` for calling
    helpers that already convert the rejection themselves. The guard
    belongs at the innermost function that can raise, not at every
    caller above it.

    Keyed by DOTTED ORIGIN and restricted to module-level functions,
    which are the only ones another module can call by name. The
    bare-name version of this set held thirty entries including `run`,
    `serve`, `evolve` and `__init__`, so a call to any unrelated
    function with a colliding name was read as a config load.

    Cached, and the cache is the measurement. Four tests in this file
    ask for this set, and each call used to re-read and re-parse all 127
    modules of the package: 1079 ms a call, four calls a session. The
    parse now comes from ``astwalk.parsed`` and the derived set is
    computed once.
    """
    found: set[str] = set()
    for path in astwalk.package_sources():
        tree = astwalk.parsed(path)
        module = astwalk.module_name(path)
        table = astwalk.bindings(tree, module=module)
        found.update(_loading_functions(tree, table, module))
    return frozenset(found)


def _loading_functions(tree: ast.Module, table: astwalk.Bindings, module: str) -> set[str]:
    """The module-level functions of ONE module that let a rejection out.

    Direct loads only, no recursion needed: a helper that only calls
    another helper cannot raise anything the inner one did not let out.
    """
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        guarded = _guarded_call_ids(node, frozenset(), table, module)
        if any(
            id(call) not in guarded
            for call in config_loads(astwalk.own_nodes(node), frozenset(), table, module)
        ):
            found.add(f"{module}.{node.name}")
    return found


def _tui_sources() -> list[Path]:
    """Every module of the surface this rule polices."""
    return sorted((astwalk.KSTRL_PACKAGE / "tui").rglob("*.py"))


#: The sites that resolve config in kstrl/tui/ and are guarded by a
#: mechanism no AST rule can see, keyed by (file, QUALIFIED scope).
#: Qualified because session.py has two `def target()` closures and a
#: bare-name key exempted both while naming one. Unlike the decorative
#: set this replaces, the walk below actually consults it and a second
#: test re-checks its stated reason.
_GUARDED_OFF_THE_EVENT_LOOP = {
    (
        "kstrl/tui/session.py",
        "_prepare_decompose.build.target",
    ): (
        "loads config through commandrun.open_command_run, but runs on "
        "the command thread rather than the event loop: "
        "start_command_thread wraps it in `except BaseException` and "
        "surfaces the error through CommandHandle.error_box"
    ),
}
#: Kept as prose because it is the measurement, not the rule: the
#: bare-name key this replaces read ("kstrl/tui/session.py", "target"),
#: and session.py has TWO `def target()` closures. The one it named,
#: `_prepare_factory.build.target`, resolves no config at all - it calls
#: `run_factory`, and the loading happens inside `_run_factory_locked`
#: below it. The one it actually exempted is the decompose closure
#: above. The exemption's own test passed throughout, because it asked
#: only whether SOME scope called `target` was flagged.

#: Every node in ``kstrl/tui/`` that writes the identifier ``load``.
#:
#: The net under the walk above, and it enumerates no node types: a
#: ``<Config>.load`` cannot happen in a module that never spells
#: ``load``, whatever shape the call takes. That is what makes it see
#: two things the resolver provably cannot, both measured:
#: ``getattr(EvolutionConfig, "load")(root)``, where the method name is
#: a string rather than an ``Attribute``, and ``LOADERS[key](root)`` off
#: a table built with ``EvolutionConfig.load``.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds
#: one is where somebody says which surface now loads a section.
#:
#: It covers the class half only. A helper such as
#: ``assemble_factory_configs`` spells no ``load`` at its call site, and
#: that half is covered by the derived helper set and by
#: ``test_the_walk_derives_the_helper_set_rather_than_listing_it``.
EXPECTED_LOADER_SPELLINGS: dict[str, int] = {
    "tui/screens/evolve.py": 2,  # the banner's load, and EvolutionConfig's
    "tui/screens/home.py": 1,  # Manifest.load
    "tui/screens/inbox.py": 2,  # the banner's load, and InboxConfig's
    "tui/screens/init_wizard.py": 1,  # the banner's load
    "tui/screens/retry.py": 1,  # Manifest.load
    "tui/session.py": 2,  # Manifest.load, and the banner's
    "tui/state.py": 1,  # Manifest.load
    "tui/widgets/config_problem.py": 1,  # the banner's own def load
}


def test_no_tui_surface_loads_config_behind_a_hand_written_guard() -> None:
    """The house pattern is a walk, not a memory (CLAUDE.md).

    Every site the #289 survey found was a hand-written exception tuple
    that had drifted one exception narrower than the entry check, two
    were missed on the first pass of this fix, and a third was missed
    by the first version of this very walk. So the rule is enforced
    rather than remembered: inside kstrl/tui/, a call that resolves a
    config section must either go through the banner's ``load`` (which
    routes to ``config_preflight.load_or_report``) or sit in the body
    of a ``try`` that cannot let the rejection escape.

    Both halves are asserted. ``undecided`` is a claim about what the
    walk could not decide, and an empty ``seen`` next to an unclaimed
    ``undecided`` is what a switched-off walk also returns.
    """
    helpers = config_loading_helpers()
    found = astwalk.Sites()
    for path in _tui_sources():
        rel = astwalk.label(path, astwalk.REPO_ROOT)
        tree = astwalk.parsed(path)
        offenders = tuple(
            f"{rel}:{lineno} in {name}"
            for lineno, name in unguarded_config_loads(tree, helpers, astwalk.module_name(path))
            if (rel, name) not in _GUARDED_OFF_THE_EVENT_LOOP
        )
        found += astwalk.Sites(offenders, undecided_calls(tree, rel))

    astwalk.assert_sites(
        found.sorted(),
        seen=(),
        undecided=(
            "kstrl/tui/app.py initial_screens_for_kind(kind, observe_only=False)",
            "kstrl/tui/app.py initial_screens_for_kind(kind, observe_only=True)",
        ),
        message="config resolved in kstrl/tui/ without a guard or the banner.",
    )


def test_every_tui_module_that_spells_a_loader_is_pinned() -> None:
    """The net: a config class cannot be loaded without the word ``load``.

    Shape-independent, so it does not depend on the resolver being
    right about a call it can see. ``assert_census`` will not pin an
    inventory whose predicate matched nothing in its own control, which
    is what stops this passing while switched off.
    """
    astwalk.assert_census(
        sources=_tui_sources(),
        sees=astwalk.spells("load"),
        expected=EXPECTED_LOADER_SPELLINGS,
        control="cfg = EvolutionConfig.load(root)\n",
        message=(
            "the set of places kstrl/tui/ spells a loader changed. If this is a new "
            "config load, guard it or route it through the banner, then add the row."
        ),
    )


def test_the_walk_derives_the_helper_set_rather_than_listing_it() -> None:
    """The hole review measured: `assemble_factory_configs` is the name
    the first version could not see, and nothing names it here."""
    import inspect

    helpers = config_loading_helpers()
    assert "kstrl.launch.assemble_factory_configs" in helpers
    assert "kstrl.config_report.build_config_report" in helpers
    # A helper that converts the rejection itself is NOT in the set,
    # which is what stops the walk flagging everyone who calls it.
    assert "kstrl.tui.screens.init_wizard._detected_text" not in helpers
    assert "kstrl.tui.session._prepare_factory" not in helpers
    # Derived means derived: the deriving code names no function.
    source = inspect.getsource(config_loading_helpers)
    body = source.split('"""')[2]
    assert "assemble_factory_configs" not in body
    assert "build_config_report" not in body


def test_the_helper_set_is_qualified_rather_than_a_bag_of_bare_names() -> None:
    """Review's measurement: the bare-name set held thirty entries,
    among them `run`, `serve`, `evolve`, `factory` and `__init__`, so a
    call to any unrelated function sharing one of those names read as a
    config load. Keys carry their module now, and an unresolvable name
    from an unrelated module resolves to an origin nothing matches."""
    helpers = config_loading_helpers()
    collisions = {"run", "serve", "evolve", "factory", "feature", "sense", "__init__"}
    assert collisions & {helper.rsplit(".", 1)[-1] for helper in helpers}, (
        "the generic names review measured are gone from kstrl entirely; "
        "this test no longer measures anything"
    )
    # ... and none of them can be hit from a file that did not import
    # the kstrl function of that name.
    innocent = astwalk.parse("def show():\n    run(thing)\n    factory()\n")
    assert unguarded_config_loads(innocent, helpers, "kstrl.tui.screens.made_up") == []


def test_a_relative_import_resolves_to_the_package_it_names() -> None:
    """The hole #324 records this file carrying, with its control.

    The old resolver read ``ImportFrom.module`` and nothing else. A
    ``from .config_report import ...`` therefore resolved to the module
    ``config_report``, which can never match a package-qualified helper
    key, and ``from . import config_report`` was dropped before it was
    looked at. Both are unguarded loads reported clean, which is the
    skip direction. Measured on this tree: with the old resolver both
    snippets below returned no offender at all.
    """
    helpers = frozenset({"kstrl.config_report.build_config_report"})
    from_module = astwalk.parse(
        "from .config_report import build_config_report\n"
        "def show(root):\n"
        "    return build_config_report(root)\n"
    )
    assert unguarded_config_loads(from_module, helpers, "kstrl.made_up") == [(3, "show")]

    bare_package = astwalk.parse(
        "from . import config_report\n"
        "def show(root):\n"
        "    return config_report.build_config_report(root)\n"
    )
    assert unguarded_config_loads(bare_package, helpers, "kstrl.made_up") == [(3, "show")]

    # The level is read, not ignored: two dots from kstrl.tui lands on
    # kstrl, one dot lands on kstrl.tui and matches nothing here.
    two_up = astwalk.parse(
        "from ..config_report import build_config_report\n"
        "def show(root):\n"
        "    return build_config_report(root)\n"
    )
    assert unguarded_config_loads(two_up, helpers, "kstrl.tui.made_up") == [(3, "show")]
    assert unguarded_config_loads(two_up, helpers, "kstrl.made_up") == []


def test_the_off_loop_exemption_still_names_a_real_site_and_a_true_reason() -> None:
    """An allow-list nobody prunes is a lie, so this prunes it.

    Both halves are checked: the exempted site must still be a site the
    walk would otherwise flag, and the reason it is exempt must still
    hold in the code that provides it.
    """
    helpers = config_loading_helpers()
    for rel, scope in _GUARDED_OFF_THE_EVENT_LOOP:
        path = astwalk.REPO_ROOT / rel
        flagged = unguarded_config_loads(
            astwalk.parsed(path),
            helpers,
            astwalk.module_name(path),
        )
        assert any(name == scope for _line, name in flagged), (
            f"{rel}:{scope} no longer loads config unguarded; drop the exemption"
        )
    # The mechanism the exemption rests on.
    bridge = (astwalk.KSTRL_PACKAGE / "tui" / "bridge.py").read_text(encoding="utf-8")
    assert "except BaseException as exc:" in bridge
    assert "error_box.append(exc)" in bridge


def test_the_exemption_key_distinguishes_two_closures_of_the_same_name() -> None:
    """Review's measurement: session.py defines `target` twice, so the
    old (file, bare name) key exempted both while claiming one."""
    session = astwalk.KSTRL_PACKAGE / "tui" / "session.py"
    targets = {
        qualified
        for _scope, qualified in astwalk.scopes(astwalk.parsed(session))
        if qualified.endswith(".target")
    }
    assert len(targets) > 1, "session.py no longer has colliding closures to distinguish"
    assert all(key in targets for _rel, key in _GUARDED_OFF_THE_EVENT_LOOP)


def test_the_walk_would_have_caught_the_original_defect() -> None:
    """The walk is only worth having if it fails on the bugs."""
    helpers = frozenset({"kstrl.launch.assemble_factory_configs"})
    prelude = "from kstrl.launch import assemble_factory_configs\n"

    unguarded = astwalk.parse(
        "def reload(self):\n    journal = EvolutionJournal(EvolutionConfig.load(root_dir))\n"
    )
    assert unguarded_config_loads(unguarded, helpers) == [(2, "reload")]

    # The site the first version of the walk could not see.
    indirect = astwalk.parse(
        prelude + "def prepare(spec, root):\n    return assemble_factory_configs(root)\n"
    )
    assert unguarded_config_loads(indirect, helpers) == [(3, "prepare")]

    guarded = astwalk.parse(
        "def reload(self):\n"
        "    try:\n"
        "        return EvolutionConfig.load(root_dir)\n"
        "    except SURFACE_REJECTIONS:\n"
        "        return None\n"
    )
    assert unguarded_config_loads(guarded, helpers) == []


def test_a_load_in_an_except_block_is_not_counted_as_guarded() -> None:
    """`ast.walk` over a Try yields its handlers too, so the first
    version called this guarded. Nothing catches it."""
    fallback = astwalk.parse(
        "def load(self):\n"
        "    try:\n"
        "        return primary()\n"
        "    except SURFACE_REJECTIONS:\n"
        "        return InboxConfig.load(root)\n"
    )
    assert unguarded_config_loads(fallback, frozenset()) == [(5, "load")]


def test_a_handler_one_exception_narrower_than_the_entry_check_is_not_a_guard() -> None:
    """#289's whole defect class, as one snippet.

    The hand-written tuple is the shape every site the survey found had
    drifted into, and it is the mutation this file is checked against.
    """
    narrowed = astwalk.parse(
        "def reload(self):\n"
        "    try:\n"
        "        return EvolutionConfig.load(root_dir)\n"
        "    except (ValueError, OSError):\n"
        "        return None\n"
    )
    assert unguarded_config_loads(narrowed, frozenset()) == [(3, "reload")]


def test_the_shapes_the_second_version_could_not_see() -> None:
    """Four blind spots review measured, each one line of source.

    Every one of these is a real config load that the walk reported as
    clean, which is the only failure mode that matters for a test whose
    whole job is noticing.
    """
    helpers = frozenset({"kstrl.config_report.build_config_report"})

    dotted_owner = astwalk.parse("def show():\n    return evolution.EvolutionConfig.load(root)\n")
    assert unguarded_config_loads(dotted_owner, helpers) == [(2, "show")]

    aliased_class = astwalk.parse(
        "from kstrl.inbox import InboxConfig as IC\ndef show():\n    return IC.load(root)\n"
    )
    assert unguarded_config_loads(aliased_class, helpers) == [(3, "show")]

    module_attribute = astwalk.parse(
        "from kstrl import config_report\n"
        "def show():\n"
        "    return config_report.build_config_report(root)\n"
    )
    assert unguarded_config_loads(module_attribute, helpers) == [(3, "show")]

    at_import_time = astwalk.parse("REPORT = build_config_report(root)\n")
    assert unguarded_config_loads(at_import_time, helpers, "kstrl.config_report") == [
        (1, "<module>")
    ]


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_loader_held_in_an_attribute_is_a_disclosed_limit() -> None:
    """`self._cfg_cls.load(root)` needs type inference, so it is missed.

    Under ``xfail(strict=True)`` rather than a plain assertion that the
    walk sees nothing: the day somebody teaches it to resolve the
    attribute, this row XPASSes, that is a failure, and the module
    docstring has to be edited in the same diff. A plain
    ``assert ... == []`` would simply start passing for a new reason.
    """
    astwalk.blind_spot(
        lambda source: unguarded_config_loads(astwalk.parse(source), frozenset()),
        "def show(self):\n    return self._cfg_cls.load(root)\n",
    )


def test_the_disclosed_limit_is_written_down_where_a_reader_finds_it() -> None:
    """The other half of the row above, and it was broken.

    This assertion used to read ``assert "..." in __doc__ if __doc__
    else False``, which Python parses as a conditional EXPRESSION: the
    ``assert`` applies to the whole ternary, so the else branch asserts
    the constant ``False``. It passed only because the docstring is
    truthy, and it would have failed rather than skipped had it not
    been.
    """
    assert __doc__ is not None
    assert "self._cfg_cls.load(root)" in __doc__
