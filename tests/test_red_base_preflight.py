"""#654 slice 1: a base branch that already fails a gate is refused before any engineer runs.

Phase 1 fails a component on any non-zero exit of the whole suite, so on a
base whose suite is already red every component failed Phase 1 after its
engineer was paid, on a test the engineer never touched. The factory now
runs Phase 1's three command gates on the base commit, in a throwaway
worktree, before the first engineer call, and refuses the run with exit 2
when a gate measurably fails there. A gate that ran and measured nothing
(pytest collecting no tests, a timeout) is warned about and recorded, never
refused, because Phase 1 still fails such a row on every component. Every
reading is written to ``.kstrl/runs/<run_id>/base-gates.json``.

End to end: a real git repository under ``tmp_path``, the real ``ks init``,
and the real ``ks factory --manifest`` in its own process group under a
fuse, with a stub engineer that appends one line per call to a log. The
refusal comes from ``_run_preflights``, which runs before the #602 plan
gate: ``_plan_gated`` returns 2 without asking when a preflight refused.
The runs that proceed reach the plan gate with ``[autonomy]`` off, which
``ks init`` seeds commented out, so it asks nothing.

Slice 2 drives the real ``ks doctor --measure`` on the same repositories:
it takes the same reading without starting a run, and its verdict is
not-ready exactly where the factory refuses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.procs import kill_group

#: Real time for one `ks factory` run; a hang fails loudly instead of waiting.
FUSE_SECONDS = 120.0

COMP = "greeter"
PY = sys.executable

#: One passing and one failing test: the base suite is red.
RED = "def test_ok():\n    assert True\n\n\ndef test_broken():\n    assert 1 == 2\n"
#: The same file with the failure fixed.
GREEN = "def test_ok():\n    assert True\n\n\ndef test_broken():\n    assert 1 == 1\n"

#: The three gates, each a command that runs here without network or uv.
GATES = {
    "--test-command": f"{PY} -m pytest -q -p no:cacheprovider",
    "--typecheck-command": "true",
    "--lint-command": "true",
}

HEADLINE = "Refusing to run: the base branch fails a gate Phase 1 runs"

#: The same three gates as environment variables, for `ks doctor`, which
#: takes no gate flags and reads them through the same loader.
GATE_ENV = {
    "KSTRL_VERIFY_TEST_CMD": GATES["--test-command"],
    "KSTRL_VERIFY_TYPECHECK_CMD": GATES["--typecheck-command"],
    "KSTRL_VERIFY_LINT_CMD": GATES["--lint-command"],
}


@dataclass(frozen=True)
class Run:
    out: str
    code: int
    calls: int


def _child_env(env: dict[str, str] | None) -> dict[str, str]:
    """This process's environment without kstrl's own settings, plus ``env``."""
    child_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KSTRL_", "FACTORY_")) and k not in ("AGENT_CMD", "MODEL")
    }
    child_env.update(KSTRL_AGENT_PROBE="0", KSTRL_NO_TUI="1", KSTRL_KNOWLEDGE_ENABLED="0")
    child_env.update(env or {})
    return child_env


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=True
    ).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A repository on ``main`` holding ``files`` and one planned component,
    after the real ``ks init``, everything committed."""
    root = tmp_path / "proj"
    root.mkdir()
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    story = {
        "id": "US-001",
        "title": "Hello",
        "acceptanceCriteria": ["prints hello"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    manifest = {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "demo",
        "baseBranch": "main",
        "singlePr": False,
        "components": [
            {
                "id": COMP,
                "title": "Greeter",
                "description": "",
                "dependencies": [],
                "prdPath": f"scripts/kstrl/feature/{COMP}/prd.json",
                "branchName": f"kstrl/factory/{COMP}",
            }
        ],
    }
    seeded = {
        "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
        f"scripts/kstrl/feature/{COMP}/prd.json": json.dumps(
            {"branchName": f"kstrl/factory/{COMP}", "userStories": [story]}
        ),
        **files,
    }
    for name, text in seeded.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root), "--ui", "plain"])
    assert result.exit_code == 0, result.output
    (root / "scripts" / "kstrl" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "seed")
    return root


def _commit(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")
    git_in(root, "add", name)
    git_in(root, "commit", "-q", "-m", f"change {name}")


def _factory(
    tmp_path: Path,
    root: Path,
    *extra: str,
    gates: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> Run:
    """The real `ks factory --manifest` in its own process group, killed on
    the fuse. The stub engineer appends one line per call to a log."""
    calls = tmp_path / "engineer.calls"
    stub = write_executable(
        tmp_path / "engineer.sh",
        f"#!/bin/sh\necho call >> '{calls}'\ncat >/dev/null\necho '<promise>COMPLETE</promise>'\n",
    )
    child_env = _child_env(env)
    flags = [item for pair in (GATES if gates is None else gates).items() for item in pair]
    args = [
        *(PY, "-m", "kstrl", "factory"),
        *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
        *("--root", str(root), "--agent-cmd", str(stub)),
        *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
        *("--max-retries", "0", "--max-parallel", "1"),
        *("--review-mode", "skip", "--contract-check", "skip"),
        *flags,
        *extra,
    ]
    child = subprocess.Popen(
        args,
        cwd=root,
        env=child_env,
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
    count = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
    return Run(out, child.returncode, count)


def _record(root: Path) -> dict[str, Any]:
    """The one base-gates record the runs under ``root`` wrote."""
    (path,) = sorted((root / ".kstrl" / "runs").glob("*/base-gates.json"))
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _row(record: dict[str, Any], name: str) -> dict[str, Any]:
    (row,) = [row for row in record["checks"] if row["name"] == name]
    return row


def test_a_red_base_is_refused_before_any_engineer_call(tmp_path: Path) -> None:
    """T1."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert "test_broken" in run.out
    assert run.calls == 0, run.out
    record = _record(root)
    assert record["refused"] is True
    assert record["baseSha"] == _git(root, "rev-parse", "main").strip()
    # `ks retry` replays a refused run from its launch record (#436), so the
    # base gates must refuse after that record is written, never before it.
    (run_dir,) = [p.parent for p in (root / ".kstrl" / "runs").glob("*/base-gates.json")]
    assert (run_dir / "launch.json").is_file(), sorted(p.name for p in run_dir.iterdir())
    row = _row(record, "test_suite")
    assert (row["passed"], row["measured"]) == (False, True)
    assert any("test_broken" in name for name in row["failing"]), row


def test_a_green_base_proceeds_to_the_engineer(tmp_path: Path) -> None:
    """T2, the control for T1: the same repository with the fix committed."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})

    run = _factory(tmp_path, root)

    assert HEADLINE not in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["refused"] is False
    assert {row["name"]: row["passed"] for row in record["checks"]} == {
        "test_suite": True,
        "typecheck": True,
        "linter": True,
    }
    # #700: every command ran on the host, and both records say so.
    assert record["isolation"] == "none: ran on the host"
    (events,) = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    verdicts = [
        line["data"]
        for line in map(json.loads, events.read_text(encoding="utf-8").splitlines())
        if line["event"] == "verification_result"
    ]
    assert [v["isolation"] for v in verdicts] == ["none: ran on the host"], verdicts


@pytest.mark.parametrize(
    ("committed", "uncommitted", "refused"),
    [(RED, GREEN, True), (GREEN, RED, False)],
    ids=["red-commit-uncommitted-fix", "green-commit-uncommitted-break"],
)
def test_the_commit_is_measured_not_the_root_checkout(
    tmp_path: Path, committed: str, uncommitted: str, refused: bool
) -> None:
    """T3. Components are cut from the commit, so a fix left uncommitted in
    the root checkout does not make the base green, and a break left there
    does not make it red."""
    root = _repo(tmp_path, {"tests/test_base.py": committed})
    (root / "tests" / "test_base.py").write_text(uncommitted, encoding="utf-8")

    run = _factory(tmp_path, root)

    assert (HEADLINE in run.out) is refused, run.out
    assert run.calls == (0 if refused else 1), run.out
    assert _record(root)["refused"] is refused


def test_the_base_branch_is_measured_not_the_checked_out_branch(tmp_path: Path) -> None:
    """T3b. Components are cut from the base branch, so a fix committed on the
    branch the root checkout is on does not make a red base green."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    git_in(root, "checkout", "-q", "-b", "work")
    _commit(root, "tests/test_base.py", GREEN)

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    assert _record(root)["baseSha"] == _git(root, "rev-parse", "main").strip()


def test_a_red_base_is_refused_without_worktrees(tmp_path: Path) -> None:
    """T4. Phase 1 still runs under --no-worktrees, in the root checkout."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    run = _factory(tmp_path, root, "--no-worktrees")

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    assert _record(root)["refused"] is True


def test_a_lint_failure_on_the_base_is_refused_and_named(tmp_path: Path) -> None:
    """T5. Every gate Phase 1 runs is measured, not only the tests."""
    root = _repo(
        tmp_path,
        {"tests/test_base.py": GREEN, "greeter.py": "import os\n"},
    )

    run = _factory(tmp_path, root, gates={**GATES, "--lint-command": f"{PY} -m ruff check ."})

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert "F401" in run.out
    assert run.calls == 0, run.out
    assert "F401" in _row(_record(root), "linter")["failing"]


def test_an_empty_gate_command_is_recorded_and_not_refused(tmp_path: Path) -> None:
    """T6. An empty command is the operator turning the gate off."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    gates = {k: v for k, v in GATES.items() if k != "--typecheck-command"}

    run = _factory(tmp_path, root, gates=gates, env={"KSTRL_VERIFY_TYPECHECK_CMD": ""})

    assert HEADLINE not in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["refused"] is False
    assert record["notMeasured"] == ["typecheck:no_target"]


def test_no_verify_skips_the_measurement_and_records_why(tmp_path: Path) -> None:
    """T7. Under --no-verify Phase 1 runs no gate, so nothing is measured."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    run = _factory(tmp_path, root, "--no-verify", gates={})

    assert HEADLINE not in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["measured"] is False
    assert record["skippedReason"] == "--no-verify: Phase 1 runs no gate"
    assert record["checks"] == []
    assert record["refused"] is False


def test_no_worktree_outlives_the_measurement(tmp_path: Path) -> None:
    """T8. After a refused run and a proceeding one, git registers only the
    main checkout."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    refused = _factory(tmp_path, root)
    after_refusal = _git(root, "worktree", "list", "--porcelain")
    _commit(root, "tests/test_base.py", GREEN)
    proceeded = _factory(tmp_path, root)
    after_run = _git(root, "worktree", "list", "--porcelain")

    assert (refused.code, proceeded.calls) == (2, 1), refused.out + proceeded.out
    assert after_refusal.count("worktree ") == 1, after_refusal
    assert after_run.count("worktree ") == 1, after_run


def test_a_base_with_no_tests_proceeds_with_a_warning(tmp_path: Path) -> None:
    """T9. pytest exits 5 when it collects nothing: the gate measured
    nothing, which a greenfield repository always does."""
    root = _repo(tmp_path, {})

    run = _factory(tmp_path, root)

    assert HEADLINE not in run.out
    assert "measured nothing" in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["refused"] is False
    row = _row(record, "test_suite")
    assert (row["passed"], row["measured"]) == (False, False)
    assert "exit code 5" in row["message"]


def test_a_gate_that_times_out_on_the_base_proceeds_and_phase_1_still_fails(
    tmp_path: Path,
) -> None:
    """T10. A timeout measured nothing: warned, recorded, not refused, and
    Phase 1 fails the component on the same timeout, as before."""
    slow = "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"
    root = _repo(tmp_path, {"tests/test_base.py": slow})

    run = _factory(tmp_path, root, env={"KSTRL_TIMEOUT_VERIFY": "2"})

    assert HEADLINE not in run.out
    assert "measured nothing" in run.out
    assert run.calls == 1, run.out
    assert _row(_record(root), "test_suite")["measured"] is False
    (failed,) = [line for line in run.out.splitlines() if "Phase 1 FAILED for" in line]
    assert "test_suite" in failed, run.out


def test_a_reading_that_cannot_be_recorded_refuses_the_run(tmp_path: Path) -> None:
    """T11. A later phase reads the record, so a run that cannot write it
    must not start (the #436 rule). The worktree setup, which runs on the
    base before the record is written, puts a directory where it goes."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    block = f"for d in '{root}'/.kstrl/runs/*/; do mkdir -p \"$d/base-gates.json\"; done"

    run = _factory(tmp_path, root, env={"KSTRL_FACTORY_WORKTREE_SETUP_COMMAND": block})

    assert run.code == 2, run.out
    assert "the base reading cannot be recorded" in run.out
    assert run.calls == 0, run.out


# --- slice 2: `ks doctor --measure` takes the same reading ----------------


def _doctor(root: Path, *args: str, env: dict[str, str]) -> tuple[int, str]:
    """The real `ks doctor --root <root> <args>` in its own process group,
    killed on the fuse. Returns the exit code and stdout."""
    child = subprocess.Popen(
        [PY, "-m", "kstrl", "doctor", "--root", str(root), *args],
        cwd=root,
        env=_child_env(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, _err = child.communicate(timeout=FUSE_SECONDS)
    except subprocess.TimeoutExpired:
        kill_group(child.pid)
        child.communicate()
        pytest.fail(f"`ks doctor` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    return child.returncode, out


def _doctor_json(root: Path, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    code, out = _doctor(root, "--measure", "--json", env=env)
    document: dict[str, Any] = json.loads(out)
    return code, document


def _doctor_row(document: dict[str, Any]) -> dict[str, Any]:
    (row,) = [row for row in document["checks"] if row["name"] == "base_gates"]
    return row


def test_doctor_measure_reports_a_red_base_not_ready(tmp_path: Path) -> None:
    """D1, the T1 repository: the reading `ks factory` refuses on is a
    failed row, and the failing test is named on stdout."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    text_code, text = _doctor(root, "--measure", env=GATE_ENV)
    code, document = _doctor_json(root, GATE_ENV)

    assert text_code == 1, text
    assert "[fail] base_gates" in text
    assert "test_broken" in text
    assert "xfail(strict=True)" in text
    assert code == 1, document
    assert document["verdict"] == "not-ready"
    reading = document["base_gates"]
    assert reading["refused"] is True
    assert reading["baseSha"] == _git(root, "rev-parse", "main").strip()
    assert any("test_broken" in name for name in _row(reading, "test_suite")["failing"])


def test_doctor_measure_reports_a_green_base_with_three_measured_rows(tmp_path: Path) -> None:
    """D2, the T2 repository and the control for D1: the fix committed."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})

    code, document = _doctor_json(root, GATE_ENV)

    assert code == 0, document
    assert _doctor_row(document)["status"] == "ok"
    reading = document["base_gates"]
    assert reading["refused"] is False
    assert {(r["name"], r["passed"], r["measured"]) for r in reading["checks"]} == {
        ("test_suite", True, True),
        ("typecheck", True, True),
        ("linter", True, True),
    }
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_doctor_measure_warns_on_a_base_with_no_tests(tmp_path: Path) -> None:
    """D9, the T9 repository: pytest exits 5 having collected nothing. That
    measured nothing, which `ks factory` warns about and does not refuse."""
    root = _repo(tmp_path, {})

    code, document = _doctor_json(root, GATE_ENV)

    assert code == 0, document
    row = _doctor_row(document)
    assert row["status"] == "warn"
    assert "measured nothing" in row["detail"]
    reading = document["base_gates"]
    assert reading["refused"] is False
    test_row = _row(reading, "test_suite")
    assert (test_row["passed"], test_row["measured"]) == (False, False)
    assert "exit code 5" in test_row["message"]


def test_doctor_measure_fails_a_base_it_cannot_resolve(tmp_path: Path) -> None:
    """D10: no branch git would name as the base, so nothing was measured,
    and an agent-ready verdict must rest on a reading."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    git_in(root, "branch", "-m", "main", "work")

    code, document = _doctor_json(root, GATE_ENV)

    assert code == 1, document
    row = _doctor_row(document)
    assert row["status"] == "fail"
    assert "the base branch main was not measured" in row["detail"]
    assert document["base_gates"]["checks"] == []


def test_doctor_runs_a_repo_command_only_under_measure(tmp_path: Path) -> None:
    """The control: without --measure, `ks doctor` runs none of the
    repository's commands. The default gates run through `uv`, and a stub
    `uv` first on PATH logs every call; the --measure run shows the stub is
    on the path the gates take."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "uv-argv.log"
    write_executable(stubs / "uv", f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}

    plain_code, plain = _doctor(root, "--json", env=env)
    after_plain = log.read_text(encoding="utf-8") if log.exists() else ""
    _doctor(root, "--measure", "--json", env=env)

    assert plain_code == 0, plain
    assert json.loads(plain)["base_gates"] is None
    assert after_plain == ""
    assert "run pytest" in log.read_text(encoding="utf-8")


def test_doctor_measure_reads_the_base_branch_not_the_checkout(tmp_path: Path) -> None:
    """D3, the doctor's T3b: a fix committed on the branch the checkout is
    on does not make a red base green, because components are cut from the
    base branch."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    git_in(root, "checkout", "-q", "-b", "work")
    _commit(root, "tests/test_base.py", GREEN)

    code, document = _doctor_json(root, GATE_ENV)

    assert code == 1, document
    assert document["base_gates"]["baseBranch"] == "main"
    assert document["base_gates"]["baseSha"] == _git(root, "rev-parse", "main").strip()


def test_doctor_measure_takes_the_reading_ks_factory_takes(tmp_path: Path) -> None:
    """D11, addendum item 9: agent-ready is keyed on (baseSha, verifyDigest,
    setupCommand). The doctor's reading and the one `ks factory` records on
    the same repository agree on all three, and on the refusal."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    setup = {"KSTRL_FACTORY_WORKTREE_SETUP_COMMAND": "true"}

    _code, document = _doctor_json(root, {**GATE_ENV, **setup})
    run = _factory(tmp_path, root, env=setup)

    assert run.code == 2, run.out
    record = _record(root)
    keys = ("baseSha", "verifyDigest", "setupCommand", "refused")
    assert {k: document["base_gates"][k] for k in keys} == {k: record[k] for k in keys}
    assert document["base_gates"]["setupCommand"] == "true"
    assert (
        _row(document["base_gates"], "test_suite")["failing"]
        == _row(record, "test_suite")["failing"]
    )


def test_doctor_measure_still_measures_under_a_config_that_only_warns(tmp_path: Path) -> None:
    """A kstrl.toml whose only problem is a section kstrl continues without
    ([evolution]) is a warning on kstrl_config, not a failure: every
    command still starts on it, so `ks doctor --measure` still measures."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    _commit(root, "kstrl.toml", '[evolution]\nlookback_runs = "many"\n')

    code, document = _doctor_json(root, GATE_ENV)

    (config_row,) = [row for row in document["checks"] if row["name"] == "kstrl_config"]
    assert config_row["status"] == "warn", config_row
    assert code == 0, document
    assert _doctor_row(document)["status"] == "ok"
    assert document["base_gates"]["refused"] is False
