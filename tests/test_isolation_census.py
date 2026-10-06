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
from collections.abc import Callable
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
            # The key, the field, its validation, load, digest and evidence,
            # and the table an operator pastes to adopt a proposal (#696 s7).
            "stack.py:<module>": 2,
            "stack.py:stack_errors": 2,
            "stack.py:stack_from_table": 2,
            "stack.py:Stack.digest": 2,
            "stack.py:stack_evidence": 2,
            "stack.py:file_stack_item": 2,
            "stack.py:stack_toml": 1,
            # The one place it runs: started inside the test zone, then waited on.
            "replay.py:_run_stages": 3,
            "replay.py:_ready": 1,
            # #700 slice 7: the designer's prompt names the command; it never runs it.
            "acceptance_design.py:build_design_prompt": 2,
        },
        control=["start_scrubbed(stack.up, cwd=w, rung=r, log=f)\n", 'raw.get("up", "")\n'],
        message="`up` is spelled somewhere new, or a count moved: read the new site.",
        key=_where,
    )


def test_start_scrubbed_runs_its_command_only_inside_a_proven_rung() -> None:
    """``start_scrubbed`` takes a ``Rung`` (a proven rung or the host
    fallback) with no default and no None, and its one ``Popen`` spawns what
    ``_in_rung`` built from it: a command that outlives the call runs on the
    host only under the explicit fallback of a platform with no prover."""
    (verify_source,) = [path for path in package_sources() if label(path) == "verify.py"]
    (function,) = [
        node
        for node in all_nodes(parsed(verify_source))
        if isinstance(node, ast.FunctionDef) and node.name == "start_scrubbed"
    ]
    names = [arg.arg for arg in function.args.kwonlyargs]
    rung = function.args.kwonlyargs[names.index("rung")]
    assert rung.annotation is not None and ast.unparse(rung.annotation) == "Rung"
    assert function.args.kw_defaults[names.index("rung")] is None, "rung has a default"
    (spawn,) = [
        node
        for node in all_nodes(function)
        if isinstance(node, ast.Call) and leaf_name(node.func) == "Popen"
    ]
    # #642 slice 6: the Popen starts the leash, the leash starts `argv`, and
    # `argv` is `spawned` as an argv, which is what `_in_rung` built from `rung`.
    built = spawn.args[0]
    assert isinstance(built, ast.Call), ast.unparse(spawn)
    assert leaf_name(built.func) == "leash_command", ast.unparse(spawn)
    assert ast.unparse(built.args[0]) == "argv", ast.unparse(built)
    assigned = _values_assigned_in(function)
    argv = assigned["argv"]
    assert _names_in(argv) == {"spawned", "isinstance", "str", "list"}, ast.unparse(argv)
    in_rung = assigned["spawned"]
    assert isinstance(in_rung, ast.Call), ast.unparse(in_rung)
    assert leaf_name(in_rung.func) == "_in_rung", ast.unparse(in_rung)
    assert ast.unparse(in_rung.args[1]) == "rung", ast.unparse(in_rung)


def _values_assigned_in(function: ast.FunctionDef) -> dict[str, ast.expr]:
    """Each assignment target in ``function``, as source, mapped to the last
    value assigned to it."""
    return {
        ast.unparse(target): node.value
        for node in all_nodes(function)
        if isinstance(node, ast.Assign)
        for target in node.targets
    }


def _names_in(node: ast.AST) -> set[str]:
    """Every name ``node`` reads."""
    return {name.id for name in ast.walk(node) if isinstance(name, ast.Name)}


# --- #700 slice 4: an acceptance check runs only through the replay, in its rung ---

#: The names a call starts a process by, and the replay's stage runner.
_RUNS_A_COMMAND = frozenset(
    {
        "_ran",
        "run_scrubbed",
        "start_scrubbed",
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "system",
        "execv",
        "execvp",
    }
)


def _runs_a_command(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and leaf_name(node.func) in _RUNS_A_COMMAND


def test_an_acceptance_check_runs_only_through_the_replay_inside_its_rung() -> None:
    """``kstrl/acceptance.py`` starts no process of its own. Its one call
    that runs a command is the replay's ``_ran``, made inside the probe the
    replay hands its test-zone rung, and it passes that rung on, so a check
    runs on the host only under the host fallback of a platform with no
    prover (#700, owner decision 2026-10-05)."""
    (source,) = [path for path in package_sources() if label(path) == "acceptance.py"]
    assert_census(
        sources=[source],
        sees=_runs_a_command,
        expected={"acceptance.py:_run_check": 1},
        control="done = subprocess.run(argv, cwd=here)\n",
        message=(
            "kstrl/acceptance.py runs a command somewhere new. A check runs only "
            "through replay._ran inside the rung the replay proved."
        ),
        key=_where,
    )
    (call,) = [node for node in all_nodes(parsed(source)) if _runs_a_command(node)]
    assert leaf_name(call.func) == "_ran", ast.unparse(call)
    assert ast.unparse(call.args[3]) == "rung", ast.unparse(call)


# --- #700, owner decision 2026-10-05: the host fallback, decided in one place ---


def _calls(name: str) -> Callable[[ast.AST], bool]:
    def sees(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and leaf_name(node.func) == name

    return sees


def test_only_host_fallback_constructs_a_fallback() -> None:
    """A ``HostFallback`` built anywhere else could carry a label other than
    the one ``HOST_FALLBACK_LABEL`` renders, or exist on a platform with a
    prover, so its one constructor is pinned."""
    assert_census(
        sources=package_sources(),
        sees=_calls("HostFallback"),
        expected={"rung.py:host_fallback": 1},
        control="fallback = rung.HostFallback('linux', 'label')\n",
        message=(
            "A HostFallback is built outside rung.host_fallback. Build it there: "
            "it is where the platform decides and the one label is rendered."
        ),
        key=_where,
    )


def test_the_fallback_label_and_the_platform_seam_have_one_reader() -> None:
    """The label template and the seam's variable name are each defined once
    and read once, by ``host_fallback``: a second reader is a second place a
    label or a platform could be made. The seam's value is pinned too, so a
    reader that spells the variable as a literal (``os.environ.get(
    "KSTRL_ISOLATION_PLATFORM")``) is seen as well as one that names
    ``PLATFORM_ENV``."""
    pins = {
        "HOST_FALLBACK_LABEL": {"rung.py:<module>": 1, "rung.py:host_fallback": 1},
        "PLATFORM_ENV": {"rung.py:<module>": 1, "rung.py:host_fallback": 1},
        "KSTRL_ISOLATION_PLATFORM": {"rung.py:<module>": 1},
    }
    for name, expected in pins.items():
        assert_census(
            sources=package_sources(),
            sees=spells(name),
            expected=expected,
            control=f"text = rung.{name}\n",
            message=f"{name} is spelled somewhere new: read it only in rung.host_fallback.",
            key=_where,
        )


def test_the_fallback_and_the_prover_are_reached_only_through_prove_zones() -> None:
    """``prove_zones`` decides between the fallback and a proof for the
    factory, the replay and ``ks doctor --measure`` alike. A caller of
    ``host_fallback`` or ``prove_rung`` elsewhere could skip the decision:
    prove a zone on a platform with no prover, or fall back where one exists."""
    for name in ("host_fallback", "prove_rung"):
        assert_census(
            sources=package_sources(),
            sees=_calls(name),
            expected={"isolation.py:prove_zones": 1},
            control=f"rung = isolation.{name}()\n",
            message=f"{name} is called outside isolation.prove_zones: go through prove_zones.",
            key=_where,
        )
