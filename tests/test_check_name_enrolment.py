"""Every check and signature prefix is enrolled in the category map.

``evolution.category_for_check`` maps a name onto a
``FailurePattern.category`` and falls through to ``"iteration"`` for
anything the table does not carry, which files a mechanical verification
gate in the journal under the engineer loop. Enrolment in
``_CATEGORY_BY_CHECK`` was a convention with no mechanism, and measured
during #294 the convention did not hold.

This is the mechanism, in the shape the repo already uses three times
(``tests/test_prompt_versions.py``, ``tests/test_atomicio.py``,
``tests/test_process_scoping.py``): AST-walk ``kstrl/`` for every name
that can reach ``category_for_check`` and fail on one the table does not
carry.

Two things the walk learned the hard way, both from its own tests:

- It resolves module-level string constants, not just literals, across
  module boundaries but only where a name means one thing package-wide.
  The
  first version matched literals only and its anti-vacuity test caught
  that it saw NONE of ``test_suite``, ``typecheck`` or ``linter``: those
  name themselves through ``gateparse.GATE_TEST`` and reach
  ``CheckResult`` via ``verify._failed_gate_result``. A guard that
  misses the three most important gates is worse than no guard, because
  it reads as covered. Annotated constants (``NAME: str = "..."``) count
  too, because ``gateparse`` already uses that form.
- It reads ``signatures=["<prefix>:<code>"]`` literals as well as check
  names. Those prefixes go through the same ``split_signature`` ->
  ``category_for_check`` path, and five of them were unenrolled while
  the first version of this module claimed "no NEW check can join it
  quietly". That claim was false for every failure recorded outside a
  ``CheckResult``.

What the walk CANNOT see, stated rather than left as a silent gap:
``evolution.signatures_from_findings`` composes ``"<phase>:<category>"``
from a runtime ``phase`` argument, so the ``security`` and ``contract``
prefixes appear in no literal anywhere in ``kstrl/``, and neither does
``verification``, which only ever comes back out of
``evolution._classify_check``. They are enrolled today, and
:data:`ENROLLED_BUT_INVISIBLE` plus
:meth:`TestEveryCheckNameIsEnrolled.test_the_walk_covers_every_enrolled_name_but_these`
say so by measurement rather than by claim.

That second test is the one aimed at this repo's dominant defect (#324):
a guard that goes BLIND rather than red. Every miss logged there is in
the skip direction - a refactor moves a name out of the shape the
matcher recognises, the matcher sees fewer sites and reports clean. This
file has already been bitten once, by #306. So the walk is now pinned
from both ends: an emitted name the table does not carry fails, AND an
enrolled name the walk stops seeing fails.

#315 emptied the grandfathered set this module shipped with. All eight
names it carried are enrolled, four of them into the ``infrastructure``
category invented to hold them, so the guard now admits no exceptions at
all.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

from kstrl.autonomy_replay import _REPLAY_ONLY_PREFIXES, INFRA_FAILURE_PREFIXES
from kstrl.evolution import (
    _CATEGORY_BY_CHECK,
    CATEGORIES,
    INFRASTRUCTURE_CHECKS,
    category_for_check,
)

KSTRL_DIR = Path(__file__).resolve().parent.parent / "kstrl"

#: Calls whose first argument (or ``name=``) is a check name.
#: ``CheckResult`` is the type itself; ``_failed_gate_result`` is the
#: shared builder the three subprocess gates package their failures
#: through.
CHECK_NAME_CALLS = frozenset({"CheckResult", "_failed_gate_result"})

#: Enrolled names the walk provably cannot see, with the reason each is
#: invisible. Anything else in the table must be reachable by the walk,
#: or the walk has gone blind and says nothing (#324).
ENROLLED_BUT_INVISIBLE = {
    # signatures_from_findings composes "<phase>:<category>" from a
    # runtime argument. "review" is NOT here: pipeline also emits
    # "review:divergence" and friends as literals, so the walk does see
    # that one, and claiming otherwise would excuse a real blind spot.
    "security": "composed from a runtime phase argument",
    "contract": "composed from a runtime phase argument",
    # _classify_check RETURNS this one for a legacy flattened error
    # string. It is never an argument, so no call site carries it.
    "verification": "returned by _classify_check, never passed to a call",
}

#: What #315 decided, pinned name by name. The table is data, so a
#: careless edit to it is a one-character behaviour change to ``ks
#: evolve``; this is the test that has to be edited alongside it.
CATEGORY_DECIDED_IN_315 = {
    "fixtures": "verification",
    "policy_envelope": "verification",
    "test_adequacy": "verification",
    "aborted": "infrastructure",
    "token_budget": "infrastructure",
    "pr": "infrastructure",
    "diff": "infrastructure",
    "engineer": "iteration",
}


def _string_bindings(node: ast.stmt) -> dict[str, str]:
    """``{name: value}`` for one statement, empty unless it binds a str.

    Handles the annotated form as well as the plain one: ``gateparse``
    declares ``GATE_TOOLS`` and friends with annotations, so an
    ``ast.Assign``-only reader would silently drop a check name and
    leave this whole module passing vacuously.
    """
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and _is_str_constant(node.value):
            return {node.target.id: node.value.value}  # type: ignore[union-attr]
        return {}
    if not isinstance(node, ast.Assign) or not _is_str_constant(node.value):
        return {}
    return {t.id: node.value.value for t in node.targets if isinstance(t, ast.Name)}  # type: ignore[attr-defined]


def _is_str_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _called_name(func: ast.expr) -> str:
    """The bare callable name, through an attribute access.

    ``verify.CheckResult(...)`` and ``CheckResult(...)`` are the same
    construction to this walk. No call site uses the qualified form
    today, which is exactly why an ``ast.Name``-only reader would not
    fail when one is added.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _name_argument(node: ast.AST) -> ast.expr | None:
    """The expression giving the check name, for a call that has one."""
    if not isinstance(node, ast.Call) or _called_name(node.func) not in CHECK_NAME_CALLS:
        return None
    for kw in node.keywords:
        if kw.arg == "name":
            return kw.value
    return node.args[0] if node.args else None


def _signature_prefixes(node: ast.AST) -> list[str]:
    """Check prefixes of any ``signatures=["check:code"]`` literal.

    These never pass through a ``CheckResult``: ``pipeline`` hands them
    straight to ``PhaseFailure``. They reach ``category_for_check`` via
    ``split_signature`` all the same.
    """
    if not isinstance(node, ast.Call):
        return []
    found: list[str] = []
    for kw in node.keywords:
        if kw.arg != "signatures" or not isinstance(kw.value, ast.List):
            continue
        for element in kw.value.elts:
            if _is_str_constant(element) and ":" in element.value:  # type: ignore[attr-defined]
                found.append(element.value.split(":", 1)[0])  # type: ignore[attr-defined]
    return found


@lru_cache(maxsize=1)
def _parsed_modules() -> tuple[tuple[str, ast.Module], ...]:
    """Every ``kstrl/`` module, parsed once for the whole session.

    Measured before caching: ``_check_names`` re-parsed all 124 files on
    each of its five calls and parsed the package a second time inside
    the constant collector, costing 1.52 s, about 70 percent of the
    scope-related test files' total runtime.
    """
    return tuple(
        (str(path.relative_to(KSTRL_DIR.parent)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(KSTRL_DIR.rglob("*.py"))
    )


@lru_cache(maxsize=1)
def _module_level_strings() -> dict[str, dict[str, str]]:
    """Module-level ``NAME = "literal"`` bindings, keyed by module."""
    return {
        rel: {k: v for node in tree.body for k, v in _string_bindings(node).items()}
        for rel, tree in _parsed_modules()
    }


def unambiguous_pool(per_module: dict[str, dict[str, str]]) -> dict[str, str]:
    """Constants that mean ONE thing across the whole package.

    A check name is usually defined in one module and used in another:
    ``verify`` builds its gates from ``gateparse.GATE_TEST``. So the
    walk cannot resolve names against the using module alone. Pooling
    every module's constants into one flat dict is the obvious fix and
    is wrong in a way nothing would report: two modules binding the same
    name to different values would resolve to whichever file sorts last,
    silently attributing one module's check name to another's.

    So a name is poolable only when every definition of it agrees. A
    genuinely ambiguous name resolves to nothing and, if it is a check
    name, surfaces as unenrolled rather than as the wrong answer. There
    are no collisions today; this is here so the walk has a reason to be
    correct rather than a coincidence.
    """
    values: dict[str, set[str]] = {}
    for constants in per_module.values():
        for name, value in constants.items():
            values.setdefault(name, set()).add(value)
    return {name: next(iter(v)) for name, v in values.items() if len(v) == 1}


def _resolve(
    value: ast.expr | None,
    own: dict[str, str],
    pool: dict[str, str],
) -> str | None:
    """A string from a literal or a module-level constant reference.

    The defining module wins over the pool, so a module that shadows an
    imported name is read as itself.
    """
    if _is_str_constant(value):
        return value.value  # type: ignore[union-attr,return-value]
    if isinstance(value, ast.Name):
        return own.get(value.id) or pool.get(value.id)
    return None


@lru_cache(maxsize=1)
def _check_names() -> dict[str, str]:
    """Every name ``kstrl/`` can send to ``category_for_check``."""
    per_module = _module_level_strings()
    pool = unambiguous_pool(per_module)
    found: dict[str, str] = {}
    for rel, tree in _parsed_modules():
        own = per_module[rel]
        for node in ast.walk(tree):
            names = [_resolve(_name_argument(node), own, pool), *_signature_prefixes(node)]
            for name in names:
                if name:
                    found.setdefault(name, rel)
    return found


class TestEveryCheckNameIsEnrolled:
    def test_the_walk_sees_the_gates_it_claims_to_guard(self) -> None:
        """A walk that matches nothing passes vacuously forever. Each
        group below is one resolution path, and each was broken at some
        point in this file's short history."""
        names = set(_check_names())
        assert {"test_suite", "typecheck", "linter"} <= names, "module-constant resolution"
        assert {"diff_scope", "scope_unreadable", "prd_stories"} <= names, "literal names"
        assert {"pr", "engineer", "token_budget"} <= names, "signature prefixes"
        # #306: this one was not pinned, and so was not protected.
        # Rewriting `CheckResult(name="mutation_testing", ...)` as
        # `name=name` off a function-local took the walk from 19 names
        # to 18 with nothing failing: the walk fails on an unenrolled
        # name and cannot fail on one it cannot see. Measured on that
        # branch before the fix.
        assert "mutation_testing" in names, "check-name constant in the defining module"

    def test_every_emitted_name_is_enrolled(self) -> None:
        """#315 emptied the grandfathered set, so this has no exceptions
        left: a name kstrl emits and the table does not carry is a
        failure filed under whichever category the fallback picks."""
        missing = {
            name: where for name, where in _check_names().items() if name not in _CATEGORY_BY_CHECK
        }
        assert not missing, (
            f"names emitted by kstrl/ but absent from "
            f"evolution._CATEGORY_BY_CHECK: {missing}. An unenrolled name "
            f"falls through to 'iteration', filing a verification gate under "
            f"the engineer loop. Add a row to that table."
        )

    def test_the_walk_covers_every_enrolled_name_but_these(self) -> None:
        """The blind-guard test (#324). Every miss logged in that issue
        was in the skip direction: the matcher stopped resolving a name,
        saw fewer sites and reported clean. Pinning the enrolled names it
        cannot see turns the next such refactor from a silent narrowing
        into a red test, for the whole table rather than the four names
        the anti-vacuity test happens to list."""
        invisible = set(_CATEGORY_BY_CHECK) - set(_check_names())
        assert invisible == set(ENROLLED_BUT_INVISIBLE), (
            f"the walk sees a different set of enrolled names than "
            f"expected. Newly invisible: {sorted(invisible - set(ENROLLED_BUT_INVISIBLE))} "
            f"(a refactor probably moved a name out of the shape the walk "
            f"matches - fix the walk, do not add the name here). Newly "
            f"visible: {sorted(set(ENROLLED_BUT_INVISIBLE) - invisible)} "
            f"(delete its row from ENROLLED_BUT_INVISIBLE)."
        )

    def test_the_runtime_composed_phases_are_enrolled(self) -> None:
        """``signatures_from_findings`` builds these from a runtime
        argument, so no literal exists for the walk to find. Asserted by
        hand because the walk provably cannot cover them: removing
        ``security`` from the table does not fail any other test here.
        ``review`` is asserted with them because it is composed the same
        way, even though a literal for it does exist elsewhere."""
        for phase in ("review", "security", "contract"):
            assert phase in _CATEGORY_BY_CHECK, (
                f"{phase!r} is composed into failure signatures by "
                f"evolution.signatures_from_findings and must stay enrolled; "
                f"the AST walk cannot see it."
            )

    def test_every_row_carries_a_category_that_exists(self) -> None:
        """A typo in a category value invents a category. Nothing else
        would notice: ``category_for_check`` returns whatever the table
        says, and the one consumer that displays it prints any string."""
        unknown = {
            name: category
            for name, category in _CATEGORY_BY_CHECK.items()
            if category not in CATEGORIES
        }
        assert not unknown, (
            f"rows in _CATEGORY_BY_CHECK naming a category that is not in "
            f"evolution.CATEGORIES: {unknown}."
        )

    @pytest.mark.parametrize(("name", "category"), sorted(CATEGORY_DECIDED_IN_315.items()))
    def test_the_names_315_enrolled_keep_the_category_it_gave_them(
        self, name: str, category: str
    ) -> None:
        """Each of these was filed under ``iteration`` by the fallback,
        so ``ks evolve`` called a merge conflict or a policy-envelope
        breach an engineer-loop problem."""
        assert name in _check_names(), f"{name!r} is no longer emitted by kstrl/"
        assert category_for_check(name) == category

    def test_a_colliding_constant_resolves_to_nothing(self) -> None:
        """The wrong answer is worse than no answer: a name two modules
        bind differently must not be attributed to either."""
        agreed = {"a.py": {"SHARED": "x"}, "b.py": {"SHARED": "x", "OWN": "y"}}
        assert unambiguous_pool(agreed) == {"SHARED": "x", "OWN": "y"}
        conflicting = {"a.py": {"SHARED": "x"}, "b.py": {"SHARED": "z"}}
        assert "SHARED" not in unambiguous_pool(conflicting)


class TestTheTwoInfrastructureConsumersAgree:
    """#315: the journal and the autonomy replay both decide what counts
    as infrastructure, and before this they disagreed about
    ``pr:merge-conflict`` - plumbing to the replay, an engineer-loop
    failure to the journal. The replay now derives its prefixes from the
    journal's table, so the shared part cannot drift. What is left is one
    deliberate difference, pinned here so that changing either side
    without the other is a red test rather than a quiet divergence."""

    def test_every_infrastructure_check_is_an_infra_abort_for_the_replay(self) -> None:
        for check in INFRASTRUCTURE_CHECKS:
            assert f"{check}:any-code".startswith(INFRA_FAILURE_PREFIXES), (
                f"{check!r} is 'infrastructure' in the journal but a decisive "
                f"judgement failure to autonomy_replay."
            )

    def test_the_replays_extra_prefixes_are_exactly_the_documented_ones(self) -> None:
        """The replay asks a wider question than the journal's category:
        not 'which part of the factory failed' but 'did this run yield a
        verdict about the factory's judgement at all'. A gate's honest
        verdict can still answer no. Anything beyond this list is an
        undocumented divergence."""
        derived = {f"{check}:" for check in INFRASTRUCTURE_CHECKS}
        assert set(INFRA_FAILURE_PREFIXES) - derived == set(_REPLAY_ONLY_PREFIXES)

    def test_the_scope_refusal_is_the_deliberate_divergence(self) -> None:
        """``scope_unreadable`` is a Phase 1 gate result, so the journal
        files it under verification (#294 gave it its own gate, table row
        and proposal arm on that basis). It is still an infrastructure
        casualty for the replay: nothing was measured about the change,
        so the run says nothing about judgement. Both halves asserted,
        because the divergence is only defensible while it is on
        purpose."""
        assert category_for_check("scope_unreadable") == "verification"
        assert "scope_unreadable" not in INFRASTRUCTURE_CHECKS
        assert "scope_unreadable:no-trustworthy-scope".startswith(INFRA_FAILURE_PREFIXES)

    def test_the_prefixes_cannot_swallow_a_neighbouring_check(self) -> None:
        """``diff:`` must not match ``diff_scope:``: a scope violation is
        the reviewer's verdict on the change and decisive evidence about
        it. The colon is what separates them, so it is load-bearing and
        tested rather than assumed."""
        assert not "diff_scope:files-outside-allowed-scope".startswith(INFRA_FAILURE_PREFIXES)
        assert not "review:scope_creep".startswith(INFRA_FAILURE_PREFIXES)
