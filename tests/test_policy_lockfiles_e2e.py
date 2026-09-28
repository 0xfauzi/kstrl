"""``ks check`` reads new dependencies from every lockfile it has a reader for (#630).

Each test builds a real git repository whose ``main`` holds one lockfile
and whose ``feature`` branch changes it, drives the real command through
``CliRunner`` and reads the ``policy_envelope`` row of the ``--json``
document back. The lockfiles are captured tool output, not hand-written
(``tests/fixtures/lockfiles/README.md``).

No test reaches a network: the ``registry`` fixture points the uv cache at
an empty directory and replaces the PyPI getter with a recorder, so a test
can also assert that no registry was asked.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from kstrl import licensing
from kstrl.lockfiles import LOCKFILE_READERS
from kstrl.policy import LOCKFILE_MANIFESTS
from tests.spine_utils import git
from tests.test_check_non_python_diff import POLICY, _check_json, _repo, _row, _write

FIXTURES = Path(__file__).parent / "fixtures" / "lockfiles"

#: The lockfile each captured format is written as.
LOCKFILE = {
    "cargo": "Cargo.lock",
    "go": "go.sum",
    "npm": "package-lock.json",
    "npm1": "package-lock.json",
    "npm2": "package-lock.json",
    "poetry": "poetry.lock",
    "yarn": "yarn.lock",
}

#: The license lookups the network is on for: the policy with the default
#: ``license_use_network = true``.
NETWORK_ON = "[policy]\nenabled = true\n"

UV_TWO = (
    'version = 1\n\n[[package]]\nname = "six"\nversion = "1.17.0"\n\n'
    '[[package]]\nname = "attrs"\nversion = "25.1.0"\n'
)


def _lock(fmt: str, state: str) -> str:
    return (FIXTURES / fmt / state / LOCKFILE[fmt]).read_text(encoding="utf-8")


def _new(path: str, names: str) -> str:
    return f"New dependencies added to {path} while deps_allow_new=false: {names}"


def _no_source(name: str, version: str, path: str, ecosystem: str) -> str:
    return (
        f"license could not be resolved for {name} {version} in {path} "
        f"(kstrl has no license source for {ecosystem} packages; nothing was consulted)"
    )


def _pypi_offline(name: str, version: str) -> str:
    return (
        f"license could not be resolved for {name} {version} "
        "(uv cache missed; network resolution disabled)"
    )


def _unread(path: str, reason: str) -> list[str]:
    return [
        f"new dependencies in {path} were not measured: {reason}, so {rule} could not be checked"
        for rule in ("deps_allow_new", "license_unresolved")
    ]


def _policy_row(root: Path) -> dict[str, Any]:
    return _row(_check_json(root), "policy_envelope")


def _changed(tmp_path: Path, fmt: str, base: str, head: str, config: str = POLICY) -> Path:
    """A repo whose branch moves ``fmt``'s lockfile from state ``base`` to ``head``."""
    lockfile = LOCKFILE[fmt]
    return _repo(tmp_path, {lockfile: _lock(fmt, base)}, {lockfile: _lock(fmt, head)}, config)


@pytest.fixture(autouse=True)
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every PyPI URL a check asked for; the uv cache is empty."""
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    calls: list[str] = []

    def recorder(url: str, timeout: float) -> bytes:
        calls.append(url)
        return b'{"info": {"license_expression": "MIT"}}'

    monkeypatch.setattr(licensing, "_default_http_get", recorder)
    return calls


# --- a new package is named, per format -------------------------------------


def test_a_new_cargo_dependency_violates_deps_allow_new_false(tmp_path: Path) -> None:
    row = _policy_row(_changed(tmp_path, "cargo", "base", "add"))

    assert row["passed"] is False
    assert row["details"] == [
        _new("Cargo.lock", "ryu"),
        _no_source("ryu", "1.0.17", "Cargo.lock", "cargo"),
    ]


def test_a_new_cargo_lock_names_its_crates_not_the_root_crate(tmp_path: Path) -> None:
    """``cargo_base`` is the root crate: it has no ``source`` and is skipped."""
    root = _repo(
        tmp_path, {"Cargo.toml": "[package]\n"}, {"Cargo.lock": _lock("cargo", "base")}, POLICY
    )

    row = _policy_row(root)

    assert row["details"] == [
        _new("Cargo.lock", "itoa"),
        _no_source("itoa", "1.0.10", "Cargo.lock", "cargo"),
    ]


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        pytest.param(
            "npm",
            [
                _new("package-lock.json", "left-pad"),
                _no_source("left-pad", "1.3.0", "package-lock.json", "npm"),
            ],
            id="package-lock-v3",
        ),
        pytest.param(
            "npm2",
            [
                _new("package-lock.json", "left-pad"),
                _no_source("left-pad", "1.3.0", "package-lock.json", "npm"),
            ],
            id="package-lock-v2",
        ),
        pytest.param(
            "yarn",
            [
                _new("yarn.lock", "@sindresorhus/is, left-pad"),
                _no_source("@sindresorhus/is", "4.6.0", "yarn.lock", "npm"),
                _no_source("left-pad", "1.3.0", "yarn.lock", "npm"),
            ],
            id="yarn-v1",
        ),
    ],
)
def test_a_new_npm_dependency_violates_deps_allow_new_false(
    tmp_path: Path, fmt: str, expected: list[str]
) -> None:
    row = _policy_row(_changed(tmp_path, fmt, "base", "add"))

    assert row["passed"] is False
    assert row["details"] == expected


def test_a_new_go_dependency_violates_deps_allow_new_false(tmp_path: Path) -> None:
    row = _policy_row(_changed(tmp_path, "go", "base", "add"))

    assert row["passed"] is False
    assert row["details"] == [
        _new("go.sum", "github.com/google/uuid"),
        _no_source("github.com/google/uuid", "v1.6.0", "go.sum", "go"),
    ]


def test_a_new_poetry_dependency_violates_deps_allow_new_false(tmp_path: Path) -> None:
    row = _policy_row(_changed(tmp_path, "poetry", "base", "add"))

    assert row["passed"] is False
    assert row["details"] == [_new("poetry.lock", "idna"), _pypi_offline("idna", "3.6")]


# --- what is not a new package ----------------------------------------------


@pytest.mark.parametrize("fmt", ["cargo", "npm", "yarn", "go", "poetry"])
def test_a_version_bump_is_not_a_new_dependency(tmp_path: Path, fmt: str) -> None:
    row = _policy_row(_changed(tmp_path, fmt, "base", "bump"))

    assert row["passed"] is True
    assert row["message"] == "policy envelope satisfied (0 files, 0 lines, within limits)"
    assert row["findings"] == []


def test_a_reformatted_package_lock_is_not_a_new_dependency(tmp_path: Path) -> None:
    base = _lock("npm", "base")
    reformatted = json.dumps(json.loads(base), indent=4) + "\n"
    root = _repo(tmp_path, {"package-lock.json": base}, {"package-lock.json": reformatted}, POLICY)

    row = _policy_row(root)

    assert row["passed"] is True
    assert row["findings"] == []


# --- the lockfile is read from its blobs --------------------------------------


def test_a_lockfile_hidden_by_a_diff_attribute_is_read_from_its_blob(tmp_path: Path) -> None:
    """``-diff`` makes ``git diff`` print "Binary files differ": no added line."""
    base = {
        "package-lock.json": _lock("npm", "base"),
        ".gitattributes": "package-lock.json -diff\n",
    }
    root = _repo(tmp_path, base, {"package-lock.json": _lock("npm", "add")}, POLICY)

    row = _policy_row(root)

    assert row["passed"] is False
    assert _new("package-lock.json", "left-pad") in row["details"]


def test_the_base_document_is_the_merge_base_the_diff_measures(tmp_path: Path) -> None:
    """``main`` gains ryu after the branch forked. The branch still adds it
    against the merge base, which is what ``main...HEAD`` measures."""
    root = _changed(tmp_path, "cargo", "base", "add")
    git("checkout", "-q", "main", cwd=root)
    _write(root, {"Cargo.lock": _lock("cargo", "add")})
    git("commit", "-q", "-am", "main adds ryu too", cwd=root)
    git("checkout", "-q", "feature", cwd=root)

    row = _policy_row(root)

    assert _new("Cargo.lock", "ryu") in row["details"]


@pytest.mark.parametrize(
    ("condition", "failed"),
    [
        pytest.param('[ "$1" = "cat-file" ]', "git cat-file Cargo.lock at {rev}", id="cat-file"),
        pytest.param(
            '[ "$1" = "ls-tree" ] && [ "$2" = "--full-tree" ]',
            "git ls-tree {rev} -- Cargo.lock",
            id="ls-tree",
        ),
    ],
)
def test_a_lockfile_git_cannot_read_is_unread_not_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str, failed: str
) -> None:
    """A ``git`` whose ``cat-file`` or ``ls-tree`` fails: the lockfile is
    unread with git's reason, never read as absent (which would make nothing
    new)."""
    root = _changed(tmp_path, "cargo", "base", "add")
    merge_base = git("merge-base", "main", "HEAD", cwd=root)
    real_git = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(
        f'#!/bin/sh\nif {condition}; then echo "fatal: simulated" >&2; exit 128; fi\n'
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    row = _policy_row(root)

    assert row["passed"] is False
    assert row["details"] == _unread(
        "Cargo.lock",
        f"git could not read it: {failed.format(rev=merge_base)} exited 128: fatal: simulated",
    )


def test_the_head_document_is_the_commit_the_diff_measures(tmp_path: Path) -> None:
    """The branch commits a bump. An uncommitted edit adding ryu is not in
    ``main...HEAD``, so it is not what the lockfile rules read either."""
    root = _changed(tmp_path, "cargo", "base", "bump")
    _write(root, {"Cargo.lock": _lock("cargo", "add")})

    row = _policy_row(root)

    assert row["passed"] is True
    assert row["findings"] == []


@pytest.mark.parametrize(
    ("fmt", "base", "head", "reason"),
    [
        pytest.param(
            "npm1",
            None,
            None,
            "the HEAD copy could not be read (LockfileShapeError: lockfileVersion 1 is not "
            "read; kstrl reads 2 and 3)",
            id="package-lock-v1",
        ),
        pytest.param(
            "go",
            "base",
            "golang.org/x/text v0.14.0\n",
            "the HEAD copy could not be read (LockfileShapeError: line 1 is not "
            "'module version h1:hash')",
            id="go-sum-two-fields",
        ),
        pytest.param(
            "npm",
            "base",
            '{\n  "lockfileVersion": 3,\n  "packages": {\n',
            "the HEAD copy could not be read (JSONDecodeError: {parser})",
            id="truncated-json",
        ),
        pytest.param(
            "yarn",
            "base",
            "# yarn lockfile v1\n\nleft-pad@1.3.0:\n  version \n",
            "the HEAD copy could not be read (IndexError: list index out of range)",
            id="yarn-version-with-no-value",
        ),
    ],
)
def test_an_unreadable_lockfile_names_its_reason(
    tmp_path: Path, fmt: str, base: str | None, head: str | None, reason: str
) -> None:
    lockfile = LOCKFILE[fmt]
    head_text = _lock(fmt, "add") if head is None else head
    base_files = {} if base is None else {lockfile: _lock(fmt, base)}
    root = _repo(tmp_path, base_files, {lockfile: head_text}, POLICY)
    if "{parser}" in reason:
        with pytest.raises(json.JSONDecodeError) as parsed:
            json.loads(head_text)
        reason = reason.format(parser=parsed.value)

    row = _policy_row(root)

    assert row["passed"] is False
    assert row["details"] == _unread(lockfile, reason)


#: What a branch adds under each lockfile name: a captured lockfile where
#: kstrl has a reader, text it cannot read where it has none.
_ADDED = {
    "uv.lock": UV_TWO,
    "poetry.lock": _lock("poetry", "add"),
    "package-lock.json": _lock("npm", "add"),
    "yarn.lock": _lock("yarn", "add"),
    "Cargo.lock": _lock("cargo", "add"),
    "go.sum": _lock("go", "add"),
}


@pytest.mark.parametrize("lockfile", sorted(LOCKFILE_MANIFESTS))
def test_every_lockfile_basename_is_measured_or_names_why_not(
    tmp_path: Path, lockfile: str
) -> None:
    text = _ADDED[lockfile] if lockfile in _ADDED else "not a lockfile kstrl reads\n"
    root = _repo(tmp_path, {"README.md": "p\n"}, {lockfile: text}, POLICY)

    row = _policy_row(root)

    assert row["passed"] is False
    assert "satisfied" not in str(row["message"])
    locations = [f["location"] for f in row["findings"] if f["category"] == "policy_deps_allow_new"]
    assert locations == [lockfile]
    if lockfile != "uv.lock" and LOCKFILE_READERS[lockfile] is None:
        assert row["details"] == _unread(lockfile, f"kstrl has no reader for {lockfile}")


def test_each_lockfile_is_its_own_finding(tmp_path: Path) -> None:
    """One ``deps_allow_new`` finding per lockfile path, so an approval of one
    (its waiver key carries the location) never covers another."""
    base = {"Cargo.lock": _lock("cargo", "base"), "web/package-lock.json": _lock("npm", "base")}
    branch = {"Cargo.lock": _lock("cargo", "add"), "web/package-lock.json": _lock("npm", "add")}
    root = _repo(tmp_path, base, branch, POLICY)

    row = _policy_row(root)

    assert [
        (f["location"], f["explanation"])
        for f in row["findings"]
        if f["category"] == "policy_deps_allow_new"
    ] == [
        ("Cargo.lock", _new("Cargo.lock", "ryu")),
        ("web/package-lock.json", _new("web/package-lock.json", "left-pad")),
    ]


def test_editing_the_lockfile_reader_halts(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, {"kstrl/lockfiles.py": "X = 1\n"}, {"kstrl/lockfiles.py": "X = 2\n"}, POLICY
    )

    row = _policy_row(root)

    assert row["passed"] is False
    assert row["message"] == "1 policy violation(s) including enforcement-machinery halt"
    assert [(f["category"], f["location"]) for f in row["findings"]] == [
        ("policy_enforcement_machinery", "kstrl/lockfiles.py")
    ]


# --- licenses: each ecosystem's own source, and no network when it is off --------


def test_no_registry_call_when_license_use_network_is_false(
    tmp_path: Path, registry: list[str]
) -> None:
    row = _policy_row(_changed(tmp_path, "poetry", "base", "add"))

    assert registry == []
    assert row["details"] == [_new("poetry.lock", "idna"), _pypi_offline("idna", "3.6")]


def test_a_poetry_dependency_is_resolved_from_pypi_when_the_network_is_on(
    tmp_path: Path, registry: list[str]
) -> None:
    """The control for the test above: the recorder is the getter kstrl calls."""
    row = _policy_row(_changed(tmp_path, "poetry", "base", "add", NETWORK_ON))

    assert registry == ["https://pypi.org/pypi/idna/3.6/json"]
    assert row["details"] == [_new("poetry.lock", "idna")]


def test_a_new_crate_reaches_the_license_gate_when_new_dependencies_are_allowed(
    tmp_path: Path,
) -> None:
    """``deps_allow_new = true`` (L3+) lifts the new-package rule only: the
    crate is still read and its license is still judged."""
    config = POLICY + "deps_allow_new = true\n"
    row = _policy_row(_changed(tmp_path, "cargo", "base", "add", config))

    assert row["passed"] is False
    assert row["details"] == [_no_source("ryu", "1.0.17", "Cargo.lock", "cargo")]


def test_a_cargo_name_is_never_resolved_from_pypi(tmp_path: Path, registry: list[str]) -> None:
    row = _policy_row(_changed(tmp_path, "cargo", "base", "add", NETWORK_ON))

    assert registry == []
    assert _no_source("ryu", "1.0.17", "Cargo.lock", "cargo") in row["details"]


# --- uv.lock is read exactly as before (the M7 golden) ------------------------

_DEPS_UV = (
    "policy_deps_allow_new",
    "uv.lock",
    "high",
    "New dependencies added while deps_allow_new=false: attrs, six",
    "Drop the dependency, or set [policy] deps_allow_new = true.",
)
_WARM = (
    "Warm the uv cache (`uv sync`) or allow network resolution; set [policy] "
    'license_unresolved = "advisory" to accept unprovable licenses.'
)


@pytest.mark.parametrize(
    ("lock_path", "config", "cached", "expected", "calls"),
    [
        pytest.param(
            "uv.lock",
            POLICY,
            {},
            [
                _DEPS_UV,
                (
                    "policy_license_unresolved",
                    "six 1.17.0",
                    "high",
                    _pypi_offline("six", "1.17.0"),
                    _WARM,
                ),
                (
                    "policy_license_unresolved",
                    "attrs 25.1.0",
                    "high",
                    _pypi_offline("attrs", "25.1.0"),
                    _WARM,
                ),
            ],
            [],
            id="offline-cache-miss",
        ),
        pytest.param(
            "py/uv.lock",
            POLICY + 'license_unresolved = "advisory"\n',
            {},
            [
                _DEPS_UV,
                (
                    "policy_license_unresolved",
                    "six 1.17.0",
                    "advisory",
                    _pypi_offline("six", "1.17.0") + "; recorded as advisory",
                    _WARM,
                ),
                (
                    "policy_license_unresolved",
                    "attrs 25.1.0",
                    "advisory",
                    _pypi_offline("attrs", "25.1.0") + "; recorded as advisory",
                    _WARM,
                ),
            ],
            [],
            id="nested-advisory",
        ),
        pytest.param(
            "uv.lock",
            POLICY,
            {"six-1.17.0": "MIT", "attrs-25.1.0": "GPL-3.0-only"},
            [
                _DEPS_UV,
                (
                    "policy_license_denied",
                    "attrs 25.1.0",
                    "high",
                    "denied license 'GPL-3.0-only' for dependency attrs 25.1.0",
                    "Drop the dependency or find a permissive alternative.",
                ),
            ],
            [],
            id="cache-denied",
        ),
        pytest.param(
            "uv.lock",
            POLICY,
            {"six-1.17.0": "MIT", "attrs-25.1.0": "MPL-2.0"},
            [
                _DEPS_UV,
                (
                    "policy_license_not_allowed",
                    "attrs 25.1.0",
                    "high",
                    "license 'MPL-2.0' for attrs 25.1.0 is not in license_allow",
                    "Add 'MPL-2.0' to [policy] license_allow if it is acceptable for this repo.",
                ),
            ],
            [],
            id="cache-not-allowed",
        ),
        pytest.param(
            "uv.lock",
            NETWORK_ON,
            {},
            [_DEPS_UV],
            ["https://pypi.org/pypi/six/1.17.0/json", "https://pypi.org/pypi/attrs/25.1.0/json"],
            id="network-on",
        ),
    ],
)
def test_uv_lock_behaviour_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: list[str],
    lock_path: str,
    config: str,
    cached: dict[str, str],
    expected: list[tuple[str, str, str, str, str]],
    calls: list[str],
) -> None:
    """Every field of every uv.lock finding, byte for byte as at the base of #630."""
    cache = tmp_path / "uv-metadata"
    for dist, spdx in cached.items():
        (cache / f"{dist}.dist-info").mkdir(parents=True)
        (cache / f"{dist}.dist-info" / "METADATA").write_text(
            f"Metadata-Version: 2.4\nLicense-Expression: {spdx}\n\n", encoding="utf-8"
        )
    cache.mkdir(exist_ok=True)
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    root = _repo(tmp_path, {"pyproject.toml": "[project]\n"}, {lock_path: UV_TWO}, config)

    row = _policy_row(root)

    assert [
        (f["category"], f["location"], f["severity"], f["explanation"], f["suggestion"])
        for f in row["findings"]
    ] == expected
    assert registry == calls
