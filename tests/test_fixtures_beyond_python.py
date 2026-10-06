"""Fixtures on a tree that is not Python (#632, slices 1 and 3).

End to end: the real ``ks check --prd P --json`` in its own process,
against a committed git repository, with fixtures enabled through the
environment. No LLM is called and no toolchain beyond ``sh`` is needed.

Two things were wrong. A ``function`` fixture imports a Python module with
kstrl's own interpreter, so on a tree with no pyproject.toml or setup.py it
failed every attempt with ``ModuleNotFoundError`` and nothing refused it
first. And a ``cli`` fixture could not be given input: its child inherited
kstrl's own stdin, so the same PRD passed under ``/dev/null`` and timed out
under an open pipe. Every run here gives ``ks check`` a pipe whose write end
stays open for the whole run, which is the case that used to time out.

Slice 3: a ``cli`` fixture could only look for substrings of stdout. It now
compares stdout as JSON (``stdout_json``) and looks at stderr
(``stderr_contains``), and every accepted key comes from one table that also
judges it, so a misspelled key or an empty list is refused. The snapshot test
drives two real ``ks factory`` runs with a shell engineer, because only the
factory passes a component id and so only it compares against a snapshot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import gitrepo
from tests.helpers.executables import write_executable
from tests.helpers.procs import kill_group
from tests.helpers.stack_confirmation import confirm_stack, write_stack

#: The whole run takes about a second; a hang fails loudly instead.
FUSE_SECONDS = 120.0

#: Short, so the case that used to hang on the inherited pipe costs 3s, not 30s.
FIXTURE_TIMEOUT_SECONDS = "3"

REFUSAL = "PRD names fixtures this tree cannot run (failing closed)"

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"

#: A program in no particular language: it sums the integers on stdin.
SUM = '#!/bin/sh\nn=0\nwhile read x; do n=$((n + x)); done\necho "sum=$n"\n'

CARGO_TREE = {"Cargo.toml": '[package]\nname = "pricing"\nversion = "0.1.0"\n'}
#: Reads its stdin to EOF: the function runner must hand it an empty one.
PRICING = (
    "import sys\n\n\ndef total(quantity):\n    return quantity * 300 + len(sys.stdin.read())\n"
)
PYTHON_TREE = {
    "pyproject.toml": '[project]\nname = "pricing"\nversion = "0.1.0"\n',
    "pricing.py": PRICING,
}
#: #621's predicate also accepts a setup.py alone; a check for pyproject.toml
#: only would refuse this tree.
SETUP_PY_TREE = {"setup.py": "from setuptools import setup\n\nsetup()\n", "pricing.py": PRICING}

FILE_FIXTURE = {
    "description": "the program is there",
    "fixture_type": "file",
    "input_data": {"path": "sum"},
    "expected": {"exists": True},
}

SCHEMA_REFUSAL = (
    "PRD failed schema validation; fixture definitions cannot be trusted (failing closed)"
)

ALLOWED_CLI_KEYS = "exit_code, stderr_contains, stdout_contains, stdout_json, stdout_not_contains"

FUNCTION_FIXTURE = {
    "description": "total(3) is 900",
    "fixture_type": "function",
    "input_data": {"module": "pricing", "function": "total", "args": [3]},
    "expected": {"returns": 900},
}


def _cli_fixture(stdin: object, expected: str) -> dict[str, Any]:
    """``./sum`` with ``stdin`` as its input; ``None`` leaves the key out."""
    input_data: dict[str, Any] = {"command": "./sum"}
    if stdin is not None:
        input_data["stdin"] = stdin
    return {
        "description": "sum of stdin",
        "fixture_type": "cli",
        "input_data": input_data,
        "expected": {"stdout_contains": [expected]},
    }


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A committed repository holding ``files``, ``./sum`` and a no-op kstrl.toml."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    write_executable(root / "sum", SUM)
    gitrepo.git_in(root, "add", "-A")
    write_stack(root)
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    confirm_stack(root)
    return root


def _fixtures_row(root: Path, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Run ``ks check --prd`` with stdin on an open pipe; return its fixtures row."""
    prd = root.parent / "prd.json"
    story = {
        "id": "US-1",
        "title": "t",
        "acceptanceCriteria": ["c"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    prd.write_text(
        json.dumps({"branchName": "main", "userStories": [story], "fixtures": fixtures}),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("KSTRL_")}
    env["KSTRL_FIXTURES_ENABLED"] = "1"
    env["KSTRL_FIXTURES_TIMEOUT"] = FIXTURE_TIMEOUT_SECONDS
    read_end, write_end = os.pipe()
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "kstrl", "check", "--root", str(root)]
            + ["--base", "main", "--prd", str(prd), "--json"],
            # Outside the repository, so a check that asks the process's own
            # directory instead of the tree it was given cannot pass by accident.
            cwd=root.parent,
            env=env,
            stdin=read_end,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            out, err = child.communicate(timeout=FUSE_SECONDS)
        except subprocess.TimeoutExpired:
            kill_group(child.pid)
            child.communicate()
            pytest.fail(f"`ks check` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    finally:
        os.close(read_end)
        os.close(write_end)
    assert child.returncode in (0, 1), err
    # A kstrl that cannot import prints a traceback, not JSON, and exits 1.
    assert out.startswith("{"), err
    rows = [c for c in json.loads(out)["checks"] if c["name"] == "fixtures"]
    assert len(rows) == 1, out
    row: dict[str, Any] = rows[0]
    return row


def test_a_function_fixture_on_a_tree_with_no_python_is_refused_at_validation(
    tmp_path: Path,
) -> None:
    """Refused by index before anything runs; the same PRD runs on both Python trees.

    The ``function`` fixture is second, so the line must carry ITS index. On
    the Python tree the function reads its stdin to EOF, so it passes only if
    the runner gave it an empty stdin rather than ``ks check``'s open pipe.
    """
    fixtures = [FILE_FIXTURE, FUNCTION_FIXTURE]

    refused = _fixtures_row(_repo(tmp_path / "cargo", CARGO_TREE), fixtures)

    assert refused["passed"] is False
    assert refused["message"] == REFUSAL
    assert len(refused["details"]) == 1, refused["details"]
    assert refused["details"][0].startswith("fixtures[1]: "), refused["details"]
    assert "a cli fixture runs any program" in refused["details"][0]
    assert not any("ModuleNotFoundError" in d for d in refused["details"])

    for name, tree in (("pyproject", PYTHON_TREE), ("setup-py", SETUP_PY_TREE)):
        ran = _fixtures_row(_repo(tmp_path / name, tree), fixtures)

        assert ran["passed"] is True, (name, ran)
        assert ran["message"] == "2/2 fixtures passed", (name, ran)


@pytest.mark.parametrize(
    ("stdin", "passed", "detail"),
    [
        ("1\n2\n3\n", True, "[PASS] sum of stdin: CLI fixture passed"),
        ("1\n2\n", False, "(actual: 'sum=3\\n')"),
        (None, False, "(actual: 'sum=0\\n')"),
        (3, False, "fixtures[0].input_data.stdin: must be a string"),
    ],
    ids=["all-input", "short-input", "no-stdin-key-reads-eof", "stdin-not-a-string"],
)
def test_a_cli_fixture_with_stdin_passes_and_fails_on_the_right_output(
    tmp_path: Path, stdin: object, passed: bool, detail: str
) -> None:
    row = _fixtures_row(_repo(tmp_path, CARGO_TREE), [_cli_fixture(stdin, "sum=6")])

    assert row["passed"] is passed, row
    assert len(row["details"]) == 1, row["details"]
    assert detail in row["details"][0], row["details"]
    assert "timed out" not in row["details"][0]


def test_a_cli_fixture_stdin_reaches_the_program_as_utf8(tmp_path: Path) -> None:
    """A non-ASCII ``stdin`` reaches the program byte for byte, whatever the locale."""
    text = "café → naïve"
    fixture = {
        "description": "echo stdin",
        "fixture_type": "cli",
        "input_data": {"command": "cat", "stdin": text + "\n"},
        "expected": {"stdout_contains": [text]},
    }

    row = _fixtures_row(_repo(tmp_path, CARGO_TREE), [fixture])

    assert row["passed"] is True, row
    assert row["details"] == ["[PASS] echo stdin: CLI fixture passed"], row


def _expecting(command: str, expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": "judged",
        "fixture_type": "cli",
        "input_data": {"command": command},
        "expected": expected,
    }


@pytest.mark.parametrize(
    ("command", "expected", "passed", "detail"),
    [
        (
            'printf \'{ "b" : 2,\\n  "a": [1, {"y": null, "x": "s"}] }\\n\'',
            {"stdout_json": {"a": [1, {"x": "s", "y": None}], "b": 2}},
            True,
            "[PASS] judged: CLI fixture passed",
        ),
        (
            "printf '{\"n\": 1.0}'",
            {"stdout_json": {"n": 1}},
            True,
            "[PASS] judged: CLI fixture passed",
        ),
        (
            "printf '{\"ok\": 1}'",
            {"stdout_json": {"ok": True}},
            False,
            'stdout is not the expected JSON: expected {"ok":true}',
        ),
        (
            'printf \'{"a": 1, "b": 2}\'',
            {"stdout_json": {"a": 1}},
            False,
            'stdout is not the expected JSON: expected {"a":1}',
        ),
        (
            "printf '[2, 1]'",
            {"stdout_json": [1, 2]},
            False,
            "stdout is not the expected JSON: expected [1,2]",
        ),
        (
            "printf '[1, 2, 3]'",
            {"stdout_json": [1, 2]},
            False,
            "stdout is not the expected JSON: expected [1,2]",
        ),
        (
            "printf hello",
            {"stdout_json": {"a": 1}},
            False,
            "stdout is not JSON: Expecting value: line 1 column 1 (char 0)",
        ),
        (
            "sh -c 'echo out; echo oops >&2'",
            {"stderr_contains": ["oops"]},
            True,
            "[PASS] judged: CLI fixture passed",
        ),
        (
            "sh -c 'echo oops'",
            {"stderr_contains": ["oops"]},
            False,
            "stderr missing expected string: 'oops'",
        ),
        (
            "sh -c 'echo oops >&2'",
            {"stdout_contains": ["oops"]},
            False,
            "stdout missing expected string: 'oops'",
        ),
    ],
    ids=[
        "reordered-keys-and-whitespace",
        "one-equals-one-point-zero",
        "true-is-not-one",
        "an-extra-key-is-a-difference",
        "array-order-is-a-difference",
        "a-longer-array-is-a-difference",
        "not-json",
        "stderr-contains",
        "stdout-is-not-stderr",
        "stderr-is-not-stdout",
    ],
)
def test_a_cli_fixture_with_stdout_json_compares_structurally(
    tmp_path: Path, command: str, expected: dict[str, Any], passed: bool, detail: str
) -> None:
    row = _fixtures_row(_repo(tmp_path, CARGO_TREE), [_expecting(command, expected)])

    assert row["passed"] is passed, row
    assert row["message"] == ("1/1" if passed else "0/1") + " fixtures passed", row
    assert len(row["details"]) == 1, row["details"]
    assert detail in row["details"][0], row["details"]


@pytest.mark.parametrize(
    ("expected", "detail"),
    [
        (
            {"stdout_jsn": {"a": 1}},
            "fixtures[0].expected: unexpected keys for cli fixture: stdout_jsn "
            f"(allowed: {ALLOWED_CLI_KEYS})",
        ),
        (
            {"stderr_contain": ["oops"]},
            "fixtures[0].expected: unexpected keys for cli fixture: stderr_contain "
            f"(allowed: {ALLOWED_CLI_KEYS})",
        ),
        (
            {"stdout_contains": []},
            "fixtures[0].expected.stdout_contains: must be a non-empty array of strings",
        ),
    ],
    ids=["stdout_jsn", "stderr_contain", "empty-stdout_contains"],
)
def test_a_misspelled_new_expected_key_is_refused(
    tmp_path: Path, expected: dict[str, Any], detail: str
) -> None:
    """Refused before anything runs; the empty list used to pass whatever was printed."""
    row = _fixtures_row(_repo(tmp_path, CARGO_TREE), [_expecting("printf x", expected)])

    assert row["passed"] is False, row
    assert row["message"] == SCHEMA_REFUSAL, row
    assert row["details"] == [detail], row["details"]


# --- two real factory runs -----------------------------------------------------

#: The engineer writes $JSON_OUT to out.json and commits it, in one iteration.
JSON_ENGINEER = (
    "printf '%s\\n' \"$JSON_OUT\" > out.json && git add -A && "
    "git commit -q -m work && echo '<promise>COMPLETE</promise>'"
)

#: Every gate but the fixtures one is `true`; review, contract and PRs are off.
QUIET_FACTORY = (
    "--yes",
    "--ui",
    "plain",
    "--no-color",
    "--no-tui",
    "--max-parallel",
    "1",
    "--max-retries",
    "0",
    "--review-mode",
    "skip",
    "--contract-check",
    "skip",
    "--no-prs",
)

COMPONENT = "api"


def _json_project(tmp_path: Path) -> tuple[Path, str]:
    """A committed repository whose one component must print {"a": 1, "b": 2}."""
    root = tmp_path / "repo"
    root.mkdir()
    gitrepo.git_in(root, "init", "-q", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    prd_rel = f"scripts/kstrl/feature/{COMPONENT}/prd.json"
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    fixture = _expecting("cat out.json", {"stdout_json": {"a": 1, "b": 2}})
    (root / prd_rel).parent.mkdir(parents=True)
    (root / prd_rel).write_text(
        json.dumps(
            {
                "branchName": f"kstrl/factory/{COMPONENT}",
                "userStories": [story],
                "fixtures": [fixture],
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "version": "1",
        "specFile": "",
        "projectName": "p",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": COMPONENT,
                "title": COMPONENT,
                "description": "",
                "dependencies": [],
                "prdPath": prd_rel,
                "branchName": f"kstrl/factory/{COMPONENT}",
            }
        ],
    }
    gitrepo.git_in(root, "add", "-A")
    write_stack(root)
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "init")
    confirm_stack(root)
    return root, json.dumps(manifest)


def _factory_run(root: Path, manifest: str, json_out: str) -> dict[str, Any]:
    """One `ks factory` run from a fresh manifest; the component's verification event.

    The branch the previous run left is deleted first, as the stale-branch
    refusal tells an operator to, so each run builds the component again.
    """
    subprocess.run(
        ["git", "branch", "-D", f"kstrl/factory/{COMPONENT}"],
        cwd=root,
        capture_output=True,
        timeout=30,
    )
    manifest_path = root / "scripts" / "kstrl" / "manifest.json"
    manifest_path.write_text(manifest, encoding="utf-8")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("KSTRL_") and k not in ("AGENT_CMD", "MODEL", "FACTORY_MAX_PARALLEL")
    }
    env.update(
        AGENT_CMD=JSON_ENGINEER,
        JSON_OUT=json_out,
        KSTRL_FIXTURES_ENABLED="1",
        KSTRL_FIXTURES_TIMEOUT=FIXTURE_TIMEOUT_SECONDS,
        KSTRL_KNOWLEDGE_ENABLED="0",
        KSTRL_NO_TUI="1",
    )
    before = set((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    child = subprocess.Popen(
        [sys.executable, "-m", "kstrl", "factory", "--manifest", str(manifest_path)]
        + [*QUIET_FACTORY, "--root", str(root)],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, _ = child.communicate(timeout=FUSE_SECONDS)
    except subprocess.TimeoutExpired:
        kill_group(child.pid)
        child.communicate()
        pytest.fail(f"`ks factory` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    (events,) = set((root / ".kstrl" / "runs").glob("*/events.jsonl")) - before
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    verified = [
        r["data"]
        for r in rows
        if r["event"] == "verification_result" and r["component"] == COMPONENT
    ]
    assert len(verified) == 1, out
    assert "fixtures" in verified[0]["checks"], out
    return {"returncode": child.returncode, "out": out, **verified[0]}


def test_a_json_fixture_does_not_report_a_snapshot_regression_on_key_order(
    tmp_path: Path,
) -> None:
    """The second run prints the same document with its keys the other way round.

    The first run saves the snapshot; the second compares against it. Raw
    stdout as ``actual`` made that comparison report "Output changed".
    """
    root, manifest = _json_project(tmp_path)

    first = _factory_run(root, manifest, '{"a": 1, "b": 2}')
    second = _factory_run(root, manifest, '{"b":2,  "a":1}')

    assert (first["returncode"], first["passed"]) == (0, True), first
    assert second["failures"] == [], second
    assert (second["returncode"], second["passed"]) == (0, True), second
    snapshot_file = root / ".kstrl" / "snapshots" / f"{COMPONENT}.json"
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert [e["actual"] for e in snapshot["entries"]] == ['{"a":1,"b":2}'], snapshot
