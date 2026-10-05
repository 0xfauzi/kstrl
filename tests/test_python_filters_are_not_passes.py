"""Every place kstrl selects its input by Python file name or by ``uv.lock`` (#619).

A check that filters the diff to ``.py`` files, to Python test paths or to
``uv.lock`` and then reports a PASS when the filter leaves nothing has
cleared content it never opened. #619 found four: ``bad_patterns``'s
secret rule, ``test_adequacy``, ``dead_code_ruff`` and the ``[policy]``
dependency and license rules, each passing a ``.rs``/``.ts``/lockfile diff
unread.

This is the census that makes the next one visible. It is closed by
construction over SPELLINGS rather than over call shapes: a selection by
Python file name has to spell the suffix somewhere, whether as
``f.endswith(".py")``, ``path.suffix == ".py"``, ``rglob("*.py")``, a
``\\.py`` inside a regex or ``"uv.lock"``, and every expression whose
folded value is one of those is counted, by module and scope. So is every
spelling of ``TEST_PATH_RE``. A new selection adds a row or moves a count,
and the diff that does so says, in :data:`EXPECTED_SELECTOR_SITES`, what
that scope reports when the selection comes back empty.

It FLAGS rather than clears, so over-matching is the permitted direction:
a ``.py`` constant that selects nothing (a location label, a module-name
strip) is a row somebody reads, not a hole.

WHAT IT CANNOT SEE: a suffix the interpreter assembles at run time, such
as ``"".join([".", "py"])``, folds to nothing (:func:`folded_str` decides
literals, ``+`` and plain f-string pieces only). Pinned below as a strict
expected failure, so a walk that learns to see it fails loudly.

#635 WIDENS IT BY TWO LAYERS, for the language knowledge that is not a
file selection.

The first counts, per module, every expression whose folded value names a
Python tool (:data:`PYTHON_TOOL_TOKENS`). A new command line, parser key
or message that assumes Python moves a count, and its row says whether the
module is the Python record, a Python-only check, or a leak.

The second counts, per scope, every spelling of a name that hands out a
toolchain fact (:data:`OBTAIN_POINTS`): the Python default commands, the
records, the detector and the two lookups built on it.
``kstrl.toolchains.resolve`` is the one reader of a record's commands for a
gate and the ``verify.resolve_*_command`` projections are its callers, so
they are not obtain points. Any other scope that reaches for a default
directly is a new row: ``cmd = DEFAULT_TEST_COMMAND`` inside a gate is the
defect this layer exists to make red.

Both FLAG. What they cannot see is pinned as strict expected failures: a
name or command assembled with ``"".join`` does not fold, and a string that
is a statement on its own (a docstring) is not counted, because nothing
reads its value. A count is per row, so a literal deleted and another added
in the same module moves nothing; the row's reason is what a reader checks.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    folded_str,
    label,
    package_sources,
    parse,
    parsed,
    scope_of,
    spells,
)

#: The whole-value spellings of a Python-file or uv.lock selection.
PYTHON_SELECTORS = frozenset({".py", "*.py", "uv.lock"})

#: A ``.py`` suffix inside a regular expression, as the pattern text holds it.
REGEX_PY_SUFFIX = "\\.py"

_spells_test_path_re = spells("TEST_PATH_RE")


def selects_python(node: ast.AST) -> bool:
    """Does this expression spell a selection by Python file or by uv.lock?"""
    value = folded_str(node)
    if value is not None and (value in PYTHON_SELECTORS or REGEX_PY_SUFFIX in value):
        return True
    return _spells_test_path_re(node)


def _scope_row(source_file: Path, node: ast.AST) -> str:
    """``verify.py: check_bad_patterns`` - the module and the innermost scope."""
    owner = scope_of(parsed(source_file)).get(id(node), "<module>")
    return f"{label(source_file)}: {owner}"


#: Every scope in ``kstrl/`` that spells a Python-file or uv.lock selection,
#: with how many spellings it holds and what it reports when the selection
#: is EMPTY. DERIVED BY RUNNING THIS FILE, never by editing it to match: a
#: moved count is read off the failure's ``Found:`` dict, and the reason is
#: written by whoever added the row.
EXPECTED_SELECTOR_SITES: dict[str, tuple[int, str]] = {
    "adequacy.py: <module>": (
        2,
        "TEST_PATH_RE's own definition, its pattern and its name; the readers are "
        "is_test_path's callers",
    ),
    "adequacy.py: coverage_targets": (
        1,
        "empty targets make check_patch_coverage return NotMeasured no_target",
    ),
    "adequacy.py: is_test_path": (
        1,
        "a predicate over one path; each caller reports its own empty",
    ),
    "adequacy.py: unread_test_paths": (
        1,
        "the complement: non-Python test files, which check_test_adequacy reports as "
        "NotMeasured no_target or names in its row message",
    ),
    "doctor.py: _interface_file_count": (
        1,
        "a count; 0 is reported by check_source_root as a WARN, never OK",
    ),
    "doctor.py: _source_mix_notes": (
        1,
        "a share of tracked source by suffix (#628); a tree with no Python at all "
        "makes check_source_root a WARN ('no tracked file is Python'), never OK",
    ),
    "feedforward.py: <module>": (
        1,
        "_SOURCE_EXTENSIONS, one of seven languages; selects nothing alone",
    ),
    "feedforward.py: _classify_dir": (1, "the Phase 0 scan's source-root probe; no verdict"),
    "feedforward.py: _ordered_source_roots": (
        1,
        "Phase 0 context: an empty scan prints its '(none: no Python source root "
        "found ...)' notice to the engineer, never a pass",
    ),
    "feedforward.py: _path_to_module": (1, "strips the suffix off one path; selects nothing"),
    "feedforward.py: build_dependency_graph": (
        1,
        "Phase 0 context: an empty graph is an empty section, never a check result",
    ),
    "lockfiles.py: parse_new_dependencies": (
        1,
        "empty means no new uv.lock package; read_new_dependencies reads every other "
        "changed lockfile from its blobs or reports it unread with its reason (#630)",
    ),
    "lockfiles.py: uv_lock_dependencies": (
        1,
        "the lockfile label of each uv.lock NewDependency; selects nothing",
    ),
    "policy.py: <module>": (
        2,
        "LOCKFILE_MANIFESTS' uv.lock key, and the import check that LOCKFILE_READERS "
        "plus uv.lock is every lockfile; selects nothing",
    ),
    "policy.py: evaluate_policy": (
        1,
        "the location label of a deps_allow_new violation; selects nothing",
    ),
    "suite_inventory.py: <module>": (
        1,
        "TEST_FILE_PATTERNS, the files pytest and vitest collect by default (#620); a "
        "changed test file no pattern claims has no runner, so unrun_test_files lists it "
        "as not_measured and the tests_ran row never counts it as run",
    ),
    "toolchains.py: <module>": (
        1,
        "the Python record's uv.lock lockfile, which ks init stages; selects nothing",
    ),
    "verify.py: _changed_non_test_python": (
        1,
        "empty makes both callers (mutation and dead-code scan) return NotMeasured no_target",
    ),
    "verify.py: _python_test_sources": (
        1,
        "the Python half of test_adequacy; unread_test_paths covers the rest",
    ),
    "verify.py: check_bad_patterns": (
        1,
        "the Python rules only; the secret rule reads every changed file and the "
        "message names both scopes",
    ),
}


def test_every_python_selection_is_enrolled_with_its_empty_case() -> None:
    assert all(reason.strip() for _count, reason in EXPECTED_SELECTOR_SITES.values())
    assert_census(
        sources=package_sources(),
        sees=selects_python,
        key=_scope_row,
        expected={row: count for row, (count, _reason) in EXPECTED_SELECTOR_SITES.items()},
        # One control per disjunct, spelled out rather than derived from
        # PYTHON_SELECTORS, so shrinking the constant cannot shrink its proof.
        control=(
            'keep = [f for f in changed if f.endswith(".py")]\n',
            'roots = sorted(src.rglob("*.py"))\n',
            'if _basename(path) != "uv.lock":\n    pass\n',
            'RE = re.compile(r"(^|/)test_[^/]*\\.py$")\n',
            "hit = TEST_PATH_RE.search(path)\n",
            'keep = [f for f in changed if f.endswith("." + "py")]\n',
        ),
        message=(
            "A scope in kstrl/ that selects its input by Python file name, Python test "
            "path or uv.lock changed. If it is a check, it must not report a pass when "
            "the selection is empty (#619): read the content with a rule that does not "
            "depend on language, or return NotMeasured with a reason. Then add or move "
            "its row in EXPECTED_SELECTOR_SITES with what the empty case reports."
        ),
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_suffix_assembled_at_run_time_is_not_seen() -> None:
    """Disclosed limit: ``"".join`` does not fold, so this selection is invisible."""
    blind_spot(
        lambda source: any(selects_python(node) for node in ast.walk(parse(source))),
        'SUFFIX = "".join([".", "py"])\nkeep = [f for f in changed if f.endswith(SUFFIX)]\n',
    )


# --- #635: Python tool literals, per module ---------------------------------

#: The Python tools a command line, a parser key or a message can name, and
#: the prefix every Python default command starts with.
PYTHON_TOOL_TOKENS = ("pytest", "mypy", "ruff", "vulture", "mutmut", "uv run")


def names_a_python_tool(node: ast.AST) -> bool:
    """Does this expression fold to a string naming a Python tool?"""
    value = folded_str(node)
    return value is not None and any(token in value for token in PYTHON_TOOL_TOKENS)


def _statement_strings(trees: Iterable[ast.Module]) -> frozenset[int]:
    """The ids of every expression that is a statement on its own.

    A docstring is one. Nothing reads its value, so it is not counted.
    """
    return frozenset(
        id(node.value) for tree in trees for node in all_nodes(tree) if isinstance(node, ast.Expr)
    )


#: One control per token, spelled out rather than derived from
#: PYTHON_TOOL_TOKENS, so shrinking the constant cannot shrink its proof.
_TOOL_CONTROLS = (
    'cmd = "uv run " + "pytest"\n',
    'if parser == "mypy":\n    pass\n',
    'FIX = f"ruff check --fix {path}"\n',
    'RUNNER = "vulture"\n',
    'MSG = "mutmut is not on PATH"\n',
    'LOG = "pytest-junit.xml"\n',
)

#: Every module in ``kstrl/`` whose code names a Python tool, with how many
#: expressions do and what they are. DERIVED BY RUNNING THIS FILE: a moved
#: count is read off the failure's ``Found:`` dict.
EXPECTED_TOOL_LITERALS: dict[str, tuple[int, str]] = {
    "adequacy.py": (5, "Python-only check: pytestmark and mutmut's junitxml report messages"),
    # #696: down from 4. The --test-command/--lint-command help text that
    # named the uv run pytest/ruff defaults is gone with the flags.
    "cli.py": (3, "help text naming the Python-only checks"),
    # #696: contract.py's and doctor.py's rows are gone outright. Both
    # named a retired per-tool command default (contract.py's exit-5
    # message was about [verify] test_suite; doctor.py's was the
    # `uv run` default warning for an unset [verify] key).
    "evolution.py": (4, "_classify_check's keywords for Phase 1 check names"),
    "feature_verify.py": (1, "message naming the dead_code_ruff check"),
    "feedforward.py": (7, "the Phase 0 scan's ruff.toml and [tool.ruff] convention readers"),
    "gateparse.py": (6, "parser registry keys for pytest, mypy and ruff output"),
    # #696: down from 4. kstrl_toml_for, which seeded DEFAULT_KSTRL_TOML's
    # commented-out [verify] command lines from the detected toolchain, is
    # gone with [verify] itself.
    "init_cmd.py": (
        3,
        "BUILD_MANIFEST_FIX's uv commands, and the enrolled Python "
        "standards and CLAUDE.md verification prompts",
    ),
    "parsers.py": (3, "parser names for pytest, mypy and ruff output"),
    "suite_inventory.py": (2, "the pytest junit report the test gate asks for (#620)"),
    # #696: down from 8. The Python record's test/typecheck/lint commands
    # and python_typecheck_default's mypy-scope literals are gone with the
    # command half; what is left is the three Python-tool cache ignores
    # (.pytest_cache/, .mypy_cache/, .ruff_cache/).
    "toolchains.py": (3, "the Python record's cache ignores"),
    "verify.py": (
        41,
        "the Python-only checks (mutation, dead code, patch coverage) and their "
        "messages; each reports NotMeasured on a tree they cannot read. #696 "
        "retired one more: a message of the per-tool gates this flag day removed",
    ),
}


def test_every_python_tool_literal_is_enrolled() -> None:
    assert all(reason.strip() for _count, reason in EXPECTED_TOOL_LITERALS.values())
    sources = package_sources()
    statements = _statement_strings(
        [*(parsed(source) for source in sources), *(parse(one) for one in _TOOL_CONTROLS)]
    )
    assert_census(
        sources=sources,
        sees=lambda node: id(node) not in statements and names_a_python_tool(node),
        expected={row: count for row, (count, _reason) in EXPECTED_TOOL_LITERALS.items()},
        control=_TOOL_CONTROLS,
        message=(
            "A module in kstrl/ that names a Python tool changed (#635). A command belongs "
            "in a kstrl.toolchains record and reaches a gate through toolchains.resolve; a "
            "Python-only check must report NotMeasured where it cannot read the tree. Then "
            "move the module's row in EXPECTED_TOOL_LITERALS and say what the new site is."
        ),
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_tool_name_assembled_at_run_time_is_not_seen() -> None:
    """Disclosed limit: ``"".join`` does not fold, so this command is invisible."""
    blind_spot(
        lambda source: any(names_a_python_tool(node) for node in all_nodes(parse(source))),
        'RUNNER = "".join(["py", "test"])\n',
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_tool_name_in_a_docstring_is_not_counted() -> None:
    """Disclosed limit: a string that is a statement on its own is skipped."""

    def counted(source: str) -> bool:
        tree = parse(source)
        statements = _statement_strings([tree])
        return any(
            id(node) not in statements and names_a_python_tool(node) for node in all_nodes(tree)
        )

    blind_spot(counted, 'def f() -> None:\n    """Runs uv run pytest."""\n')


# --- #635: who reaches for a toolchain fact, per scope -----------------------

#: Every name that hands out a toolchain fact. #696 retired the command
#: half (``DEFAULT_TEST_COMMAND``, ``DEFAULT_LINT_COMMAND``,
#: ``DEFAULT_TYPECHECK_COMMAND``, ``SCOPED_TYPECHECK_COMMAND``,
#: ``toolchains.resolve`` and ``python_typecheck_default``) along with
#: every command kstrl chose for a tree: a confirmed ``[stack]`` is the
#: only source now. What is left is detection.
OBTAIN_POINTS = (
    "TOOLCHAINS",
    "detect",
    "toolchain_named",
    "is_python_project",
)

_OBTAIN_NETS = tuple(spells(point) for point in OBTAIN_POINTS)


def reaches_an_obtain_point(node: ast.AST) -> bool:
    """Does this node spell a name that hands out a toolchain fact?"""
    return any(net(node) for net in _OBTAIN_NETS)


#: Every scope in ``kstrl/`` that spells an obtain point, with how many
#: spellings it holds and why it may. DERIVED BY RUNNING THIS FILE.
#:
#: #696 flag day: cli.py, contract.py, doctor.py and verify.py dropped
#: out of this census entirely - each only reached for a command default
#: (the --test-command/--lint-command help text, ContractConfig's
#: KSTRL_CONTRACT_TEST_CMD fallback, the "Python default on a
#: not-Python tree" refusal), and kstrl chooses no command for any tree
#: now. init_cmd.py's kstrl_toml_for row is gone the same way: it seeded
#: a detected toolchain's commands into the retired [verify].
EXPECTED_OBTAIN_SITES: dict[str, tuple[int, str]] = {
    "decompose.py: <module>": (2, "ROOT_BUILD_MANIFESTS, the union of every record's markers"),
    "fixtures.py: <module>": (1, "imports is_python_project"),
    "fixtures.py: fixture_tree_errors": (1, "a function fixture needs a Python tree (#632)"),
    "init_cmd.py: <module>": (2, "imports detect and toolchain_named"),
    "init_cmd.py: _detect_project_context": (1, "the detected language ks init reports"),
    "init_cmd.py: _ensure_lockfiles_tracked": (1, "the record's lockfiles, which ks init stages"),
    "init_cmd.py: _generate_claude_md": (1, "the record's id keys the enrolled standards bodies"),
    "init_cmd.py: _language_ignores": (1, "the record's ignores, which ks init writes"),
    "toolchains.py: <module>": (6, "the definitions and the Python record"),
    "toolchains.py: detect": (2, "first match wins in TOOLCHAINS order"),
    "toolchains.py: is_python_project": (2, "detect's choice compared with the Python record"),
    "toolchains.py: toolchain_named": (1, "a language string back to its record"),
}


def test_every_scope_that_reaches_for_a_toolchain_fact_is_enrolled() -> None:
    assert all(reason.strip() for _count, reason in EXPECTED_OBTAIN_SITES.values())
    assert_census(
        sources=package_sources(),
        sees=reaches_an_obtain_point,
        key=_scope_row,
        expected={row: count for row, (count, _reason) in EXPECTED_OBTAIN_SITES.items()},
        # One control per obtain point, spelled out for the same reason as above.
        control=(
            'rust = toolchains.TOOLCHAINS["Rust"]\n',
            "found = detect(root)\n",
            "record = toolchain_named(language)\n",
            "if is_python_project(cwd):\n    pass\n",
        ),
        message=(
            "A scope in kstrl/ that reaches for a toolchain record or the detector "
            "changed (#696: kstrl chooses no command for any tree, so this is detection "
            "only now). If the new site is not detection, add or move its row in "
            "EXPECTED_OBTAIN_SITES and say why."
        ),
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_an_obtain_point_named_at_run_time_is_not_seen() -> None:
    """Disclosed limit: a name assembled with ``"".join`` is not a spelling."""
    blind_spot(
        lambda source: any(reaches_an_obtain_point(node) for node in all_nodes(parse(source))),
        'cmd = getattr(toolchains, "".join(["TOOL", "CHAINS"]))\n',
    )
