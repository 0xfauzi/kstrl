"""Every manifest write site is listed with the lock discipline it runs under (#597).

A live factory run holds ``.kstrl/factory.lock`` and saves its whole
in-memory manifest, so a manifest write another command makes outside
that lock is undone at the run's next save, and one made before a
confirmation is made even when the operator answers Quit. #597 found
four such writers (`ks retry`, `ks inbox retry`, `ks decompose` and
`ks factory --spec`); `tests/test_retry_lock_discipline.py` proves each
one's behaviour. This file is the guard for the fifth: a closed census of
every reference to the names that write or stage a manifest change, so a
new site anywhere in ``kstrl/`` is an unexplained census delta that must
be classified here before it can land.

What the net counts, by construction rather than by a list of shapes:
every ``ast.Attribute`` whose attribute is one of ``WRITE_NAMES`` (a call
``m.save(p)``, an unbound ``Manifest.save(m, p)`` and an alias
``s = m.save`` alike), every ``ast.Name`` of one of them (``prepare_retry``
imported and called bare), and ``getattr(obj, "save")`` for a literal
name that folds to one - closing the blind spot the earlier hand-rolled
walk disclosed, with no new site in ``kstrl/`` today, so the census is
unchanged by adding it. ``save`` is matched on the SPELLING, not the
receiver's type, so non-manifest saves are in the census too and are
classified ``not-a-manifest``: a guess about the receiver would be a
guess that goes blind.

TWO FURTHER CHECKS close the two holes #597's own round found: a hand
written ``takes-lock`` label that nothing verified, and a
``caller-holds-lock`` label whose caller could drift without the table
noticing. Removing ``_inbox_run_lock(`` from ``inbox_retry`` left the
OLD census unchanged (the site being counted is ``reset_for_retry`` and
``save``, not the lock call itself), so a hand-written label was the
whole guard. ``test_every_takes_lock_site_is_preceded_by_its_lock_call``
walks the site's own enclosing scope for a lexical reference to
``_acquire_run_lock``, ``_inbox_run_lock`` or ``held_or_acquired_run_lock``
(the acquire-or-use-the-caller's-lock wrapper `_decompose_spec_impl`
calls) at an earlier line, so deleting the lock call turns the label
into a lie the walk can see.
``test_every_caller_holds_site_is_called_from_a_disciplined_site`` reads
every OTHER row whose write-name is the owning function's own name (its
callers, which the completeness check above already forces into this
table) and requires each one to be ``TAKES_LOCK`` or the one disclosed
``UNGUARDED`` exception, so a caller-holds row cannot rot silently if a
new caller stops taking the lock.

Disclosed blind spot: a manifest file written without ``Manifest.save``
(only ``kstrl/manifest.py`` writes one today) is not a reference this
walk can see.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.astwalk import (
    REPO_ROOT,
    assert_census,
    census,
    folded_str,
    label,
    own_nodes,
    package_sources,
    parsed,
    scope_of,
    scopes,
)

#: ``Manifest.save`` writes the manifest; ``Manifest.reset_for_retry`` stages
#: a requeue that a save then writes; ``prepare_retry`` calls both for its
#: caller, so each caller decides the discipline and is listed itself.
WRITE_NAMES = frozenset({"save", "reset_for_retry", "prepare_retry"})

#: What a ``TAKES_LOCK`` site's enclosing scope must reference, lexically
#: before the site's own line, or the label is unverified (#597).
_LOCK_CALL_NAMES = frozenset({"_acquire_run_lock", "_inbox_run_lock", "held_or_acquired_run_lock"})

IN_RUN = "in-run"  # inside run_factory's lock: _run_factory_locked and what it drives
TAKES_LOCK = "takes-lock"  # takes the run lock before its first change (#597)
CALLER_HOLDS = "caller-holds-lock"  # prepare_retry: its callers are listed below
COPY = "copy"  # a deep copy that is never saved
NOT_A_MANIFEST = "not-a-manifest"
UNGUARDED = "unguarded"  # a disclosed hole; pinned by the last test

#: ``"<path>::<qualified function> <name>"`` -> (references, discipline).
EXPECTED_MANIFEST_WRITE_SITES: dict[str, tuple[int, str]] = {
    # --- outside a factory run: each takes the run lock (#597) ---
    "kstrl/cli.py::retry prepare_retry": (1, TAKES_LOCK),
    "kstrl/cli.py::inbox_retry reset_for_retry": (1, TAKES_LOCK),
    "kstrl/cli.py::inbox_retry save": (1, TAKES_LOCK),
    "kstrl/decompose.py::_decompose_spec_impl save": (1, TAKES_LOCK),
    "kstrl/retry_plan.py::prepare_retry reset_for_retry": (1, CALLER_HOLDS),
    "kstrl/retry_plan.py::prepare_retry save": (1, CALLER_HOLDS),
    "kstrl/retry_plan.py::preview_retry reset_for_retry": (1, COPY),
    # The TUI retry screen resets and saves, then launches a factory that
    # takes the lock itself. Not fixed in #597 (UI work deferred by the owner).
    "kstrl/tui/screens/retry.py::RetryScreen._confirm_retry prepare_retry": (1, UNGUARDED),
    # --- inside run_factory's lock ---
    "kstrl/factory.py::_run_factory_locked save": (6, IN_RUN),
    "kstrl/factory.py::_run_factory_locked._cleanup_pass_worktrees save": (1, IN_RUN),
    "kstrl/factory.py::_run_factory_locked._run_scheduling_pass save": (1, IN_RUN),
    "kstrl/factory.py::_stamp_feature_base save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._fail_pr_flow save": (1, IN_RUN),
    "kstrl/plan_gate.py::_settle save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._park_awaiting_approval save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._park_merge_pending save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._phase_pr save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._record_merge save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.complete save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.fail save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.fail_scheduler_backstop save": (1, IN_RUN),
    # #646: the gates after the engineer moved out of process_result, so
    # ks retry's kept head is judged by the same code with no engineer.
    "kstrl/pipeline.py::ComponentPipeline._judge_attempt save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.repoll_merge_pending save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.retry_or_fail save": (1, IN_RUN),
    # --- a ``save`` that is not a manifest's ---
    "kstrl/autonomy.py::commit_transition save": (1, NOT_A_MANIFEST),  # ladder state
    "kstrl/autonomy.py::save_ladder_state save": (1, NOT_A_MANIFEST),  # ladder state
    "kstrl/integration.py::write_integration_prd save": (1, NOT_A_MANIFEST),  # PRD
    "kstrl/pipeline.py::ComponentPipeline._phase_review save": (1, NOT_A_MANIFEST),  # PRD
    "kstrl/serve.py::_save_pr_count_streak save": (1, NOT_A_MANIFEST),  # serve streak
}


def _name_at(node: ast.AST) -> str:
    """The write-name a matched node spells, for a node :func:`writes` accepted."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        folded = folded_str(node.args[1])
        assert folded is not None, f"a getattr call {ast.unparse(node)} folded to no name"
        return folded
    raise AssertionError(f"unexpected node type for a write site: {ast.dump(node)}")


def writes(node: ast.AST) -> bool:
    """Does this node reference one of ``WRITE_NAMES``?

    Three shapes, none of them a node-type enumeration of "every way to
    call something": an attribute access, a bare name, and
    ``getattr(obj, "save")`` for a literal second argument that folds to
    one - the shape the earlier hand-rolled walk disclosed as a blind
    spot and never closed.
    """
    if isinstance(node, ast.Attribute):
        return node.attr in WRITE_NAMES
    if isinstance(node, ast.Name):
        return node.id in WRITE_NAMES
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
    ):
        return folded_str(node.args[1]) in WRITE_NAMES
    return False


def census_key(source_file: Path, node: ast.AST) -> str:
    """``<path>::<qualified function> <name>``, matching the old hand-rolled walk exactly."""
    owner = scope_of(parsed(source_file))
    scope = owner.get(id(node), "<module>")
    return f"{label(source_file, root=REPO_ROOT)}::{scope} {_name_at(node)}"


def _parse_key(key: str) -> tuple[str, str, str]:
    module, _, rest = key.partition("::")
    scope, _, name = rest.rpartition(" ")
    return module, scope, name


def _is_lock_reference(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in _LOCK_CALL_NAMES
    if isinstance(node, ast.Name):
        return node.id in _LOCK_CALL_NAMES
    return False


def _scope_body(path: Path | None, scope: str) -> list[ast.AST]:
    """The nodes owned by one qualified scope of one module, or ``[]`` if either is missing."""
    if path is None:
        return []
    owned = {qualified: own_nodes(node) for node, qualified in scopes(parsed(path))}
    return owned.get(scope, [])


def _lock_precedes_every_reference(body: list[ast.AST], name: str) -> bool:
    """Does every reference to ``name`` in ``body`` have a lock call at an earlier line?"""
    site_lines = [node.lineno for node in body if writes(node) and _name_at(node) == name]
    lock_lines = [node.lineno for node in body if _is_lock_reference(node)]
    return bool(site_lines) and all(any(ll < sl for ll in lock_lines) for sl in site_lines)


def test_the_census_matches_the_tree() -> None:
    sources = package_sources()
    found = dict(census(sources, writes, key=census_key))
    expected = {site: count for site, (count, _) in EXPECTED_MANIFEST_WRITE_SITES.items()}
    new = {site: n for site, n in found.items() if site not in expected}
    gone = sorted(site for site in expected if site not in found)
    moved = {s: (expected[s], found[s]) for s in expected if s in found and found[s] != expected[s]}
    assert (new, gone, moved) == ({}, [], {}), (
        "A manifest write site changed. Classify each new one in "
        "EXPECTED_MANIFEST_WRITE_SITES with the lock it runs under: a write "
        "outside a factory run takes the run lock before its first change "
        f"(#597). new={new} gone={gone} (expected, found)={moved}"
    )


def test_the_census_predicate_is_not_switched_off() -> None:
    """The net fires on all three shapes, proved against synthetic controls (#324)."""
    sources = package_sources()
    expected = {site: count for site, (count, _) in EXPECTED_MANIFEST_WRITE_SITES.items()}
    assert_census(
        sources=sources,
        sees=writes,
        key=census_key,
        expected=expected,
        control=(
            "m.save(p)\n",
            "prepare_retry(m, 'c', p, p, None)\n",
            "getattr(m, 'save')(p)\n",
        ),
        message="A manifest write site changed.",
    )


def test_the_walk_sees_every_shape_of_reference() -> None:
    """Positive control: a walk that stops seeing a shape fails here, not silently."""
    source = (
        "from kstrl.retry_plan import prepare_retry\n"
        "import kstrl.retry_plan as rp\n"
        "class Screen:\n"
        "    def confirm(self, m, p):\n"
        "        m.save(p)\n"
        "        Manifest.save(m, p)\n"
        "        alias = m.save\n"
        "        m.reset_for_retry('c')\n"
        "        prepare_retry(m, 'c', p, p, None)\n"
        "        rp.prepare_retry(m, 'c', p, p, None)\n"
        "        getattr(m, 'save')(p)\n"
        "def top():\n"
        "    def inner(m, p):\n"
        "        m.save(p)\n"
    )
    tree = ast.parse(source)
    owner = scope_of(tree)
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not writes(node):
            continue
        scope = owner.get(id(node), "<module>")
        key = f"kstrl/x.py::{scope} {_name_at(node)}"
        found[key] = found.get(key, 0) + 1

    assert found == {
        "kstrl/x.py::Screen.confirm save": 4,
        "kstrl/x.py::Screen.confirm reset_for_retry": 1,
        "kstrl/x.py::Screen.confirm prepare_retry": 2,
        "kstrl/x.py::top.inner save": 1,
    }


def test_the_only_unguarded_site_is_the_disclosed_tui_retry() -> None:
    unguarded = {s for s, (_, d) in EXPECTED_MANIFEST_WRITE_SITES.items() if d == UNGUARDED}
    assert unguarded == {"kstrl/tui/screens/retry.py::RetryScreen._confirm_retry prepare_retry"}


def test_every_takes_lock_site_is_preceded_by_its_lock_call() -> None:
    """A ``TAKES_LOCK`` label is not a control until something checks it (#597).

    Removing ``_inbox_run_lock(`` from ``inbox_retry`` left the write-site
    census unchanged (the counted references are ``reset_for_retry`` and
    ``save``, not the lock call), so the hand-written label was the whole
    guard. This walks each labelled site's own enclosing scope for a
    lexical reference to ``_acquire_run_lock``, ``_inbox_run_lock`` or
    ``held_or_acquired_run_lock`` at an earlier line, so deleting the
    lock call turns the label into something this test can see is false.
    """
    modules = {label(path, root=REPO_ROOT): path for path in package_sources()}
    rows = [
        _parse_key(key)
        for key, (_count, discipline) in EXPECTED_MANIFEST_WRITE_SITES.items()
        if discipline == TAKES_LOCK
    ]
    assert rows, "no TAKES_LOCK row in the table; nothing to check"

    violations = [
        f"{module}::{scope} {name}"
        for module, scope, name in rows
        if not _lock_precedes_every_reference(_scope_body(modules.get(module), scope), name)
    ]
    assert violations == [], (
        "these TAKES_LOCK sites have no lock-acquiring call before them in their own "
        f"scope, so the label is unverified: {violations}"
    )


def test_every_caller_holds_site_is_called_from_a_disciplined_site() -> None:
    """A ``CALLER_HOLDS`` row is only as true as its caller (#597).

    Every call site of a ``CALLER_HOLDS`` function's own name is already
    forced into this table by ``test_the_census_matches_the_tree`` (it is
    itself a ``WRITE_NAMES`` reference), so this only has to check each
    one's discipline: ``TAKES_LOCK``, or the one disclosed ``UNGUARDED``
    exception the last test pins.
    """
    owning_functions = {
        _parse_key(key)[1].rsplit(".", 1)[-1]
        for key, (_count, discipline) in EXPECTED_MANIFEST_WRITE_SITES.items()
        if discipline == CALLER_HOLDS
    }
    assert owning_functions, "no CALLER_HOLDS row in the table; nothing to check"

    bad = {
        key: discipline
        for key, (_count, discipline) in EXPECTED_MANIFEST_WRITE_SITES.items()
        if _parse_key(key)[2] in owning_functions and discipline not in (TAKES_LOCK, UNGUARDED)
    }
    assert bad == {}, f"a caller of a CALLER_HOLDS function does not hold the lock: {bad}"
