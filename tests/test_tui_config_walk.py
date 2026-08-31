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

WHAT THE SECOND VERSION COULD NOT SEE, AND WHAT THIS ONE CAN
------------------------------------------------------------
Review measured five more blind spots and one false-positive engine,
and the fix for all six is the same: resolve names instead of matching
strings.

Now seen: ``evolution.EvolutionConfig.load(root)`` and any other dotted
owner; ``IC.load(root)`` where ``IC`` is an alias for a ``*Config``
class, resolved through the importing file's own bindings;
``config_report.build_config_report(root)`` and any other helper reached
through a module attribute; and a load at MODULE level, which the
previous version could not report at all because it only ever walked
into function bodies.

Now not guessed at: the helper set was matched by bare name and
contained thirty of them, including ``run``, ``serve``, ``evolve``,
``factory`` and ``__init__``, so any call to a function that happened to
share a name with one was a config load as far as this walk was
concerned. Helpers are now keyed by ``(module, name)`` and a call site
resolves through its own imports, so ``run(...)`` counts only when
``run`` is the one kstrl defines and loads config in.

STILL NOT SEEN, DELIBERATELY
----------------------------
``self._cfg_cls.load(root)`` - a loader held in an attribute - needs
type inference, not name resolution, and nothing in kstrl writes that
shape today. Matching every ``.load(`` instead would sweep in
``Manifest.load`` and ``json.load``, which is a rule nobody keeps. The
limit is recorded here rather than left to be discovered.

This is the house pattern: ``test_atomicio`` walks for ``mkstemp``,
``test_process_scoping`` for ``pgrep``, ``test_config_preflight`` for
unenrolled config dataclasses, ``test_prompt_versions`` for unenrolled
``*_PROMPT``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef

# --------------------------------------------------------------------------
# Name resolution: what a call site in one file is actually calling
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Bindings:
    """What the names in one file refer to, from its own imports.

    Three maps rather than one because a call site can name a helper
    (``assemble_factory_configs(...)``), a module holding one
    (``config_report.build_config_report(...)``) or a config class
    under any alias (``IC.load(...)``), and each needs a different
    lookup. Populated from every ``import`` in the file at ANY depth:
    kstrl's TUI modules import inside functions to keep startup cheap,
    so a module-level-only scan sees almost none of them.
    """

    functions: dict[str, tuple[str, str]] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)
    config_classes: set[str] = field(default_factory=set)


def _is_config_class(name: str) -> bool:
    return name.endswith("Config")


def _bind_from_import(module: str, names: list[ast.alias], bindings: Bindings) -> None:
    """Record one ``from <module> import ...`` under its local names."""
    for alias in names:
        local = alias.asname or alias.name
        # An imported name may be a function or a submodule and the AST
        # cannot tell which, so record both readings. A wrong one cannot
        # produce a false hit: it only ever fails to match the derived
        # helper set.
        bindings.functions[local] = (module, alias.name)
        bindings.modules[local] = f"{module}.{alias.name}"
        if _is_config_class(alias.name):
            # Keyed by the LOCAL name, which is the point: `import
            # InboxConfig as IC` makes `IC.load` a config load and no
            # name-shape rule can see it.
            bindings.config_classes.add(local)


def collect_bindings(tree: ast.AST) -> Bindings:
    """Resolve the file's imported names to their defining module."""
    bindings = Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings.modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            _bind_from_import(node.module, node.names, bindings)
    return bindings


def dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` for an attribute chain rooted at a plain name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def is_config_class_load(call: ast.Call, bindings: Bindings) -> bool:
    """``<anything naming a config class>.load(...)``.

    The owner is flattened first, so a dotted module prefix is stripped
    rather than being the reason the call is missed.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "load":
        return False
    owner = dotted_name(func.value)
    if owner is None:
        return False
    last = owner.rsplit(".", 1)[-1]
    return _is_config_class(last) or last in bindings.config_classes


def call_target(call: ast.Call, bindings: Bindings, module: str) -> tuple[str, str] | None:
    """The ``(module, name)`` a call refers to, as best a file can say."""
    func = call.func
    if isinstance(func, ast.Name):
        return bindings.functions.get(func.id, (module, func.id))
    dotted = dotted_name(func)
    if dotted is None or "." not in dotted:
        return None
    prefix, name = dotted.rsplit(".", 1)
    return (bindings.modules.get(prefix, prefix), name)


# --------------------------------------------------------------------------
# Scopes: the unit a load is attributed to
# --------------------------------------------------------------------------


def module_name(path: Path, root: Path) -> str:
    """``kstrl/tui/session.py`` -> ``kstrl.tui.session``."""
    dotted = path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
    return dotted.removesuffix(".__init__")


def scopes(tree: ast.Module) -> list[tuple[Scope, str]]:
    """Every executable scope in a module, with its qualified name.

    The module itself is one of them: a load at import time is a load,
    and the previous version of this walk started at ``FunctionDef`` and
    so could not report one. Qualified because a bare function name is
    not a key - ``kstrl/tui/session.py`` has two ``def target()``
    closures, and an exemption written against "target" silently
    covered both.
    """
    found: list[tuple[Scope, str]] = [(tree, "<module>")]

    def descend(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                qualified = f"{prefix}.{child.name}" if prefix else child.name
                if not isinstance(child, ast.ClassDef):
                    found.append((child, qualified))
                descend(child, qualified)
            else:
                descend(child, prefix)

    descend(tree, "")
    return found


def own_nodes(scope: Scope) -> Iterator[ast.AST]:
    """Every node in ``scope`` except those inside a nested function.

    So a call is attributed to the innermost scope that contains it.
    Plain ``ast.walk`` reported one call three times, once per ancestor,
    which makes the offender list wrong and an exemption unkeyable.
    """
    stack: list[ast.AST] = list(scope.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def guarding_handler(node: ast.Try) -> bool:
    """Whether this try cannot let a config rejection escape.

    ``SURFACE_REJECTIONS`` is the rule. A bare ``except Exception`` also
    satisfies the PROPERTY being enforced - nothing escapes as an
    unhandled exception - and `app._safe_mode_worker` uses one
    deliberately, with its reason written next to it.
    """
    for handler in node.handlers:
        if handler.type is None:
            return True
        for name in ast.walk(handler.type):
            if isinstance(name, ast.Name) and name.id in {
                "SURFACE_REJECTIONS",
                "Exception",
                "BaseException",
            }:
                return True
    return False


def config_loads(
    nodes: Iterable[ast.AST],
    helpers: frozenset[tuple[str, str]],
    bindings: Bindings,
    module: str,
) -> list[ast.Call]:
    """Calls that resolve a kstrl.toml section, directly or via a helper."""
    found: list[ast.Call] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        if is_config_class_load(node, bindings) or call_target(node, bindings, module) in helpers:
            found.append(node)
    return found


def _guarded_call_ids(
    scope: Scope,
    helpers: frozenset[tuple[str, str]],
    bindings: Bindings,
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
    for node in own_nodes(scope):
        if not isinstance(node, ast.Try) or not guarding_handler(node):
            continue
        for statement in node.body:
            guarded.update(
                id(call) for call in config_loads(ast.walk(statement), helpers, bindings, module)
            )
    return guarded


def unguarded_config_loads(
    tree: ast.Module,
    helpers: frozenset[tuple[str, str]],
    module: str = "<test>",
) -> list[tuple[int, str]]:
    """(line, qualified scope) for every unguarded config load."""
    bindings = collect_bindings(tree)
    found: list[tuple[int, str]] = []
    for scope, qualified in scopes(tree):
        guarded = _guarded_call_ids(scope, helpers, bindings, module)
        found += [
            (call.lineno, qualified)
            for call in config_loads(own_nodes(scope), helpers, bindings, module)
            if id(call) not in guarded
        ]
    return found


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


def config_loading_helpers(package: Path, root: Path) -> frozenset[tuple[str, str]]:
    """Functions in ``package`` that can PROPAGATE a config rejection.

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

    Keyed by ``(module, name)`` and restricted to module-level
    functions, which are the only ones another module can call by name.
    The bare-name version of this set held thirty entries including
    `run`, `serve`, `evolve` and `__init__`, so a call to any unrelated
    function with a colliding name was read as a config load.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = module_name(path, root)
        bindings = collect_bindings(tree)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            # Direct loads only, no recursion needed - a helper that
            # only calls another helper cannot raise anything the inner
            # one did not already let out.
            guarded = _guarded_call_ids(node, frozenset(), bindings, module)
            if any(
                id(call) not in guarded
                for call in config_loads(own_nodes(node), frozenset(), bindings, module)
            ):
                found.add((module, node.name))
    return frozenset(found)


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
    """
    root = Path(__file__).resolve().parent.parent
    helpers = config_loading_helpers(root / "kstrl", root)
    offenders = [
        f"{rel}:{lineno} in {name}"
        for path in sorted((root / "kstrl" / "tui").rglob("*.py"))
        for rel in [path.relative_to(root).as_posix()]
        for lineno, name in unguarded_config_loads(
            ast.parse(path.read_text(encoding="utf-8")),
            helpers,
            module_name(path, root),
        )
        if (rel, name) not in _GUARDED_OFF_THE_EVENT_LOOP
    ]
    assert offenders == [], (
        "config resolved in kstrl/tui/ without a guard or the banner: " + ", ".join(offenders)
    )


def test_the_walk_derives_the_helper_set_rather_than_listing_it() -> None:
    """The hole review measured: `assemble_factory_configs` is the name
    the first version could not see, and nothing names it here."""
    import inspect

    root = Path(__file__).resolve().parent.parent
    helpers = config_loading_helpers(root / "kstrl", root)
    assert ("kstrl.launch", "assemble_factory_configs") in helpers
    assert ("kstrl.config_report", "build_config_report") in helpers
    # A helper that converts the rejection itself is NOT in the set,
    # which is what stops the walk flagging everyone who calls it.
    assert ("kstrl.tui.screens.init_wizard", "_detected_text") not in helpers
    assert ("kstrl.tui.session", "_prepare_factory") not in helpers
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
    from an unrelated module resolves to a pair nothing matches."""
    root = Path(__file__).resolve().parent.parent
    helpers = config_loading_helpers(root / "kstrl", root)
    collisions = {"run", "serve", "evolve", "factory", "feature", "sense", "__init__"}
    assert collisions & {name for _module, name in helpers}, (
        "the generic names review measured are gone from kstrl entirely; "
        "this test no longer measures anything"
    )
    # ... and none of them can be hit from a file that did not import
    # the kstrl function of that name.
    innocent = ast.parse("def show():\n    run(thing)\n    factory()\n")
    assert unguarded_config_loads(innocent, helpers, "kstrl.tui.screens.made_up") == []


def test_the_off_loop_exemption_still_names_a_real_site_and_a_true_reason() -> None:
    """An allow-list nobody prunes is a lie, so this prunes it.

    Both halves are checked: the exempted site must still be a site the
    walk would otherwise flag, and the reason it is exempt must still
    hold in the code that provides it.
    """
    root = Path(__file__).resolve().parent.parent
    helpers = config_loading_helpers(root / "kstrl", root)
    for rel, scope in _GUARDED_OFF_THE_EVENT_LOOP:
        path = root / rel
        flagged = unguarded_config_loads(
            ast.parse(path.read_text(encoding="utf-8")),
            helpers,
            module_name(path, root),
        )
        assert any(name == scope for _line, name in flagged), (
            f"{rel}:{scope} no longer loads config unguarded; drop the exemption"
        )
    # The mechanism the exemption rests on.
    bridge = (root / "kstrl" / "tui" / "bridge.py").read_text(encoding="utf-8")
    assert "except BaseException as exc:" in bridge
    assert "error_box.append(exc)" in bridge


def test_the_exemption_key_distinguishes_two_closures_of_the_same_name() -> None:
    """Review's measurement: session.py defines `target` twice, so the
    old (file, bare name) key exempted both while claiming one."""
    root = Path(__file__).resolve().parent.parent
    session = root / "kstrl" / "tui" / "session.py"
    targets = {
        name
        for name in (
            qualified for _scope, qualified in scopes(ast.parse(session.read_text("utf-8")))
        )
        if name.endswith(".target")
    }
    assert len(targets) > 1, "session.py no longer has colliding closures to distinguish"
    assert all(key in targets for _rel, key in _GUARDED_OFF_THE_EVENT_LOOP)


def test_the_walk_would_have_caught_the_original_defect() -> None:
    """The walk is only worth having if it fails on the bugs."""
    helpers = frozenset({("kstrl.launch", "assemble_factory_configs")})
    prelude = "from kstrl.launch import assemble_factory_configs\n"

    unguarded = ast.parse(
        "def reload(self):\n    journal = EvolutionJournal(EvolutionConfig.load(root_dir))\n"
    )
    assert unguarded_config_loads(unguarded, helpers) == [(2, "reload")]

    # The site the first version of the walk could not see.
    indirect = ast.parse(
        prelude + "def prepare(spec, root):\n    return assemble_factory_configs(root)\n"
    )
    assert unguarded_config_loads(indirect, helpers) == [(3, "prepare")]

    guarded = ast.parse(
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
    fallback = ast.parse(
        "def load(self):\n"
        "    try:\n"
        "        return primary()\n"
        "    except SURFACE_REJECTIONS:\n"
        "        return InboxConfig.load(root)\n"
    )
    assert unguarded_config_loads(fallback, frozenset()) == [(5, "load")]


def test_the_shapes_the_second_version_could_not_see() -> None:
    """Four blind spots review measured, each one line of source.

    Every one of these is a real config load that the walk reported as
    clean, which is the only failure mode that matters for a test whose
    whole job is noticing.
    """
    helpers = frozenset({("kstrl.config_report", "build_config_report")})

    dotted_owner = ast.parse("def show():\n    return evolution.EvolutionConfig.load(root)\n")
    assert unguarded_config_loads(dotted_owner, helpers) == [(2, "show")]

    aliased_class = ast.parse(
        "from kstrl.inbox import InboxConfig as IC\ndef show():\n    return IC.load(root)\n"
    )
    assert unguarded_config_loads(aliased_class, helpers) == [(3, "show")]

    module_attribute = ast.parse(
        "from kstrl import config_report\n"
        "def show():\n"
        "    return config_report.build_config_report(root)\n"
    )
    assert unguarded_config_loads(module_attribute, helpers) == [(3, "show")]

    at_import_time = ast.parse("REPORT = build_config_report(root)\n")
    assert unguarded_config_loads(at_import_time, helpers, "kstrl.config_report") == [
        (1, "<module>")
    ]


def test_an_unresolvable_loader_attribute_is_a_stated_limit_not_a_silent_one() -> None:
    """`self._cfg_cls.load(root)` needs type inference, so it is missed.

    Pinned rather than left to be discovered: if someone teaches the
    walk to resolve it, this test is the one that says so, and the
    module docstring is what has to change with it.
    """
    held_in_an_attribute = ast.parse("def show(self):\n    return self._cfg_cls.load(root)\n")
    assert unguarded_config_loads(held_in_an_attribute, frozenset()) == []
    assert "self._cfg_cls.load(root)" in __doc__ if __doc__ else False
