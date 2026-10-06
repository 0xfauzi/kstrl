"""``ks check`` on a diff with no Python in it (#619).

Four Phase 1 checks chose their input by Python file name or by
``uv.lock`` and reported a pass when that choice left nothing: a secret
added in a ``.rs`` or ``.ts`` file, a new TypeScript test file, a tree
with no Python for ruff, and a new npm or cargo dependency all passed
having been read by nobody. Each test here builds a real git repository
holding only Rust, TypeScript or lockfile content, drives the real
command through ``CliRunner`` and reads the ``--json`` document back.
The verify commands are no-op Python one-liners, so no Rust or Node
toolchain is needed.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.helpers.stack_confirmation import confirm_stack, write_stack
from tests.spine_utils import git

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"

#: Built by concatenation, never as one literal: the assembled spelling is
#: what the gitleaks pre-commit hook refuses to let be committed.
AWS_KEY = "AKIA" + "A" * 16

RUST_BASE = {
    "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
    "src/lib.rs": "pub fn a() -> i32 {\n    1\n}\n",
}
TS_BASE = {
    "package.json": '{"name": "p", "version": "0.1.0"}\n',
    "src/a.ts": "export const a = 1;\n",
}
PY_BASE = {
    "pyproject.toml": '[project]\nname = "p"\nversion = "0.1.0"\n',
    "src/a.py": "A = 1\n",
}

POLICY = "[policy]\nenabled = true\n"
ADEQUACY = "[adequacy]\nenabled = true\n"


def _write(root: Path, files: dict[str, str | None]) -> None:
    """Write each file; a ``None`` text deletes it."""
    for rel, text in files.items():
        path = root / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _repo(
    tmp_path: Path,
    base: dict[str, str | None],
    branch: dict[str, str | None],
    config: str = "",
) -> Path:
    """A repo whose ``main`` holds ``base`` and whose ``feature`` adds ``branch``.

    ``config`` is appended after the confirmed ``[stack]`` table, so a
    ``[section]`` header starts its own table.
    """
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    _write(root, base)
    write_stack(root, {"tests": _OK_COMMAND, "typecheck": _OK_COMMAND, "lint": _OK_COMMAND})
    if config:
        path = root / "kstrl.toml"
        path.write_text(path.read_text(encoding="utf-8") + config, encoding="utf-8")
    confirm_stack(root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    git("checkout", "-q", "-b", "feature", cwd=root)
    _write(root, branch)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "change", cwd=root)
    return root


def _check_json(root: Path, *extra: str) -> dict[str, Any]:
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json", *extra])
    assert result.exit_code in (0, 1), result.output
    document: dict[str, Any] = json.loads(result.stdout)
    return document


def _row(document: dict[str, Any], name: str) -> dict[str, Any]:
    rows = [c for c in document["checks"] if c["name"] == name]
    assert len(rows) == 1, f"expected one {name!r} row, got {rows!r}"
    return rows[0]


def _gaps(document: dict[str, Any], name: str) -> list[dict[str, str]]:
    return [g for g in document["not_measured"] if g["check"] == name]


# --- bad_patterns: the secret rule reads every changed file ----------------


def test_a_secret_added_in_a_rust_file_fails_bad_patterns(tmp_path: Path) -> None:
    root = _repo(tmp_path, RUST_BASE, {"src/pricing.rs": f'pub const KEY: &str = "{AWS_KEY}";\n'})

    row = _row(_check_json(root), "bad_patterns")

    assert row["passed"] is False
    assert row["details"] == ["src/pricing.rs: possible secret/credential detected"]


def test_a_secret_added_in_a_typescript_file_fails_bad_patterns(tmp_path: Path) -> None:
    root = _repo(tmp_path, TS_BASE, {"src/key.ts": f'export const KEY = "{AWS_KEY}";\n'})

    row = _row(_check_json(root), "bad_patterns")

    assert row["passed"] is False
    assert row["details"] == ["src/key.ts: possible secret/credential detected"]


def test_a_secret_added_in_a_python_file_still_fails_bad_patterns(tmp_path: Path) -> None:
    """The control: the Python path is unchanged."""
    root = _repo(tmp_path, PY_BASE, {"src/key.py": f'KEY = "{AWS_KEY}"\n'})

    row = _row(_check_json(root), "bad_patterns")

    assert row["passed"] is False
    assert row["details"] == ["src/key.py: possible secret/credential detected"]


def test_bad_patterns_on_a_non_python_diff_names_both_scopes(tmp_path: Path) -> None:
    """The message says what each rule read, and "no issues" only for the
    rule that read something."""
    root = _repo(tmp_path, RUST_BASE, {"src/lib.rs": "pub fn a() -> i32 {\n    2\n}\n"})

    row = _row(_check_json(root), "bad_patterns")

    assert row["passed"] is True
    assert row["message"] == (
        "secrets: scanned 1 changed files, no issues; "
        "Python rules: scanned 0 of 0 changed Python files"
    )


# --- test_adequacy: tests Layer 0 cannot read are not a pass ---------------


def test_a_new_typescript_test_file_is_not_a_passing_adequacy_row(tmp_path: Path) -> None:
    root = _repo(tmp_path, TS_BASE, {"src/bulk.test.ts": "expect(true).toBe(true);\n"}, ADEQUACY)

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "test_adequacy"] == []
    assert _gaps(document, "test_adequacy") == [
        {
            "check": "test_adequacy",
            "reason": "no_target",
            "detail": "test adequacy reads Python test files only; not read: src/bulk.test.ts",
        }
    ]


def test_a_new_rust_cfg_test_module_is_not_a_passing_adequacy_row(tmp_path: Path) -> None:
    """Rust keeps unit tests in the source file, so no path rule sees them:
    the added ``#[cfg(test)]`` line is what marks ``src/lib.rs``."""
    branch = {
        "src/lib.rs": (
            "pub fn a() -> i32 {\n    1\n}\n\n"
            "#[cfg(test)]\nmod tests {\n    #[test]\n"
            "    fn t() {\n        assert!(true);\n    }\n}\n"
        )
    }
    root = _repo(tmp_path, RUST_BASE, branch, ADEQUACY)

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "test_adequacy"] == []
    assert _gaps(document, "test_adequacy") == [
        {
            "check": "test_adequacy",
            "reason": "no_target",
            "detail": "test adequacy reads Python test files only; not read: src/lib.rs",
        }
    ]


# --- dead_code_ruff: no Python file is no measurement ----------------------


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff on PATH")
def test_dead_code_ruff_on_a_tree_with_no_python_is_not_measured(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        RUST_BASE,
        {"src/lib.rs": "pub fn a() -> i32 {\n    2\n}\n"},
        "[verify]\ndead_code_cleanup = true\n",
    )

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "dead_code_ruff"] == []
    assert _gaps(document, "dead_code_ruff") == [
        {
            "check": "dead_code_ruff",
            "reason": "no_target",
            "detail": "no Python file for ruff F401/F811/F841 to read",
        }
    ]


# --- policy_envelope and diff_scope: kstrl reads no lockfile (#696 slice 9) ---

CARGO_LOCK = (
    '[[package]]\nname = "serde"\nversion = "1.0.0"\n'
    'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
)


@pytest.mark.parametrize(
    ("base", "lockfile", "text"),
    [
        (RUST_BASE, "Cargo.lock", CARGO_LOCK),
        (
            TS_BASE,
            "package-lock.json",
            '{\n  "packages": {\n    "node_modules/left-pad": {"version": "1.3.0"}\n  }\n}\n',
        ),
        (PY_BASE, "uv.lock", 'version = 1\n\n[[package]]\nname = "six"\nversion = "1.17.0"\n'),
        (TS_BASE, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n"),
    ],
    ids=["cargo", "npm", "uv", "pnpm"],
)
def test_a_new_dependency_in_any_lockfile_is_not_a_policy_finding(
    tmp_path: Path, base: dict[str, str | None], lockfile: str, text: str
) -> None:
    """kstrl reads no lockfile, of any ecosystem: a lockfile that names a new
    package is a changed file like any other, and the security reviewer, not
    the envelope, lists the dependency. Before slice 9 each of these four
    failed the row, naming the package or calling the lockfile unread."""
    root = _repo(tmp_path, base, {lockfile: text}, POLICY)

    row = _row(_check_json(root), "policy_envelope")

    assert row["passed"] is True, row
    assert row["details"] == [], row
    assert row["message"].startswith("policy envelope satisfied (1 files, "), row


def test_a_lockfile_written_beside_an_unchanged_manifest_is_outside_the_allowed_paths(
    tmp_path: Path,
) -> None:
    """kstrl holds no table of which file a manifest's toolchain writes, so a
    lockfile is in scope only when the allowed paths cover it. Before slice 9
    a Cargo.lock new beside a Cargo.toml the change left alone was cleared."""
    branch = {"src/lib.rs": "pub fn a() -> i32 {\n    2\n}\n", "Cargo.lock": CARGO_LOCK}
    root = _repo(tmp_path, RUST_BASE, branch)

    row = _row(_check_json(root, "--allowed-path", "src/"), "diff_scope")

    assert row["passed"] is False, row
    assert any("Cargo.lock" in detail for detail in row["details"]), row
    assert not any("src/lib.rs" in detail for detail in row["details"]), row


def test_a_deleted_python_test_is_still_reported_beside_a_new_typescript_test(
    tmp_path: Path,
) -> None:
    """A Layer 0 finding is never traded for a not-measured record, and the
    row still names the test file it did not read."""
    base = {**TS_BASE, "tests/test_a.py": "def test_a() -> None:\n    assert 1 + 1 == 2\n"}
    branch = {"tests/test_a.py": None, "src/bulk.test.ts": "expect(true).toBe(true);\n"}
    root = _repo(tmp_path, base, branch, ADEQUACY)

    document = _check_json(root)

    row = _row(document, "test_adequacy")
    assert row["message"] == "2 test-adequacy finding(s) [advisory]; not read: src/bulk.test.ts"
    assert _gaps(document, "test_adequacy") == []


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff on PATH")
def test_dead_code_ruff_that_cannot_load_its_config_is_a_failure_not_an_empty_tree(
    tmp_path: Path,
) -> None:
    """A ruff that fails before listing any file is reported as a failed
    command, never as a tree with no Python in it."""
    root = _repo(
        tmp_path,
        {**PY_BASE, "ruff.toml": 'line-length = "x"\n'},
        {"src/a.py": "A = 2\n"},
        "[verify]\ndead_code_cleanup = true\n",
    )

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "dead_code_ruff"] == []
    gaps = _gaps(document, "dead_code_ruff")
    assert [g["reason"] for g in gaps] == ["command_failed"]
    assert gaps[0]["detail"].startswith("ruff check exited 2")


def test_a_python_test_beside_a_typescript_test_names_the_file_it_did_not_read(
    tmp_path: Path,
) -> None:
    """A passing row over Python tests still says which tests it did not open."""
    branch = {
        "tests/test_b.py": "def test_b() -> None:\n    assert abs(-2) == 2\n",
        "src/bulk.test.ts": "expect(true).toBe(true);\n",
    }
    root = _repo(tmp_path, TS_BASE, branch, ADEQUACY)

    document = _check_json(root)

    row = _row(document, "test_adequacy")
    assert row["passed"] is True
    assert row["message"] == (
        "test adequacy: 1 changed Python test file(s), no weakening signals; "
        "not read: src/bulk.test.ts"
    )
    assert _gaps(document, "test_adequacy") == []


# --- test_adequacy: the #631 diff table for Rust, JS/TS and Go -------------
#
# Layer 0's diff half reads paths TEST_PATH_RE does not match through a
# table of each language's test syntax: in-source Rust tests, vitest/jest
# files and Go ``_test.go`` files. Its findings are advisory at every level.

BLOCK = '[adequacy]\nenabled = true\nlayer0 = "block"\n'
DELETED = (
    "test removed by this diff; deleting a test is a change to what the suite "
    "guarantees and needs a spec-linked reason"
)
LOST = (
    "1 more assertion line(s) removed than added; if this is a consolidation say so, "
    "otherwise the suite got weaker"
)
GO_BASE = {
    "go.mod": "module p\n\ngo 1.21\n",
    "a.go": "package p\n\nfunc A(x int) int { return x }\n",
}


def _rust_lib(*tests: str) -> str:
    """``src/lib.rs`` with one ``#[cfg(test)]`` module holding ``tests``."""
    return (
        "pub fn a(x: i32) -> i32 {\n    x\n}\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n\n" + "\n".join(tests) + "}\n"
    )


def _rust_test(name: str, arg: int, attributes: str = "    #[test]\n") -> str:
    return f"{attributes}    fn {name}() {{\n        assert_eq!(a({arg}), {arg});\n    }}\n"


def _vitest(*tests: tuple[str, str, int]) -> str:
    """A vitest file; each test is ``(declaration, name, argument)``."""
    head = "import { expect, it } from 'vitest';\nimport { a } from './a';\n\n"
    return head + "\n".join(
        f"{decl}('{name}', () => {{\n  expect(a({arg})).toBe({arg});\n}});\n"
        for decl, name, arg in tests
    )


def _go_tests(*tests: tuple[str, int, bool]) -> str:
    """A Go test file; each test is ``(name, argument, skipped)``."""
    body = "\n".join(
        f"func {name}(t *testing.T) {{\n"
        + ('\tt.Skip("later")\n' if skipped else "")
        + f'\tif A({arg}) != {arg} {{\n\t\tt.Fatal("wrong")\n\t}}\n}}\n'
        for name, arg, skipped in tests
    )
    return 'package p\n\nimport "testing"\n\n' + body


def _no_target(path: str) -> list[dict[str, str]]:
    return [
        {
            "check": "test_adequacy",
            "reason": "no_target",
            "detail": f"test adequacy reads Python test files only; not read: {path}",
        }
    ]


def _severities(row: dict[str, Any]) -> list[tuple[str, str, str]]:
    return sorted((f["location"], f["category"], f["severity"]) for f in row["findings"])


def test_a_rust_test_replaced_under_a_shared_test_attribute_is_an_advisory_deletion(
    tmp_path: Path,
) -> None:
    """git aligns the ``#[test]`` both tests carry as a context line, so the
    removed ``fn gone`` has no removed attribute above it."""
    base = {**RUST_BASE, "src/lib.rs": _rust_lib(_rust_test("gone", 3))}
    root = _repo(tmp_path, base, {"src/lib.rs": _rust_lib(_rust_test("more", 4))}, ADEQUACY)

    document = _check_json(root)

    row = _row(document, "test_adequacy")
    assert row["passed"] is True
    assert row["message"] == "1 test-adequacy finding(s) [advisory]; not read: src/lib.rs"
    assert row["details"] == [f"src/lib.rs::gone: {DELETED}"]
    assert _severities(row) == [("src/lib.rs", "adequacy_test_deleted", "advisory")]
    assert _gaps(document, "test_adequacy") == []


def test_an_added_rust_ignore_is_a_skipped_test(tmp_path: Path) -> None:
    base = {**RUST_BASE, "src/lib.rs": _rust_lib(_rust_test("keeps", 3))}
    ignored = _rust_test("keeps", 3, attributes="    #[test]\n    #[ignore]\n")
    root = _repo(tmp_path, base, {"src/lib.rs": _rust_lib(ignored)}, ADEQUACY)

    row = _row(_check_json(root), "test_adequacy")

    assert row["message"] == "1 test-adequacy finding(s) [advisory]; not read: src/lib.rs"
    assert row["details"] == ["src/lib.rs: skip/xfail added: #[ignore]"]
    assert _severities(row) == [("src/lib.rs", "adequacy_test_skipped", "advisory")]


def test_a_deleted_and_a_skipped_vitest_test_are_each_reported_once(tmp_path: Path) -> None:
    """``it('keeps')`` turned into ``it.skip('keeps')`` is a skip, not a deletion."""
    base = {**TS_BASE, "src/a.test.ts": _vitest(("it", "gone", 1), ("it", "keeps", 2))}
    branch = {"src/a.test.ts": _vitest(("it.skip", "keeps", 2))}
    root = _repo(tmp_path, base, branch, ADEQUACY)

    document = _check_json(root)

    row = _row(document, "test_adequacy")
    assert row["passed"] is True
    assert row["message"] == "3 test-adequacy finding(s) [advisory]; not read: src/a.test.ts"
    assert row["details"] == [
        f"src/a.test.ts::gone: {DELETED}",
        "src/a.test.ts: skip/xfail added: it.skip('keeps', () => {",
        f"src/a.test.ts: {LOST}",
    ]
    assert _gaps(document, "test_adequacy") == []


def test_a_deleted_go_test_and_an_added_t_skip_are_reported(tmp_path: Path) -> None:
    base = {**GO_BASE, "a_test.go": _go_tests(("TestGone", 1, False), ("TestKeeps", 2, False))}
    root = _repo(tmp_path, base, {"a_test.go": _go_tests(("TestKeeps", 2, True))}, ADEQUACY)

    row = _row(_check_json(root), "test_adequacy")

    assert row["passed"] is True
    assert row["message"] == "3 test-adequacy finding(s) [advisory]; not read: a_test.go"
    assert row["details"] == [
        f"a_test.go::TestGone: {DELETED}",
        'a_test.go: skip/xfail added: t.Skip("later")',
        f"a_test.go: {LOST}",
    ]


@pytest.mark.parametrize(
    ("base", "branch", "path"),
    [
        (
            {**RUST_BASE, "src/lib.rs": _rust_lib(_rust_test("keeps", 3))},
            {"src/lib.rs": _rust_lib(_rust_test("keeps", 3), _rust_test("more", 5))},
            "src/lib.rs",
        ),
        (
            {**TS_BASE, "src/a.test.ts": _vitest(("it", "keeps", 2))},
            {"src/a.test.ts": _vitest(("it", "keeps", 2), ("it", "more", 5))},
            "src/a.test.ts",
        ),
        (
            {**GO_BASE, "a_test.go": _go_tests(("TestKeeps", 2, False))},
            {"a_test.go": _go_tests(("TestKeeps", 2, False), ("TestMore", 5, False))},
            "a_test.go",
        ),
    ],
    ids=["rust", "ts", "go"],
)
def test_adding_a_strong_test_is_no_finding_and_not_a_pass(
    tmp_path: Path, base: dict[str, str | None], branch: dict[str, str | None], path: str
) -> None:
    root = _repo(tmp_path, base, branch, ADEQUACY)

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "test_adequacy"] == []
    assert _gaps(document, "test_adequacy") == _no_target(path)


def test_an_edit_inside_an_existing_rust_test_module_is_not_a_passing_row(tmp_path: Path) -> None:
    """The diff adds no ``#[cfg(test)]`` line, only a changed assertion."""
    base = {**RUST_BASE, "src/lib.rs": _rust_lib(_rust_test("keeps", 3))}
    root = _repo(tmp_path, base, {"src/lib.rs": _rust_lib(_rust_test("keeps", 4))}, ADEQUACY)

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "test_adequacy"] == []
    assert _gaps(document, "test_adequacy") == _no_target("src/lib.rs")


def test_a_rust_deletion_does_not_block_under_layer0_block(tmp_path: Path) -> None:
    base = {**RUST_BASE, "src/lib.rs": _rust_lib(_rust_test("gone", 3))}
    root = _repo(tmp_path, base, {"src/lib.rs": _rust_lib(_rust_test("more", 4))}, BLOCK)

    row = _row(_check_json(root), "test_adequacy")

    assert row["passed"] is True
    assert row["message"] == (
        "1 test-adequacy finding(s) [blocking]; 1 advisory(ies); not read: src/lib.rs"
    )
    assert _severities(row) == [("src/lib.rs", "adequacy_test_deleted", "advisory")]


def test_a_python_deletion_beside_a_rust_deletion_still_blocks(tmp_path: Path) -> None:
    base = {
        **RUST_BASE,
        "src/lib.rs": _rust_lib(_rust_test("gone", 3)),
        "tests/test_a.py": "def test_a() -> None:\n    assert abs(-2) == 2\n",
    }
    branch = {"src/lib.rs": _rust_lib(_rust_test("more", 4)), "tests/test_a.py": None}
    root = _repo(tmp_path, base, branch, BLOCK)

    row = _row(_check_json(root), "test_adequacy")

    assert row["passed"] is False
    assert row["message"] == (
        "3 test-adequacy finding(s) [blocking]; 1 advisory(ies); not read: src/lib.rs"
    )
    assert _severities(row) == [
        ("src/lib.rs", "adequacy_test_deleted", "advisory"),
        ("tests/test_a.py", "adequacy_assertion_removed", "high"),
        ("tests/test_a.py", "adequacy_test_deleted", "high"),
    ]


def test_a_production_debug_assert_removed_is_not_an_assertion_finding(tmp_path: Path) -> None:
    """The table does not count assertions in ``.rs`` files outside ``tests/``."""
    base = {
        **RUST_BASE,
        "src/lib.rs": "pub fn a(x: i32) -> i32 {\n    debug_assert!(x >= 0);\n    x\n}\n",
    }
    root = _repo(tmp_path, base, {"src/lib.rs": "pub fn a(x: i32) -> i32 {\n    x\n}\n"}, ADEQUACY)

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "test_adequacy"] == []
    assert _gaps(document, "test_adequacy") == _no_target("src/lib.rs")


def test_a_rust_file_under_tests_keeps_its_blocking_assertion_finding(tmp_path: Path) -> None:
    """A ``.rs`` file under ``tests/`` is still read by the Python branch, as
    before #631: its lost ``assert!`` is found and blocks under ``block``."""
    base = {
        **RUST_BASE,
        "tests/it.rs": (
            "#[test]\nfn gone() {\n    assert!(p::a() == 1);\n}\n\n"
            "#[test]\nfn keeps() {\n    assert_eq!(p::a(), 1);\n}\n"
        ),
    }
    branch = {"tests/it.rs": "#[test]\nfn keeps() {\n    assert_eq!(p::a(), 1);\n}\n"}
    root = _repo(tmp_path, base, branch, BLOCK)

    row = _row(_check_json(root), "test_adequacy")

    assert row["passed"] is False
    assert row["message"] == "1 test-adequacy finding(s) [blocking]; not read: tests/it.rs"
    assert row["details"] == [f"tests/it.rs: {LOST}"]
    assert _severities(row) == [("tests/it.rs", "adequacy_assertion_removed", "high")]


def test_a_test_attribute_ending_one_hunk_does_not_name_a_function_in_the_next(
    tmp_path: Path,
) -> None:
    """The ``#[test]`` git shows as the last context line of one hunk applies
    to a line the diff does not show, so the non-test ``fn helper`` removed
    in the next hunk is not a deleted test."""
    head = "pub fn a() -> i32 {\n    1\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n"
    pad = "".join(f"    // pad {i}\n" for i in range(5))
    tail = "    #[test]\n    fn keeps() {\n        assert_eq!(a(), 1);\n    }\n" + pad
    helper = "    fn helper() -> i32 {\n        2\n    }\n"

    def lib(first: str, extra: str) -> str:
        return head + _rust_test("first", 1).replace("a(1), 1", first) + "\n" + tail + extra + "}\n"

    base = {**RUST_BASE, "src/lib.rs": lib("a(1), 1", helper)}
    root = _repo(tmp_path, base, {"src/lib.rs": lib("a(2), 2", "")}, ADEQUACY)

    document = _check_json(root)

    assert [c for c in document["checks"] if c["name"] == "test_adequacy"] == []
    assert _gaps(document, "test_adequacy") == _no_target("src/lib.rs")


def test_a_rust_deletion_with_no_assertion_still_names_its_file_as_not_read(
    tmp_path: Path,
) -> None:
    """No removed or added line holds an assertion, an attribute or an
    ``#[ignore]``, and the file the finding is in is still named."""
    empty = "    #[test]\n    fn {}() {{}}\n"
    base = {**RUST_BASE, "src/lib.rs": _rust_lib(empty.format("gone"))}
    root = _repo(tmp_path, base, {"src/lib.rs": _rust_lib(empty.format("more"))}, ADEQUACY)

    row = _row(_check_json(root), "test_adequacy")

    assert row["message"] == "1 test-adequacy finding(s) [advisory]; not read: src/lib.rs"
    assert row["details"] == [f"src/lib.rs::gone: {DELETED}"]


def test_a_non_test_fn_removed_below_a_rust_test_is_not_a_deleted_test(tmp_path: Path) -> None:
    """``#[test]`` names only the ``fn`` right below it: a helper removed a
    few lines further down the same hunk is not a deleted test."""
    one_line = "    #[test]\n    fn keeps() { assert_eq!(a(3), 3); }\n"
    helper = "    fn helper() -> i32 {\n        2\n    }\n"
    base = {**RUST_BASE, "src/lib.rs": _rust_lib(one_line + helper)}
    root = _repo(tmp_path, base, {"src/lib.rs": _rust_lib(one_line)}, ADEQUACY)

    document = _check_json(root)

    findings = [
        f for c in document["checks"] if c["name"] == "test_adequacy" for f in c["findings"]
    ]
    assert findings == []
