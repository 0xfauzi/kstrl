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

#: ``license_use_network = false`` so the uv control never reaches PyPI.
POLICY = "[policy]\nenabled = true\nlicense_use_network = false\n"
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

    ``config`` is appended right after the ``[verify]`` keys, so a bare
    ``key = value`` line lands in ``[verify]`` and a ``[section]`` header
    starts its own table.
    """
    root = tmp_path / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    _write(root, base)
    (root / "kstrl.toml").write_text(
        "[verify]\n"
        f"test_command = {json.dumps(_OK_COMMAND)}\n"
        f"typecheck_command = {json.dumps(_OK_COMMAND)}\n"
        f"lint_command = {json.dumps(_OK_COMMAND)}\n" + config,
        encoding="utf-8",
    )
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    git("checkout", "-q", "-b", "feature", cwd=root)
    _write(root, branch)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "change", cwd=root)
    return root


def _check_json(root: Path) -> dict[str, Any]:
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json"])
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
        "dead_code_cleanup = true\n",
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


# --- policy_envelope: a lockfile kstrl cannot parse is not "no new deps" ---


def _unread(lockfile: str) -> list[str]:
    """The two rules the default ``[policy]`` could not check."""
    return [
        f"new dependencies in {lockfile} were not measured: kstrl reads uv.lock "
        f"only, so {rule} could not be checked"
        for rule in ("deps_allow_new", "license_unresolved")
    ]


def test_a_new_npm_dependency_is_not_reported_as_satisfied_under_deps_allow_new_false(
    tmp_path: Path,
) -> None:
    branch = {
        "package.json": (
            '{"name": "p", "version": "0.1.0", "dependencies": {"left-pad": "1.3.0"}}\n'
        ),
        "package-lock.json": (
            '{\n  "packages": {\n    "node_modules/left-pad": {"version": "1.3.0"}\n  }\n}\n'
        ),
    }
    root = _repo(tmp_path, TS_BASE, branch, POLICY)

    row = _row(_check_json(root), "policy_envelope")

    assert row["passed"] is False
    assert "satisfied" not in row["message"]
    assert row["details"] == _unread("package-lock.json")


def test_a_new_cargo_dependency_is_not_reported_as_satisfied_under_deps_allow_new_false(
    tmp_path: Path,
) -> None:
    branch = {"Cargo.lock": '[[package]]\nname = "serde"\nversion = "1.0.0"\n'}
    root = _repo(tmp_path, RUST_BASE, branch, POLICY)

    row = _row(_check_json(root), "policy_envelope")

    assert row["passed"] is False
    assert "satisfied" not in row["message"]
    assert row["details"] == _unread("Cargo.lock")


def test_a_new_uv_dependency_still_violates_deps_allow_new_false(tmp_path: Path) -> None:
    """The control: ``uv.lock`` is still parsed and names the package."""
    branch = {"uv.lock": 'version = 1\n\n[[package]]\nname = "six"\nversion = "1.17.0"\n'}
    root = _repo(tmp_path, PY_BASE, branch, POLICY)

    row = _row(_check_json(root), "policy_envelope")

    assert row["passed"] is False
    assert "New dependencies added while deps_allow_new=false: six" in row["details"]
    assert not any("were not measured" in d for d in row["details"])


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


def test_an_unread_lockfile_is_advisory_when_license_unresolved_is_advisory(
    tmp_path: Path,
) -> None:
    branch = {"Cargo.lock": '[[package]]\nname = "serde"\nversion = "1.0.0"\n'}
    config = POLICY + 'license_unresolved = "advisory"\n'
    root = _repo(tmp_path, RUST_BASE, branch, config)

    row = _row(_check_json(root), "policy_envelope")

    assert row["passed"] is True
    assert row["message"].endswith("; 2 advisory(ies)")
    assert row["details"] == [d + "; recorded as advisory" for d in _unread("Cargo.lock")]


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
        "dead_code_cleanup = true\n",
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
