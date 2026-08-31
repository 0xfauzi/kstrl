"""Every Phase 1 check name is enrolled in the evolution category map.

``evolution.category_for_check`` maps a check name onto a
``FailurePattern`` category and falls through to ``"iteration"`` for
anything it does not recognise, which files a mechanical verification
gate in the journal under the engineer loop. Enrolment in
``_CATEGORY_BY_CHECK`` was a convention with no mechanism, and measured
during #294 the convention did not hold: three names emitted by
``kstrl/`` were absent from the table.

This is the mechanism, in the shape the repo already uses three times
(``tests/test_prompt_versions.py``, ``tests/test_atomicio.py``,
``tests/test_process_scoping.py``): AST-walk ``kstrl/`` for every check
name it constructs and fail on one the table does not carry.

The walk resolves module-level string constants, not just literals, and
that is load-bearing rather than thoroughness for its own sake. The
first version of this file matched literals only, and its own
anti-vacuity test caught that it saw NONE of ``test_suite``,
``typecheck`` or ``linter``: those three name themselves through
``gateparse.GATE_TEST`` and friends, and reach ``CheckResult`` via
``verify._failed_gate_result``. A guard that misses the three most
important gates is worse than no guard, because it reads as covered.

The three names already unenrolled when this walk was written are listed
in :data:`UNENROLLED_AT_INTRODUCTION` rather than fixed here, and are
tracked in #315. Correcting them changes what ``ks evolve`` reports for
existing journal history, which is a behaviour change with nothing to do
with #294 and wants its own justification. Grandfathering them by name is what makes the gate
landable today: the debt is enumerated, and no NEW check can join it,
because adding a name to that set is a deliberate edit a reviewer sees.
Deleting an entry as it gets fixed is the intended direction, and the
test fails if an entry is enrolled and left in the set, so the list
cannot rot into a lie.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kstrl.evolution import _CATEGORY_BY_CHECK

KSTRL_DIR = Path(__file__).resolve().parent.parent / "kstrl"

#: Calls whose first argument is a check name. ``CheckResult`` is the
#: type itself; ``_failed_gate_result`` is the shared builder the three
#: subprocess gates package their failures through.
CHECK_NAME_CALLS = frozenset({"CheckResult", "_failed_gate_result"})

#: Check names that ``kstrl/`` emitted and ``_CATEGORY_BY_CHECK`` did not
#: carry on the day this walk was added. Each is a Phase 1 gate whose
#: failures are categorised as ``"iteration"`` in the evolution journal
#: instead of ``"verification"``. Tracked in #315. Do not add to this set
#: to make a new check pass; enrol the check instead.
UNENROLLED_AT_INTRODUCTION = frozenset(
    {
        "fixtures",
        "policy_envelope",
        "test_adequacy",
    }
)


def _module_level_strings() -> dict[str, str]:
    """Every ``NAME = "literal"`` bound at module level in ``kstrl/``.

    Enough to resolve the constants the gates name themselves with
    (``GATE_TEST``, ``SCOPE_UNREADABLE_CHECK``). Deliberately not a full
    constant folder: a check name assembled at run time is not something
    this walk can see, and pretending otherwise would be the vacuity the
    anti-vacuity test exists to catch.
    """
    constants: dict[str, str] = {}
    for path in sorted(KSTRL_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            constants.update(_string_bindings(node))
    return constants


def _string_bindings(node: ast.stmt) -> dict[str, str]:
    """``{name: value}`` for one statement, empty unless it binds a str."""
    if not isinstance(node, ast.Assign):
        return {}
    if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
        return {}
    return {t.id: node.value.value for t in node.targets if isinstance(t, ast.Name)}


def _name_argument(node: ast.AST) -> ast.expr | None:
    """The expression giving the check name, for a call that has one."""
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name) or node.func.id not in CHECK_NAME_CALLS:
        return None
    for kw in node.keywords:
        if kw.arg == "name":
            return kw.value
    return node.args[0] if node.args else None


def _resolve(value: ast.expr | None, constants: dict[str, str]) -> str | None:
    """A string from a literal or a module-level constant reference."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        return constants.get(value.id)
    return None


def _check_names() -> dict[str, str]:
    """Every check name ``kstrl/`` constructs, mapped to its file."""
    constants = _module_level_strings()
    found: dict[str, str] = {}
    for path in sorted(KSTRL_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = _resolve(_name_argument(node), constants)
            if name is not None:
                found.setdefault(name, str(path.relative_to(KSTRL_DIR.parent)))
    return found


class TestEveryCheckNameIsEnrolled:
    def test_the_walk_sees_the_gates_it_claims_to_guard(self) -> None:
        """A walk that matches nothing passes vacuously forever. The
        three subprocess gates are the ones a literal-only walk missed,
        so they are named here explicitly rather than by count."""
        names = set(_check_names())
        assert {"test_suite", "typecheck", "linter"} <= names, (
            "the three gates that name themselves through gateparse "
            "constants are invisible to the walk"
        )
        assert {"diff_scope", "scope_unreadable", "prd_stories"} <= names

    def test_no_unenrolled_check_name_beyond_the_grandfathered_set(self) -> None:
        missing = {
            name: where
            for name, where in _check_names().items()
            if name not in _CATEGORY_BY_CHECK and name not in UNENROLLED_AT_INTRODUCTION
        }
        assert not missing, (
            f"check names emitted by kstrl/ but absent from "
            f"evolution._CATEGORY_BY_CHECK: {missing}. An unenrolled name "
            f"falls through to 'iteration', filing a Phase 1 gate under the "
            f"engineer loop. Add a row to that table."
        )

    @pytest.mark.parametrize("name", sorted(UNENROLLED_AT_INTRODUCTION))
    def test_the_grandfathered_set_still_describes_real_debt(self, name: str) -> None:
        """Two ways the list could lie: an entry no longer emitted, and
        an entry since enrolled. Both make it stale."""
        assert name in _check_names(), (
            f"{name!r} is no longer emitted by kstrl/; remove it from UNENROLLED_AT_INTRODUCTION."
        )
        assert name not in _CATEGORY_BY_CHECK, (
            f"{name!r} is now enrolled in _CATEGORY_BY_CHECK; remove it from "
            f"UNENROLLED_AT_INTRODUCTION."
        )
