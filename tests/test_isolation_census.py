"""#700 slice 1: one constructor of a rung, one builder of a nono command line.

``ProvenRung`` is the record that says a zone was proven. If any code
other than ``isolation.prove_rung`` could build one, a rung could exist
that no canary ever ran for, and the label it carries would be a claim
rather than a measurement. So the census pins every ``ProvenRung(...)``
call in ``kstrl/`` by module and scope, and the only row is
``prove_rung``.

The nono command line is the other half. ``rung._nono_argv`` (moved out
of ``isolation`` in slice 2, so ``verify`` can hold a rung without an
import cycle) is where the
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
    spells,
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
        expected={"rung.py:_nono_argv": 1},
        control="argv = [nono, 'wrap', '-s', '-p', str(policy), '--', *argv]\n",
        message=(
            "A second nono command line is built outside rung._nono_argv. The "
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


# --- #700 slice 3: `up` starts only through start_scrubbed, inside the rung ---


def _starts_a_lasting_command(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and leaf_name(node.func) == "start_scrubbed"


def test_only_the_replay_starts_a_command_that_keeps_running() -> None:
    """``verify.start_scrubbed`` is the one spawn that returns while its
    command runs on, which is what ``[stack] up`` needs. Every call is
    pinned by module and scope, so a second place that starts ``up`` (or
    anything else that outlives the call) is a census delta."""
    assert_census(
        sources=package_sources(),
        sees=_starts_a_lasting_command,
        expected={"replay.py:_run_stages": 1},
        control="proc = verify.start_scrubbed(up, cwd=w, rung=r, log=f)\n",
        message=(
            "start_scrubbed is called somewhere new. It starts a command that "
            "outlives the call; the replay is the one caller, and stops its group."
        ),
        key=_where,
    )


def test_up_is_named_only_where_it_is_read_validated_or_started() -> None:
    """Every spelling of ``up`` in ``kstrl/``: the key, its validation, the
    digest, the evidence and the one read that starts it. A new read of
    ``stack.up`` is a new place that could run it, so it is a delta here."""
    assert_census(
        sources=package_sources(),
        sees=spells("up"),
        expected={
            # The key, the field, its validation, load, digest and evidence.
            "stack.py:<module>": 2,
            "stack.py:stack_errors": 2,
            "stack.py:load_stack": 2,
            "stack.py:Stack.digest": 2,
            "stack.py:stack_evidence": 2,
            "stack.py:file_stack_item": 2,
            # The one place it runs: started inside the test zone, then waited on.
            "replay.py:_run_stages": 3,
            "replay.py:_ready": 1,
        },
        control=["start_scrubbed(stack.up, cwd=w, rung=r, log=f)\n", 'raw.get("up", "")\n'],
        message="`up` is spelled somewhere new, or a count moved: read the new site.",
        key=_where,
    )


def test_start_scrubbed_runs_its_command_only_inside_a_proven_rung() -> None:
    """``start_scrubbed`` takes a rung with no default and no None, and its
    one ``Popen`` spawns what ``_in_rung`` built from that rung: a command
    that outlives the call never runs on the host."""
    (verify_source,) = [path for path in package_sources() if label(path) == "verify.py"]
    (function,) = [
        node
        for node in all_nodes(parsed(verify_source))
        if isinstance(node, ast.FunctionDef) and node.name == "start_scrubbed"
    ]
    names = [arg.arg for arg in function.args.kwonlyargs]
    rung = function.args.kwonlyargs[names.index("rung")]
    assert rung.annotation is not None and ast.unparse(rung.annotation) == "ProvenRung"
    assert function.args.kw_defaults[names.index("rung")] is None, "rung has a default"
    (spawn,) = [
        node
        for node in all_nodes(function)
        if isinstance(node, ast.Call) and leaf_name(node.func) == "Popen"
    ]
    built = spawn.args[0]
    assert isinstance(built, ast.Call) and leaf_name(built.func) == "_in_rung", ast.unparse(spawn)
    assert ast.unparse(built.args[1]) == "rung", ast.unparse(built)
