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
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
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
    "init_cmd.py: <module>": (1, "_LANGUAGE_LOCKFILES, ks init's language detection"),
    "policy.py: <module>": (1, "LOCKFILE_MANIFESTS' uv.lock key; every lockfile is listed"),
    "policy.py: evaluate_policy": (
        1,
        "the location label of a deps_allow_new violation; selects nothing",
    ),
    "policy.py: parse_new_dependencies": (
        1,
        "empty means no new uv.lock package; unread_lockfile_violations reports every "
        "other lockfile the diff adds lines to",
    ),
    "policy.py: unread_lockfiles": (
        1,
        "the complement of parse_new_dependencies; the verifier reports each lockfile "
        "it lists as unmeasured dependency rules",
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
