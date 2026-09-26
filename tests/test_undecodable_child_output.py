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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

import pytest

from kstrl import breaker, contract, fixtures
from kstrl.fixtures import Fixture
from kstrl.verify import (
    CheckResult,
    ChildOutputDecodeError,
    check_linter,
    check_test_suite,
    check_typecheck,
    run_scrubbed,
)
from tests.helpers.astwalk import (
    Bindings,
    all_nodes,
    bindings,
    calls_to,
    guarded_by,
    handler_clauses,
    label,
    module_name,
    package_sources,
    parse,
    resolved_calls,
)
from tests.helpers.astwalk.scope import own_nodes, try_body_nodes

#: A child that writes one latin-1 byte and exits 0. `python3 -c` rather than
#: `printf`, so the byte is written as BYTES on any shell. The word is
#: encoded rather than escaped, for the codespell reason
#: ``tests/test_undecodable_diff.py`` states.
UNDECODABLE_STDOUT = (
    "python3 -c \"import sys; sys.stdout.buffer.write('café'.encode('latin-1') + b'\\n')\""
)
UNDECODABLE_STDERR = (
    "python3 -c \"import sys; sys.stderr.buffer.write('café'.encode('latin-1') + b'\\n')\""
)


def test_run_scrubbed_refuses_output_it_cannot_decode(tmp_path: Path) -> None:
    with pytest.raises(ChildOutputDecodeError) as excinfo:
        run_scrubbed(UNDECODABLE_STDOUT, cwd=tmp_path, timeout=60.0)

    assert "not valid utf-8" in str(excinfo.value)
    assert "0xe9" in str(excinfo.value)
    assert "position" in str(excinfo.value)


def test_stderr_is_the_same_failure(tmp_path: Path) -> None:
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
    """``kstrl/fixtures.py::run_cli_fixture`` widens the existing ``except
    OSError`` clause rather than adding a new one, because one more branch
    fails both pre-commit complexity ratchets (see the comment at
    ``kstrl/fixtures.py:182-187``). So the row keeps its "Failed to run
    command: {exc}" body and the exception's own text is what names the
    fault - asserted on the exception's half of the string, not on "Failed
    to run command", so this would still fail if the clause caught the
    error and said nothing about it."""
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

#: The exact spelling only (#416's round-two review). The CPython-derived
#: set this replaced (``BaseException``, ``Exception``, ``RuntimeError``,
#: plus the name itself) and this one narrow name give byte-identical
#: censuses on the real package (``{"breaker.py": 1, "contract.py": 5,
#: "fixtures.py": 2, "verify.py": 10}``, ``reported == []`` either way), so
#: the wider set bought nothing but a clearing hole: ``except Exception:
#: pass`` cleared under it - a call site with no verdict, exactly what
#: ``run_scrubbed``'s decode conversion exists to prevent. A guard that
#: CLEARS must be narrow (CLAUDE.md).
_ACCEPTED = frozenset({"ChildOutputDecodeError"})

#: The target: every call to ``run_scrubbed`` in ``kstrl/``. ``resolved_calls``
#: with this target finds only the sites outside ``verify.py``; the sites
#: inside call the bare local name, which the resolver does not place. The
#: union with the bare-name walk below is built in ``_run_scrubbed_calls``.
_RESOLVED_TARGET = frozenset({"kstrl.verify.run_scrubbed"})


def _not_a_bare_reraise(handler: ast.ExceptHandler) -> bool:
    """Clears unless the handler's only observable act is handing the same
    exception straight back out unchanged.

    ``except ChildOutputDecodeError: raise`` catches the name and lets the
    original exception continue exactly as if the clause were not there -
    measured clearing under a guard that asked only what a clause NAMES
    (#416's round-two review). ``tests/helpers/encodingguards.py``'s own
    ``_reraises`` names this question for the sibling encoding guard, but
    over a whole ``try``'s handlers, to decide whether to keep looking
    OUTWARD; this asks it of the ONE handler that names the target, which
    is what ``guarded_by``'s ``handler_converts`` hook needs answered. A
    bare ``raise`` has no ``.exc``; ``raise ChildOutputDecodeError(...)``
    does and is a conversion, not a re-raise.
    """
    return not any(isinstance(n, ast.Raise) and n.exc is None for n in own_nodes(handler))


def _handler_disposition(handler: ast.ExceptHandler) -> str:
    """What a clearing clause DOES with the decode: "raises" a new
    exception, "returns" a result the caller reads, "swallows" it with no
    observable effect, or "converts" it into state a later statement in
    the same function reads (measured: ``verify.py``'s mutation-report
    reader sets ``report_error`` and returns nothing itself; the ``if
    report is None`` branch after the ``try`` is what reads it).

    A swallow is legitimate at two sites - ``contract._abort_merge`` and
    the prune call in ``contract._remove_temp_worktree``, whose results
    are never read (#416's simplify review) - so this is a census, not a
    ban: a new swallowing site is then an unexplained delta rather than a
    silent clear.
    """
    if all(isinstance(statement, ast.Pass) for statement in handler.body):
        return "swallows"
    if any(isinstance(n, ast.Raise) for n in own_nodes(handler)):
        return "raises"
    if any(isinstance(n, ast.Return) for n in own_nodes(handler)):
        return "returns"
    return "converts"


def _is_bare_run_scrubbed_call(node: ast.AST) -> TypeGuard[ast.Call]:
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
        if _is_bare_run_scrubbed_call(candidate):
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
        if not guarded_by(tree, node, _ACCEPTED, table, handler_converts=_not_a_bare_reraise):
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


def _covering_handler(tree: ast.Module, node: ast.AST, table: Bindings) -> ast.ExceptHandler | None:
    """The handler ``guarded_by`` would credit for clearing ``node``, so a
    caller can ask what its BODY does rather than only whether it exists.
    Same walk as ``guarded_by``, restricted to the clause that both names
    the target and is not a bare re-raise, which is exactly the set of
    handlers a clearing site relies on."""
    for candidate in all_nodes(tree):
        if not isinstance(candidate, ast.Try | ast.TryStar):
            continue
        if not any(body is node for body in try_body_nodes(candidate)):
            continue
        for handler, clause in zip(
            candidate.handlers, handler_clauses(candidate, table), strict=True
        ):
            if clause.decided and clause.names & _ACCEPTED and _not_a_bare_reraise(handler):
                return handler
    return None


def _disposition_census() -> dict[str, int]:
    """Per-file counts of what each clearing site's handler DOES, keyed
    ``"<file>:<disposition>"``. A swallow is legitimate at two known sites
    (see ``_handler_disposition``); this is how a THIRD one would be
    caught, as an unexplained delta rather than a silent clear."""
    counts: dict[str, int] = {}
    for source_file in package_sources():
        where = label(source_file)
        tree = parse(source_file.read_text(encoding="utf-8"))
        table = bindings(tree, module=module_name(source_file))
        for node in _run_scrubbed_calls(tree, module=module_name(source_file)):
            handler = _covering_handler(tree, node, table)
            if handler is None:
                continue
            key = f"{where}:{_handler_disposition(handler)}"
            counts[key] = counts.get(key, 0) + 1
    return counts


_PLANTED_WITHOUT_HANDLER = """
def helper(cmd, cwd):
    result = run_scrubbed(cmd, cwd=cwd, timeout=30.0)
    return result.returncode
"""

#: A clause that NAMES the target and hands it straight back out unchanged.
#: #416's round-two review measured this clearing a call site under a guard
#: that asked only what a clause CATCHES, although nothing is handled and
#: the exception still reaches the caller - the property
#: ``tests/helpers/encodingguards.py``'s own ``_reraises`` names for the
#: sibling encoding guard. ``_not_a_bare_reraise`` is what reports it.
_PLANTED_BARE_RERAISE = """
def helper(cmd, cwd):
    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=30.0)
    except ChildOutputDecodeError:
        raise
    return result.returncode
"""

#: A NEW swallow: legitimate at exactly two known real sites (see
#: ``_handler_disposition``'s docstring), so the guard clears it rather
#: than reporting it - but it is not silent, because the disposition
#: census this site adds is a key no real file contributes.
_PLANTED_NEW_SWALLOW = """
def helper(cmd, cwd):
    try:
        run_scrubbed(cmd, cwd=cwd, timeout=30.0)
    except ChildOutputDecodeError:
        pass
"""

#: The exact shape #416's round-two review measured clearing under the
#: CPython-derived accepted set (``except Exception: pass``): a clause
#: naming ``Exception``, not the exact target, that swallows. Unlike guard
#: 1's ``handler_converts`` (which independently forbids a swallow no
#: matter what the clause names), guard 2's ``_not_a_bare_reraise`` does
#: NOT forbid a swallow - it only forbids a bare re-raise - so this
#: specific vacuous clear is caught by the exact-spelling accepted set
#: (``_ACCEPTED_NAMES`` / ``_ACCEPTED``) alone, not by
#: ``handler_converts``.
_PLANTED_UNRELATED_EXCEPTION = """
def helper(cmd, cwd):
    try:
        run_scrubbed(cmd, cwd=cwd, timeout=30.0)
    except Exception:
        pass
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
        "learning_fixture.py": 1,
        "verify.py": 10,
        "worktree_sweep.py": 1,
    }


def test_every_call_site_handles_a_decode_failure() -> None:
    _census, reported = _package_scan()

    assert reported == []


def test_a_new_call_site_without_a_handler_is_reported() -> None:
    tree = parse(_PLANTED_WITHOUT_HANDLER)

    _census, reported = scan_verify_source("planted.py", "planted", tree)

    assert reported == ["planted.py:3"]


def test_a_bare_reraise_does_not_clear_the_site() -> None:
    """Naming ``ChildOutputDecodeError`` is not handling it. #416's
    round-two review measured ``except ChildOutputDecodeError: raise``
    clearing this site under a guard that asked only what the clause
    catches; ``_not_a_bare_reraise`` is what reports it."""
    tree = parse(_PLANTED_BARE_RERAISE)

    _census, reported = scan_verify_source("planted.py", "planted", tree)

    assert reported == ["planted.py:4"]


def test_a_new_swallow_clears_but_shows_up_as_a_census_delta() -> None:
    """A swallow is legitimate at two known sites, so the guard does not
    report one outright - but it is not silent either: the disposition
    census this planted site adds is a key no real file contributes,
    which is how a THIRD real swallow would be caught."""
    tree = parse(_PLANTED_NEW_SWALLOW)
    table = bindings(tree, module="planted")
    node = next(n for n in all_nodes(tree) if _is_bare_run_scrubbed_call(n))

    _census, reported = scan_verify_source("planted.py", "planted", tree)
    handler = _covering_handler(tree, node, table)

    assert reported == []
    assert handler is not None
    assert _handler_disposition(handler) == "swallows"


def test_a_clause_naming_an_unrelated_broad_exception_is_reported() -> None:
    """The exact shape #416's round-two review measured clearing under the
    CPython-derived accepted set: ``except Exception: pass``. Guard 2's
    ``_not_a_bare_reraise`` does not forbid a swallow on its own, so this
    site is caught by the exact-spelling accepted set (``_ACCEPTED``)
    alone: ``clause.names`` is ``{"Exception"}``, which does not overlap
    ``_ACCEPTED`` at all."""
    tree = parse(_PLANTED_UNRELATED_EXCEPTION)

    _census, reported = scan_verify_source("planted.py", "planted", tree)

    assert reported == ["planted.py:4"]


def test_the_disposition_census_is_pinned() -> None:
    """What each of the 18 clearing sites' handler DOES, re-derived by
    running rather than assumed. Two swallows, both in ``contract.py``
    (``_abort_merge`` and the prune call in ``_remove_temp_worktree``,
    whose results are never read); one ``verify.py`` site CONVERTS the
    decode into a variable a later statement reads (the mutation-report
    reader sets ``report_error`` and returns nothing itself - the ``if
    report is None`` branch after the ``try`` is what reads it) rather
    than returning or raising from inside the handler; the rest return a
    result or raise. #416's simplify review measured two swallows and
    stopped there; this census's own walk found the third shape and
    reports it honestly as ``converts`` rather than folding it into
    "swallows", which would have hidden it from a future comparison."""
    assert _disposition_census() == {
        "breaker.py:returns": 1,
        "contract.py:raises": 1,
        "contract.py:returns": 2,
        "contract.py:swallows": 2,
        "fixtures.py:returns": 2,
        "learning_fixture.py:raises": 1,
        "verify.py:converts": 1,
        "verify.py:returns": 9,
        "worktree_sweep.py:returns": 1,
    }


def _offending_class(where: str, node: ast.AST) -> str | None:
    if not isinstance(node, ast.ClassDef) or node.name != "ChildOutputDecodeError":
        return None
    if where == "verify.py":
        return None
    return f"{where}:{node.lineno} class"


def _offending_binding(where: str, module: str, tree: ast.Module) -> list[str]:
    """Every binding of ``ChildOutputDecodeError`` in one module that is
    not its ``ClassDef`` in ``verify.py`` or a resolved origin of
    ``kstrl.verify.ChildOutputDecodeError``.

    Built on ``astwalk.bindings`` rather than hand-rolled per shape
    (#416's reuse review): the walk this replaced checked only
    ``ast.ImportFrom`` and a plain ``ast.Assign``, and MISSED three shapes
    ``bindings`` already resolves - ``import x as
    ChildOutputDecodeError``, an annotated assignment, and a walrus - all
    reported here in the fix. ``bindings`` deliberately does not bind a
    ``ClassDef`` (its docstring says why), so that check stays separate.
    """
    found: list[str] = []
    for node in all_nodes(tree):
        hit = _offending_class(where, node)
        if hit is not None:
            found.append(hit)
    table = bindings(tree, module=module)
    origin = table.origins.get("ChildOutputDecodeError")
    if origin is not None and origin != "kstrl.verify.ChildOutputDecodeError":
        found.append(f"{where}: bound to {origin}")
    return found


def test_the_error_name_has_one_home() -> None:
    """Every binding of the name ``ChildOutputDecodeError`` in ``kstrl/``
    is either its ``ClassDef`` in ``verify.py`` or resolves to
    ``kstrl.verify.ChildOutputDecodeError``. A name is not an identity
    (#324 round 2)."""
    offenders: list[str] = []
    for source_file in package_sources():
        where = label(source_file)
        tree = parse(source_file.read_text(encoding="utf-8"))
        offenders.extend(_offending_binding(where, module_name(source_file), tree))

    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import shutil as ChildOutputDecodeError\n", id="import-as"),
        pytest.param("import shutil\nChildOutputDecodeError: object = shutil\n", id="annassign"),
        pytest.param("import shutil\nx = (ChildOutputDecodeError := shutil)\n", id="walrus"),
    ],
)
def test_a_rebinding_shape_the_hand_rolled_walk_missed_is_reported(source: str) -> None:
    """The three shapes ``astwalk.bindings`` resolves and the walk this
    replaced (a plain ``ast.ImportFrom``/``ast.Assign`` only) did not
    (#416's reuse review, measured on this exact tree)."""
    tree = parse(source)

    offenders = _offending_binding("other.py", "kstrl.other", tree)

    assert offenders != []


# --- #527: the refused output is carried, and nothing else changed -------


@pytest.mark.parametrize(
    ("command", "stdout", "stderr"),
    [
        (
            "python3 -c \"import sys; sys.stdout.buffer.write(b'out kstrl527 \\\\xe9\\\\n'); "
            "sys.stderr.buffer.write(b'err kstrl527\\\\n')\"",
            "out kstrl527 \\xe9\n",
            "err kstrl527\n",
        ),
        (
            "python3 -c \"import sys; sys.stdout.buffer.write(b'out kstrl527\\\\n'); "
            "sys.stderr.buffer.write(b'err kstrl527 \\\\xe9\\\\n')\"",
            "out kstrl527\n",
            "err kstrl527 \\xe9\n",
        ),
    ],
    ids=["bad-stdout", "bad-stderr"],
)
def test_the_decode_error_carries_both_streams(
    tmp_path: Path, command: str, stdout: str, stderr: str
) -> None:
    """Whichever stream holds the bad byte, BOTH reach the exception, the
    bad byte written as an escape. Text mode discarded both (#527)."""
    with pytest.raises(ChildOutputDecodeError) as excinfo:
        run_scrubbed(command, cwd=tmp_path, timeout=60.0)

    assert excinfo.value.stdout == stdout
    assert excinfo.value.stderr == stderr


def test_run_scrubbed_decodes_exactly_as_text_mode_did(tmp_path: Path) -> None:
    """``run_scrubbed`` reads bytes and decodes them itself (#527), so its
    result must be what CPython's text mode gives for the same child:
    utf-8, and ``\\r\\n`` and ``\\r`` read as ``\\n``. Measured against a
    real text-mode run rather than against constants written here."""
    command = [
        sys.executable,
        "-c",
        "import sys; "
        "sys.stdout.buffer.write('a\\r\\nb\\rc \\u00e9\\n'.encode('utf-8')); "
        "sys.stderr.buffer.write(b'x\\r\\ny\\rz\\n')",
    ]
    text_mode = subprocess.run(
        command, cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=60.0, check=False
    )

    result = run_scrubbed(command, cwd=tmp_path, timeout=60.0)

    assert result.stdout == text_mode.stdout == "a\nb\nc \u00e9\n"
    assert result.stderr == text_mode.stderr == "x\ny\nz\n"


def test_a_timeout_carries_what_the_child_printed_as_text(tmp_path: Path) -> None:
    """``run_scrubbed`` stands in for a text-mode run, so the timeout it
    re-raises carries ``str``, the refused byte escaped and ``\\r\\n`` read
    as ``\\n``, never the raw bytes the drain returned (#527)."""
    command = [
        sys.executable,
        "-c",
        "import sys, time; "
        "sys.stdout.buffer.write(b'out kstrl527 \\xff\\r\\n'); sys.stdout.flush(); "
        "sys.stderr.buffer.write(b'err kstrl527\\n'); sys.stderr.flush(); "
        "time.sleep(60)",
    ]
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_scrubbed(command, cwd=tmp_path, timeout=1.0, term_grace=1.0)

    assert excinfo.value.output == "out kstrl527 \\xff\n"
    assert excinfo.value.stderr == "err kstrl527\n"
