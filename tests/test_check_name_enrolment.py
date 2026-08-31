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
from a runtime ``phase`` argument, so the ``review``, ``security`` and
``contract`` prefixes appear in no literal anywhere in ``kstrl/``. They
are enrolled today, and
:meth:`TestEveryCheckNameIsEnrolled.test_the_runtime_composed_phases_are_enrolled`
asserts it directly, because the walk will not notice if that stops
being true.

Names already unenrolled when this walk was written are listed in
:data:`UNENROLLED_AT_INTRODUCTION` rather than fixed here, and tracked
in #315. Correcting them changes what ``ks evolve`` reports for journal
history already written, which is a behaviour change with nothing to do
with #294 and wants its own justification. Grandfathering by name is
what makes the gate landable today: the debt is enumerated, and nothing
new can join it without a visible edit. The set is guarded in both
directions, so it cannot rot into a lie.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

from kstrl.evolution import _CATEGORY_BY_CHECK

KSTRL_DIR = Path(__file__).resolve().parent.parent / "kstrl"

#: Calls whose first argument (or ``name=``) is a check name.
#: ``CheckResult`` is the type itself; ``_failed_gate_result`` is the
#: shared builder the three subprocess gates package their failures
#: through.
CHECK_NAME_CALLS = frozenset({"CheckResult", "_failed_gate_result"})

#: Names that ``kstrl/`` emits and ``_CATEGORY_BY_CHECK`` did not carry
#: on the day this walk was added, so ``category_for_check`` files them
#: under ``"iteration"``. The first three are Phase 1 gates; the rest are
#: signature prefixes for failures recorded outside a ``CheckResult``.
#: Tracked in #315. Do not add to this set to make a new name pass;
#: enrol the name instead.
UNENROLLED_AT_INTRODUCTION = frozenset(
    {
        "fixtures",
        "policy_envelope",
        "test_adequacy",
        "aborted",
        "token_budget",
        "pr",
        "engineer",
        "diff",
    }
)


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

    def test_no_unenrolled_name_beyond_the_grandfathered_set(self) -> None:
        missing = {
            name: where
            for name, where in _check_names().items()
            if name not in _CATEGORY_BY_CHECK and name not in UNENROLLED_AT_INTRODUCTION
        }
        assert not missing, (
            f"names emitted by kstrl/ but absent from "
            f"evolution._CATEGORY_BY_CHECK: {missing}. An unenrolled name "
            f"falls through to 'iteration', filing a verification gate under "
            f"the engineer loop. Add a row to that table."
        )

    def test_the_runtime_composed_phases_are_enrolled(self) -> None:
        """``signatures_from_findings`` builds these from a runtime
        argument, so no literal exists for the walk to find. Asserted by
        hand because the walk provably cannot cover them: removing
        ``security`` from the table does not fail any other test here."""
        for phase in ("review", "security", "contract"):
            assert phase in _CATEGORY_BY_CHECK, (
                f"{phase!r} is composed into failure signatures by "
                f"evolution.signatures_from_findings and must stay enrolled; "
                f"the AST walk cannot see it."
            )

    def test_a_colliding_constant_resolves_to_nothing(self) -> None:
        """The wrong answer is worse than no answer: a name two modules
        bind differently must not be attributed to either."""
        agreed = {"a.py": {"SHARED": "x"}, "b.py": {"SHARED": "x", "OWN": "y"}}
        assert unambiguous_pool(agreed) == {"SHARED": "x", "OWN": "y"}
        conflicting = {"a.py": {"SHARED": "x"}, "b.py": {"SHARED": "z"}}
        assert "SHARED" not in unambiguous_pool(conflicting)

    @pytest.mark.parametrize("name", sorted(UNENROLLED_AT_INTRODUCTION))
    def test_the_grandfathered_set_still_describes_real_debt(self, name: str) -> None:
        """Two ways the list could lie: a name no longer emitted, and a
        name since enrolled. Both make it stale."""
        assert name in _check_names(), (
            f"{name!r} is no longer emitted by kstrl/; remove it from UNENROLLED_AT_INTRODUCTION."
        )
        assert name not in _CATEGORY_BY_CHECK, (
            f"{name!r} is now enrolled in _CATEGORY_BY_CHECK; remove it from "
            f"UNENROLLED_AT_INTRODUCTION."
        )
