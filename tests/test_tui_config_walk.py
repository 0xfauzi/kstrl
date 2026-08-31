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

This is the house pattern: ``test_atomicio`` walks for ``mkstemp``,
``test_process_scoping`` for ``pgrep``, ``test_config_preflight`` for
unenrolled config dataclasses, ``test_prompt_versions`` for unenrolled
``*_PROMPT``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from pathlib import Path

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


def _config_load_attr(node: ast.AST) -> bool:
    """``<Something>Config.load(...)``, the shape every loader has."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "load"
        and isinstance(func.value, ast.Name)
        and func.value.id.endswith("Config")
    )


def _config_loading_helpers(package: Path) -> frozenset[str]:
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
    """
    names: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            # frozenset(): direct loads only, no recursion needed - a
            # helper that only calls another helper cannot raise
            # anything the inner one did not already let out.
            if _unguarded_config_loads(function, frozenset()):
                names.add(function.name)
    return frozenset(names)


def _guarding_handler(node: ast.Try) -> bool:
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


def _own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """Every node in ``function`` except those inside a nested function.

    So a call is attributed to the innermost function that contains it.
    Plain ``ast.walk`` reported one call three times, once per ancestor,
    which makes the offender list wrong and an exemption unkeyable.
    """
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _config_loads(nodes: Iterable[ast.AST], helpers: frozenset[str]) -> list[ast.Call]:
    """Calls that resolve a kstrl.toml section, directly or via a helper."""
    found: list[ast.Call] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        if _config_load_attr(node):
            found.append(node)
        elif isinstance(node.func, ast.Name) and node.func.id in helpers:
            found.append(node)
    return found


def _guarded_call_ids(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    helpers: frozenset[str],
) -> set[int]:
    """Ids of the config loads a handler on this function actually covers.

    Only ``Try.body``. Walking the whole ``Try`` node counted a load in
    an ``except`` or ``finally`` block as guarded, which is backwards:
    a fallback of the shape ``try: primary() except SURFACE_REJECTIONS:
    cfg = InboxConfig.load(root)`` runs OUTSIDE any handler, and that
    is precisely the retry shape this rule should police.
    """
    guarded: set[int] = set()
    for node in _own_nodes(function):
        if not isinstance(node, ast.Try) or not _guarding_handler(node):
            continue
        for statement in node.body:
            guarded.update(id(call) for call in _config_loads(ast.walk(statement), helpers))
    return guarded


def _unguarded_config_loads(tree: ast.AST, helpers: frozenset[str]) -> list[tuple[int, str]]:
    """(line, enclosing function) for every unguarded config load."""
    found: list[tuple[int, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        guarded = _guarded_call_ids(function, helpers)
        found += [
            (call.lineno, function.name)
            for call in _config_loads(_own_nodes(function), helpers)
            if id(call) not in guarded
        ]
    return found


#: The one site that resolves config in kstrl/tui/ and is guarded by a
#: mechanism no AST rule can see, keyed by (file, innermost function).
#: Unlike the decorative set this replaces, the walk below actually
#: consults it and a second test re-checks its stated reason.
_GUARDED_OFF_THE_EVENT_LOOP = {
    (
        "kstrl/tui/session.py",
        "target",
    ): (
        "runs on the command thread, not the event loop: "
        "start_command_thread wraps it in `except BaseException` and "
        "surfaces the error through CommandHandle.error_box"
    ),
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
    """
    root = Path(__file__).resolve().parent.parent
    helpers = _config_loading_helpers(root / "kstrl") - {"load", "load_or_report"}
    offenders = [
        f"{rel}:{lineno} in {name}()"
        for path in sorted((root / "kstrl" / "tui").rglob("*.py"))
        for rel in [path.relative_to(root).as_posix()]
        for lineno, name in _unguarded_config_loads(
            ast.parse(path.read_text(encoding="utf-8")), helpers
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
    helpers = _config_loading_helpers(root / "kstrl")
    assert "assemble_factory_configs" in helpers
    assert "build_config_report" in helpers
    # A helper that converts the rejection itself is NOT in the set,
    # which is what stops the walk flagging everyone who calls it.
    assert "_detected_text" not in helpers
    assert "_prepare_factory" not in helpers
    # Derived means derived: the deriving code names no function.
    source = inspect.getsource(_config_loading_helpers)
    body = source.split('"""')[2]
    assert "assemble_factory_configs" not in body
    assert "build_config_report" not in body


def test_the_off_loop_exemption_still_names_a_real_site_and_a_true_reason() -> None:
    """An allow-list nobody prunes is a lie, so this prunes it.

    Both halves are checked: the exempted site must still be a site the
    walk would otherwise flag, and the reason it is exempt must still
    hold in the code that provides it.
    """
    root = Path(__file__).resolve().parent.parent
    helpers = _config_loading_helpers(root / "kstrl")
    for rel, function in _GUARDED_OFF_THE_EVENT_LOOP:
        flagged = _unguarded_config_loads(
            ast.parse((root / rel).read_text(encoding="utf-8")), helpers
        )
        assert any(name == function for _line, name in flagged), (
            f"{rel}:{function} no longer loads config unguarded; drop the exemption"
        )
    # The mechanism the exemption rests on.
    bridge = (root / "kstrl" / "tui" / "bridge.py").read_text(encoding="utf-8")
    assert "except BaseException as exc:" in bridge
    assert "error_box.append(exc)" in bridge


def test_the_walk_would_have_caught_the_original_defect() -> None:
    """The walk is only worth having if it fails on the bugs."""
    helpers = frozenset({"assemble_factory_configs"})

    unguarded = ast.parse(
        "def reload(self):\n    journal = EvolutionJournal(EvolutionConfig.load(root_dir))\n"
    ).body[0]
    assert isinstance(unguarded, ast.FunctionDef)
    assert len(_config_loads(_own_nodes(unguarded), helpers)) == 1

    # The site the first version of the walk could not see.
    indirect = ast.parse(
        "def prepare(spec, root):\n    return assemble_factory_configs(root)\n"
    ).body[0]
    assert isinstance(indirect, ast.FunctionDef)
    assert _unguarded_config_loads(indirect, helpers) == [(2, "prepare")]

    guarded = ast.parse(
        "def reload(self):\n"
        "    try:\n"
        "        return EvolutionConfig.load(root_dir)\n"
        "    except SURFACE_REJECTIONS:\n"
        "        return None\n"
    ).body[0]
    assert isinstance(guarded, ast.FunctionDef)
    assert _unguarded_config_loads(guarded, helpers) == []


def test_a_load_in_an_except_block_is_not_counted_as_guarded() -> None:
    """`ast.walk` over a Try yields its handlers too, so the first
    version called this guarded. Nothing catches it."""
    fallback = ast.parse(
        "def load(self):\n"
        "    try:\n"
        "        return primary()\n"
        "    except SURFACE_REJECTIONS:\n"
        "        return InboxConfig.load(root)\n"
    ).body[0]
    assert isinstance(fallback, ast.FunctionDef)
    assert _unguarded_config_loads(fallback, frozenset()) == [(5, "load")]
