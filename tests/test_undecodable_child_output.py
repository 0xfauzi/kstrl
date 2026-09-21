"""#416: a verification child's bytes are decoded as utf-8 by
``kstrl.verify.run_scrubbed``, and a child whose output it cannot decode is
now a named error every call site answers for, rather than a bare
``UnicodeDecodeError`` (a ``ValueError``) escaping as a traceback.

On main this file fails at import with ``ImportError: cannot import name
'ChildOutputDecodeError' from 'kstrl.verify'``. That is this file's RED
state.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable
from pathlib import Path

import pytest

from kstrl import breaker, contract, fixtures
from kstrl.fixtures import Fixture
from kstrl.verify import (
    CheckResult,
    ChildOutputDecodeError,
    check_linter,
    check_test_suite,
    check_typecheck,
)
from tests.helpers.astwalk import (
    Bindings,
    all_nodes,
    bindings,
    calls_to,
    handler_clauses,
    label,
    module_name,
    package_sources,
    parse,
    resolved_calls,
)
from tests.helpers.astwalk.scope import try_body_nodes

#: A child that writes one latin-1 byte and exits 0. `python3 -c` rather than
#: `printf`, so the byte is written as BYTES on any shell. The word is
#: encoded rather than escaped, for the codespell reason file 1 states.
UNDECODABLE_STDOUT = (
    "python3 -c \"import sys; sys.stdout.buffer.write('café'.encode('latin-1') + b'\\n')\""
)
UNDECODABLE_STDERR = (
    "python3 -c \"import sys; sys.stderr.buffer.write('café'.encode('latin-1') + b'\\n')\""
)


def test_run_scrubbed_refuses_output_it_cannot_decode(tmp_path: Path) -> None:
    from kstrl.verify import run_scrubbed

    with pytest.raises(ChildOutputDecodeError) as excinfo:
        run_scrubbed(UNDECODABLE_STDOUT, cwd=tmp_path, timeout=60.0)

    assert "not valid utf-8" in str(excinfo.value)
    assert "0xe9" in str(excinfo.value)
    assert "position" in str(excinfo.value)


def test_stderr_is_the_same_failure(tmp_path: Path) -> None:
    from kstrl.verify import run_scrubbed

    with pytest.raises(ChildOutputDecodeError) as excinfo:
        run_scrubbed(UNDECODABLE_STDERR, cwd=tmp_path, timeout=60.0)

    assert "not valid utf-8" in str(excinfo.value)
    assert "0xe9" in str(excinfo.value)
    assert "position" in str(excinfo.value)


_Gate = Callable[[Path, str, float], CheckResult]


@pytest.mark.parametrize(
    "fn",
    [check_test_suite, check_typecheck, check_linter],
    ids=["test_suite", "typecheck", "linter"],
)
def test_the_three_gates_fail_closed_on_undecodable_output(fn: _Gate, tmp_path: Path) -> None:
    result = fn(tmp_path, UNDECODABLE_STDOUT, 60.0)

    assert result.passed is False
    assert result.measured is False
    assert "could not be decoded" in result.message


def test_a_cli_fixture_reports_undecodable_output(tmp_path: Path) -> None:
    """D4 widens the existing ``except OSError`` clause rather than adding
    a new one, so the row keeps its "Failed to run command: {exc}" body and
    the exception's own text is what names the fault - asserted on the
    exception's half of the string, not on "Failed to run command", so this
    would still fail if the clause caught the error and said nothing about
    it."""
    fixture = Fixture(
        description="a fixture whose command prints an undecodable byte",
        fixture_type="cli",
        input_data={"command": UNDECODABLE_STDOUT},
        expected={"exit_code": 0},
    )

    result = fixtures.run_cli_fixture(fixture, tmp_path, 60.0)

    assert result.passed is False
    assert result.measured is False
    assert "not valid utf-8" in result.message


def test_contract_run_tests_reports_undecodable_output(tmp_path: Path) -> None:
    passed, message = contract._run_tests(tmp_path, UNDECODABLE_STDOUT, 60.0)

    assert passed is False
    assert "could not be decoded" in message


def test_the_breaker_signs_an_undecodable_probe_as_a_probe_error(tmp_path: Path) -> None:
    config = breaker.BreakerConfig(test_command=UNDECODABLE_STDOUT, test_timeout=60.0)

    signature = breaker.compute_test_signature(tmp_path, config)

    assert signature == "probe-error:ChildOutputDecodeError"


# --- Guard 2 -----------------------------------------------------------


def _catching_names(target: type[BaseException]) -> frozenset[str]:
    """Every BUILTIN name whose class catches ``target``. See file 1's copy
    for the full rationale; kept independent per file, per the plan."""
    found = set()
    for name in dir(builtins):
        obj = getattr(builtins, name)
        if isinstance(obj, type) and issubclass(obj, BaseException) and issubclass(target, obj):
            found.add(name)
    return frozenset(found)


def _guarded_by(tree: ast.Module, node: ast.AST, names: frozenset[str], table: Bindings) -> bool:
    """Is ``node`` in the BODY of a try whose clauses catch one of ``names``?"""
    for candidate in all_nodes(tree):
        if not isinstance(candidate, ast.Try | ast.TryStar):
            continue
        if not any(body is node for body in try_body_nodes(candidate)):
            continue
        for clause in handler_clauses(candidate, table):
            if clause.decided and clause.names & names:
                return True
    return False


#: Derived from the real class rather than listed, so ``except Exception``,
#: ``except RuntimeError`` and the name itself all clear. ``_catching_names``
#: reads ``builtins`` only, so the name of the class itself is unioned in by
#: hand; that is why the ``| {"ChildOutputDecodeError"}`` half is here and it
#: is not redundant.
_ACCEPTED = _catching_names(ChildOutputDecodeError) | {"ChildOutputDecodeError"}

#: The target: every call to ``run_scrubbed`` in ``kstrl/``. ``resolved_calls``
#: with this target finds only the sites outside ``verify.py``; the sites
#: inside call the bare local name, which the resolver does not place. The
#: union with the bare-name walk below is built in ``_run_scrubbed_calls``.
_RESOLVED_TARGET = frozenset({"kstrl.verify.run_scrubbed"})


def _is_bare_run_scrubbed_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_scrubbed"
    )


def _run_scrubbed_calls(tree: ast.Module, *, module: str) -> list[ast.Call]:
    """The UNION of the resolved calls and every bare-name ``run_scrubbed``
    call, deduplicated by ``id(node)``.

    The bare-name half deliberately over-matches - another module defining
    its own ``run_scrubbed`` would be counted - which is the REPORTING
    direction, the safe one for a guard that flags (CLAUDE.md guard rule 3).
    """
    seen: dict[int, ast.Call] = {}
    for node, _origin in resolved_calls(tree, _RESOLVED_TARGET, module=module):
        seen[id(node)] = node
    for candidate in all_nodes(tree):
        if isinstance(candidate, ast.Call) and _is_bare_run_scrubbed_call(candidate):
            seen[id(candidate)] = candidate
    return list(seen.values())


def scan_verify_source(
    where: str, module: str, tree: ast.Module
) -> tuple[dict[str, int], list[str]]:
    """``(census, reported)`` for one module: how many ``run_scrubbed``
    calls it holds, and which ones have no handler for a decode failure."""
    table = bindings(tree, module=module)
    census: dict[str, int] = {}
    reported: list[str] = []
    for node in _run_scrubbed_calls(tree, module=module):
        census[where] = census.get(where, 0) + 1
        if not _guarded_by(tree, node, _ACCEPTED, table):
            reported.append(f"{where}:{node.lineno}")
    return census, reported


def _package_scan() -> tuple[dict[str, int], list[str]]:
    total_census: dict[str, int] = {}
    total_reported: list[str] = []
    for source_file in package_sources():
        where = label(source_file)
        tree = parse(source_file.read_text(encoding="utf-8"))
        census, reported = scan_verify_source(where, module_name(source_file), tree)
        for key, value in census.items():
            total_census[key] = total_census.get(key, 0) + value
        total_reported.extend(reported)
    return total_census, total_reported


_PLANTED_WITHOUT_HANDLER = """
def helper(cmd, cwd):
    result = run_scrubbed(cmd, cwd=cwd, timeout=30.0)
    return result.returncode
"""


def test_the_walk_reports_what_it_could_not_decide() -> None:
    """The undecided half owed by ``resolved_calls``, filtered to the
    ``run_scrubbed`` rows: measured as ``{"verify.py": 10}`` on 1f891f1,
    the ten bare-name calls inside ``verify.py`` itself the resolver
    cannot place, alongside unrelated calls in other modules this guard
    does not own."""
    counts: dict[str, int] = {}
    for source_file in package_sources():
        tree = parse(source_file.read_text(encoding="utf-8"))
        found = calls_to(
            tree,
            _RESOLVED_TARGET,
            where=label(source_file),
            module=module_name(source_file),
        )
        for row in found.undecided:
            if not row.endswith(" run_scrubbed"):
                continue
            where = row.split(":", 1)[0]
            counts[where] = counts.get(where, 0) + 1

    assert counts == {"verify.py": 10}


def test_the_call_site_census_is_pinned() -> None:
    census, _reported = _package_scan()

    assert census == {
        "breaker.py": 1,
        "contract.py": 5,
        "fixtures.py": 2,
        "verify.py": 10,
    }


def test_every_call_site_handles_a_decode_failure() -> None:
    _census, reported = _package_scan()

    assert reported == []


def test_a_new_call_site_without_a_handler_is_reported() -> None:
    tree = parse(_PLANTED_WITHOUT_HANDLER)

    _census, reported = scan_verify_source("planted.py", "planted", tree)

    assert reported == ["planted.py:3"]


def _offending_class(where: str, node: ast.AST) -> tuple[int, str] | None:
    if (
        isinstance(node, ast.ClassDef)
        and node.name == "ChildOutputDecodeError"
        and where != "verify.py"
    ):
        return node.lineno, "class"
    return None


def _offending_import(node: ast.AST) -> tuple[int, str] | None:
    if not isinstance(node, ast.ImportFrom):
        return None
    for alias in node.names:
        bound = alias.asname or alias.name
        if bound == "ChildOutputDecodeError" and node.module != "kstrl.verify":
            return node.lineno, f"import from {node.module}"
    return None


def _offending_assignment(node: ast.AST) -> tuple[int, str] | None:
    if not isinstance(node, ast.Assign):
        return None
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id == "ChildOutputDecodeError":
            return node.lineno, "assignment"
    return None


def _offending_bindings(where: str, tree: ast.Module) -> list[str]:
    """Every binding of ``ChildOutputDecodeError`` in one module that is
    not its ``ClassDef`` in ``verify.py`` or an ``ImportFrom`` from
    ``kstrl.verify``. Split into one helper per node shape so this walk's
    own cyclomatic complexity stays under the repo's ratchet."""
    found: list[str] = []
    for node in all_nodes(tree):
        hit = (
            _offending_class(where, node) or _offending_import(node) or _offending_assignment(node)
        )
        if hit is not None:
            lineno, kind = hit
            found.append(f"{where}:{lineno} {kind}")
    return found


def test_the_error_name_has_one_home() -> None:
    """Every binding of the name ``ChildOutputDecodeError`` in ``kstrl/``
    is either its ``ClassDef`` in ``verify.py`` or an ``ImportFrom`` whose
    module is ``kstrl.verify``. A name is not an identity (#324 round 2)."""
    offenders: list[str] = []
    for source_file in package_sources():
        where = label(source_file)
        tree = parse(source_file.read_text(encoding="utf-8"))
        offenders.extend(_offending_bindings(where, tree))

    assert offenders == []
