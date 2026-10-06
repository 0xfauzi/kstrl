"""``ks check`` on a diff with no Python in it (#619).

Phase 1 checks chose their input by Python file name or by ``uv.lock``
and reported a pass when that choice left nothing: a secret added in a
``.rs`` or ``.ts`` file and a new npm or cargo dependency passed having
been read by nobody. #696 slice 8 removed the Python-only rules of
``bad_patterns`` and the test-adequacy and dead-code checks outright, so
what is left here is the secret rule and the policy envelope. Each test
builds a real git repository, drives the real command through
``CliRunner`` and reads the ``--json`` document back. The verify commands
are no-op Python one-liners, so no Rust or Node toolchain is needed.
"""

from __future__ import annotations

import json
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


def test_bad_patterns_on_a_non_python_diff_names_no_language(tmp_path: Path) -> None:
    """The message says what the one rule read, and names no language."""
    root = _repo(tmp_path, RUST_BASE, {"src/lib.rs": "pub fn a() -> i32 {\n    2\n}\n"})

    row = _row(_check_json(root), "bad_patterns")

    assert row["passed"] is True
    assert row["message"] == "secrets: scanned the lines added to 1 changed files, no issues"


#: A file emptied, and a file that does not parse in its own language. #696
#: slice 8 removed the empty-file and syntax-error rules, which read Python
#: only, so neither is a ``bad_patterns`` finding in any language: the
#: project's own [stack] checks are what parse a file.
EMPTIED_OR_BROKEN = {
    "python-emptied": (PY_BASE, {"src/a.py": ""}),
    "rust-emptied": (RUST_BASE, {"src/lib.rs": ""}),
    "python-broken": (PY_BASE, {"src/a.py": "def f(:\n    pass\n"}),
    "rust-broken": (RUST_BASE, {"src/lib.rs": "pub fn a( -> i32 {\n"}),
}


@pytest.mark.parametrize("case", sorted(EMPTIED_OR_BROKEN))
def test_an_emptied_or_broken_file_is_no_bad_patterns_finding_in_any_language(
    tmp_path: Path, case: str
) -> None:
    base, branch = EMPTIED_OR_BROKEN[case]
    root = _repo(tmp_path, base, branch)

    row = _row(_check_json(root), "bad_patterns")

    assert (row["passed"], row["details"]) == (True, [])
    assert "Python" not in row["message"]


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


def test_a_lockfile_is_still_held_to_paths_deny_and_the_secret_patterns(tmp_path: Path) -> None:
    """Slice 9 removed only the dependency and license rules. A lockfile is
    still a changed file for ``paths_deny`` and the secret patterns, so a
    rule that skips lockfiles there loosens the envelope."""
    lock = CARGO_LOCK + 'checksum = "' + "AKIA" + "Q" * 16 + '"\n'
    config = POLICY + 'paths_deny = ["Cargo.lock"]\n'
    root = _repo(tmp_path, RUST_BASE, {"Cargo.lock": lock}, config)

    row = _row(_check_json(root), "policy_envelope")

    assert row["passed"] is False, row
    assert "Denied paths modified: Cargo.lock (deny 'Cargo.lock')" in row["details"], row
    assert any(d.startswith("Possible secrets in added lines: Cargo.lock#") for d in row["details"])
