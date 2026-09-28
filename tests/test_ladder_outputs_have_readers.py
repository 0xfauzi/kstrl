"""Every permission the autonomy ladder varies by level has a reader (#602).

The defect: ``FlagBundle.auto_accept_plan`` was the one field on which L1
and L2 differ, and no code outside ``kstrl/autonomy.py`` read it. The
ladder printed "plans: human-approved" at L1 and nothing asked anyone, so
L1 and L2 ran identically. A bundle field nobody reads is a gate that
exists only in the audit record.

The census is closed by construction: the fields come from
``dataclasses.fields(FlagBundle)`` and the values from ``flag_bundle_for``
at every ``AutonomyLevel``, so a new field is checked the day it is added,
with no pin to update. A field whose value is the same at every level is
not a permission the ladder grants, and is skipped.

This guard CLEARS a field, so it is narrow on purpose: a read counts only
when its base expression is a name ending in ``bundle`` (``bundle.x``,
``ladder.bundle.x``, ``self.bundle.x``) or a call of ``flag_bundle_for``.
A read through an alias it cannot name does not clear, and the positive
controls below show both halves on source it is given.
"""

from __future__ import annotations

import ast
import dataclasses

from kstrl.autonomy import AutonomyLevel, FlagBundle, flag_bundle_for
from tests.helpers.astwalk import label, package_sources, parsed

#: The module that declares, prints and assigns the bundle. Its own
#: references are the declaration, not a reader.
LADDER_MODULE = "autonomy.py"


def _level_varying_fields() -> set[str]:
    fields = {f.name for f in dataclasses.fields(FlagBundle)}
    assert fields, "FlagBundle has no fields: the census would check nothing"
    bundles = [flag_bundle_for(level) for level in AutonomyLevel]
    return {name for name in fields if len({repr(getattr(b, name)) for b in bundles}) > 1}


def _is_bundle(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        return ast.unparse(node.func).split(".")[-1] == "flag_bundle_for"
    return ast.unparse(node).split(".")[-1].endswith("bundle")


def _read_fields(tree: ast.AST, names: set[str]) -> set[str]:
    """The names in ``names`` read as an attribute of a bundle in ``tree``."""
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and node.attr in names
        and _is_bundle(node.value)
    }


def _unread(sources: dict[str, ast.AST], names: set[str]) -> set[str]:
    read: set[str] = set()
    for where, tree in sources.items():
        if where != LADDER_MODULE:
            read |= _read_fields(tree, names)
    return names - read


def test_every_level_varying_bundle_field_has_a_reader() -> None:
    """RED before #602: ``auto_accept_plan`` had no reader."""
    varying = _level_varying_fields()
    # Not vacuous: L1 and L2 differ in this field and nothing else.
    assert "auto_accept_plan" in varying, varying
    sources: dict[str, ast.AST] = {label(p): parsed(p) for p in package_sources()}
    assert LADDER_MODULE in sources, sorted(sources)
    assert _unread(sources, varying) == set(), (
        "a bundle field that differs between levels is read nowhere outside "
        "kstrl/autonomy.py: the ladder grants or withholds it, and nothing enforces it"
    )


def test_the_census_sees_a_field_nobody_reads() -> None:
    """Positive control: the walk reports a field with no reader."""
    source = ast.parse("def f(bundle):\n    return bundle.review_mode\n")
    assert _unread({"other.py": source}, {"review_mode", "auto_accept_plan"}) == {
        "auto_accept_plan"
    }


def test_a_read_off_something_that_is_not_a_bundle_does_not_clear() -> None:
    """Positive control for the narrow half: ``config.auto_accept_plan``
    and a read inside kstrl/autonomy.py clear nothing."""
    other = ast.parse("def f(config):\n    return config.auto_accept_plan\n")
    ladder = ast.parse("def g(bundle):\n    return bundle.auto_accept_plan\n")
    assert _unread({"other.py": other, LADDER_MODULE: ladder}, {"auto_accept_plan"}) == {
        "auto_accept_plan"
    }


def test_the_census_reads_every_kind_of_bundle_base() -> None:
    source = ast.parse(
        "def f(ladder, bundle, level):\n"
        "    return (ladder.bundle.a, bundle.b, flag_bundle_for(level).c)\n"
    )
    assert _unread({"other.py": source}, {"a", "b", "c"}) == set()
