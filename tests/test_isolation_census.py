"""#700 slice 1: one constructor of a rung, one builder of a nono command line.

``ProvenRung`` is the record that says a zone was proven. If any code
other than ``isolation.prove_rung`` could build one, a rung could exist
that no canary ever ran for, and the label it carries would be a claim
rather than a measurement. So the census pins every ``ProvenRung(...)``
call in ``kstrl/`` by module and scope, and the only row is
``prove_rung``.

The nono command line is the other half. ``_nono_argv`` is where the
policy, the ``env`` in front of the command and nono's own state
directories are put together, and the canaries prove that command line
and no other. A second builder would run commands in a rung the
canaries never saw, so the census pins every list or tuple display in
``kstrl/`` holding an element that folds to ``"wrap"``.

Both guards FLAG: an over-match costs a false positive somebody reads,
never a site cleared. Each census proves its net fires on a control
before it reads the package. Two shapes are disclosed and pinned as
strict xfails, so a walk that learns to see them XPASSes and fails:
a rung built by ``dataclasses.replace`` (a call whose name is not the
class), and a command line grown by ``append`` rather than written as
one display.

When red, re-derive the census by running this file and read the
message; never type a count in.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    folded_str,
    label,
    leaf_name,
    package_sources,
    parse,
    parsed,
    scope_of,
)


def _constructs_a_rung(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and leaf_name(node.func) == "ProvenRung"


def _builds_a_nono_argv(node: ast.AST) -> bool:
    return isinstance(node, ast.List | ast.Tuple) and any(
        folded_str(element) == "wrap" for element in node.elts
    )


def _where(source_file: Path, node: ast.AST) -> str:
    return f"{label(source_file)}:{scope_of(parsed(source_file))[id(node)]}"


def _sees(predicate: object, source: str) -> bool:
    assert callable(predicate)
    return any(predicate(node) for node in all_nodes(parse(source)))


def test_only_prove_rung_constructs_a_rung() -> None:
    assert_census(
        sources=package_sources(),
        sees=_constructs_a_rung,
        expected={"isolation.py:prove_rung": 1},
        control="rung = isolation.ProvenRung(zone='test')\n",
        message=(
            "A ProvenRung is built outside isolation.prove_rung. A rung no canary "
            "ran for would label results with isolation nobody measured: build it "
            "through prove_rung."
        ),
        key=_where,
    )


def test_only_one_place_builds_a_nono_command_line() -> None:
    assert_census(
        sources=package_sources(),
        sees=_builds_a_nono_argv,
        expected={"isolation.py:_nono_argv": 1},
        control="argv = [nono, 'wrap', '-s', '-p', str(policy), '--', *argv]\n",
        message=(
            "A second nono command line is built outside isolation._nono_argv. The "
            "canaries prove that one; a second would run commands in a rung they "
            "never measured. Build it through _nono_argv."
        ),
        key=_where,
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_disclosed_a_rung_built_by_replace_is_not_seen() -> None:
    blind_spot(
        lambda source: _sees(_constructs_a_rung, source),
        "proven = dataclasses.replace(rung, refusal='')\n",
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_disclosed_a_command_line_grown_by_append_is_not_seen() -> None:
    blind_spot(
        lambda source: _sees(_builds_a_nono_argv, source),
        "argv = [nono]\nargv.append('wrap')\n",
    )
