"""#654 slice 1: a base branch that already fails a gate is refused before any engineer runs.

Phase 1 fails a component on any non-zero exit of the whole suite, so on a
base whose suite is already red every component failed Phase 1 after its
engineer was paid, on a test the engineer never touched. The factory now
runs Phase 1's command gates on the base commit, in a throwaway worktree,
before the first engineer call, and refuses the run with exit 2 when a
gate measurably fails there. Every reading is written to
``.kstrl/runs/<run_id>/base-gates.json``.

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

#696 flag day: Phase 1's gates are the ``[stack]``'s checks, confirmed by
``write_stack``/``confirm_stack``, not CLI flags or env-var overrides (both
are gone: the four ``--*-command`` flags are deleted from ``ks factory``,
and ``KSTRL_VERIFY_*_CMD``/``KSTRL_FACTORY_WORKTREE_SETUP_COMMAND`` are
refused as retired). Row names move from ``test_suite``/``typecheck``/
``linter`` to ``stack:tests``/``stack:typecheck``/``stack:lint``. #696
decision 4 also removes the old carve-out for a check that "measured
nothing": every check that did not pass now refuses the base whether or
not it measured anything, so pytest's exit 5 (no tests collected) and a
timeout are both refusals now, not warnings (decision 6: Phase 1 reads
exit status only, no per-tool parsing of what a check's output means).

#700 slice 2: under a confirmed ``[stack]``, ``ks factory`` proves the
isolation rung before it measures the base, so every test here that calls
``_factory()`` on a repository with a ``[stack]`` needs ``@needs_nono``
(macOS + nono 0.79, see the local marker below) and skips elsewhere.
``ks doctor --measure`` proves no rung (``VerifyConfig.load`` leaves
``rung`` at its default None), so the doctor-only tests do not need it.

The local ``needs_nono`` here is not imported from
``tests.test_isolation_rung``: that module imports ``_repo``/
``_doctor_json`` FROM this one, so importing back would be a cycle Python
cannot resolve (the mark is defined after the import line on the far
side). It is the same check, kept in sync by hand.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in, set_identity
from tests.helpers.procs import kill_group
from tests.helpers.stack_confirmation import confirm_stack, write_stack

#: Real time for one `ks factory` run; a hang fails loudly instead of waiting.
FUSE_SECONDS = 120.0

COMP = "greeter"
PY = sys.executable

#: One passing and one failing test: the base suite is red.
RED = "def test_ok():\n    assert True\n\n\ndef test_broken():\n    assert 1 == 2\n"
#: The same file with the failure fixed.
GREEN = "def test_ok():\n    assert True\n\n\ndef test_broken():\n    assert 1 == 1\n"

#: The default [stack]: three checks named "tests"/"typecheck"/"lint", rows
#: "stack:tests"/"stack:typecheck"/"stack:lint" (#696).
CHECKS = {
    "tests": f"{PY} -m pytest -q -p no:cacheprovider",
    "typecheck": "true",
    "lint": "true",
}

HEADLINE = "Refusing to run: the base branch fails a gate Phase 1 runs"

#: Duplicated from tests/test_isolation_rung.py rather than imported: see
#: the module docstring for why the import would cycle.
_NONO = os.environ.get("KSTRL_NONO", "").strip() or shutil.which("nono") or ""


def _nono_version(path: str) -> tuple[int, ...]:
    if not path:
        return ()
    try:
        with tempfile.TemporaryDirectory(prefix="kstrl-nono-version-") as scratch:
            env = {**os.environ, "NONO_NO_UPDATE_CHECK": "1", "TMPDIR": f"{scratch}/"}
            out = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                cwd=scratch,
                env=env,
            ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(part) for part in found.groups()) if found else ()


_NONO_VERSION = _nono_version(_NONO)

needs_nono = pytest.mark.skipif(
    sys.platform != "darwin" or _NONO_VERSION < (0, 79, 0),
    reason=f"needs macOS and nono 0.79 or later; found {_NONO or 'none'} "
    f"{'.'.join(map(str, _NONO_VERSION)) or ''}; set KSTRL_NONO",
)


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


def _repo(
    tmp_path: Path,
    files: dict[str, str],
    *,
    checks: Mapping[str, str] | None = CHECKS,
    setup: str = "",
    writable: tuple[str, ...] = (),
) -> Path:
    """A repository on ``main`` holding ``files`` and one planned component,
    after the real ``ks init``, everything committed.

    ``checks`` is written as the ``[stack]`` and confirmed (#696);
    ``checks=None`` writes no ``[stack]`` at all, the ``--no-verify``
    control. Always named ``tmp_path / "proj"``: a caller that must know
    the path before this returns (to bake it into a shell command passed
    as ``setup``) may rely on that.
    """
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
    if checks is not None:
        write_stack(root, checks, setup=setup, writable=writable)
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "seed")
    if checks is not None:
        confirm_stack(root)
    return root


def _commit(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")
    git_in(root, "add", name)
    git_in(root, "commit", "-q", "-m", f"change {name}")


def _factory(
    tmp_path: Path,
    root: Path,
    *extra: str,
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
    args = [
        *(PY, "-m", "kstrl", "factory"),
        *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
        *("--root", str(root), "--agent-cmd", str(stub)),
        *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
        *("--max-retries", "0", "--max-parallel", "1"),
        *("--review-mode", "skip", "--contract-check", "skip"),
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


@needs_nono
def test_a_red_base_is_refused_before_any_engineer_call(tmp_path: Path) -> None:
    """T1."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    record = _record(root)
    assert record["refused"] is True
    assert record["baseSha"] == _git(root, "rev-parse", "main").strip()
    # `ks retry` replays a refused run from its launch record (#436), so the
    # base gates must refuse after that record is written, never before it.
    (run_dir,) = [p.parent for p in (root / ".kstrl" / "runs").glob("*/base-gates.json")]
    assert (run_dir / "launch.json").is_file(), sorted(p.name for p in run_dir.iterdir())
    row = _row(record, "stack:tests")
    # #696 decision 6: Phase 1 reads exit status only, never output, so the
    # record names no failing test any more; the message is the exit code.
    assert (row["passed"], row["measured"]) == (False, True)
    assert "exited 1" in row["message"], row


@needs_nono
def test_a_green_base_proceeds_to_the_engineer(tmp_path: Path) -> None:
    """T2, the control for T1: the same repository with the fix committed."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})

    run = _factory(tmp_path, root)

    assert HEADLINE not in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["refused"] is False
    assert {row["name"]: row["passed"] for row in record["checks"]} == {
        "stack:tests": True,
        "stack:typecheck": True,
        "stack:lint": True,
    }
    # #700: under a confirmed [stack] every check runs inside the proven
    # TEST-zone rung, never on the bare host.
    assert record["isolation"] != "none: ran on the host", record
    (events,) = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    verdicts = [
        line["data"]
        for line in map(json.loads, events.read_text(encoding="utf-8").splitlines())
        if line["event"] == "verification_result"
    ]
    assert verdicts and all(v["isolation"] != "none: ran on the host" for v in verdicts), verdicts


@needs_nono
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


@needs_nono
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


@needs_nono
def test_a_red_base_is_refused_without_worktrees(tmp_path: Path) -> None:
    """T4. Phase 1 still runs under --no-worktrees, in the root checkout."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})

    run = _factory(tmp_path, root, "--no-worktrees")

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    assert _record(root)["refused"] is True


@needs_nono
def test_a_lint_failure_on_the_base_is_refused_and_named(tmp_path: Path) -> None:
    """T5. Every check the [stack] names is measured, not only the tests."""
    root = _repo(
        tmp_path,
        {"tests/test_base.py": GREEN, "greeter.py": "import os\n"},
        checks={**CHECKS, "lint": f"{PY} -m ruff check ."},
    )

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    # #696 decision 6: no parsed failure detail any more; the row's message
    # is the exit code (ruff exits 1 on a finding).
    assert "exited 1" in _row(_record(root), "stack:lint")["message"]


@needs_nono
def test_a_stack_with_no_typecheck_check_measures_only_what_it_lists(tmp_path: Path) -> None:
    """T6 (#696): [stack] has no three fixed gate slots to turn one off in;
    the operator just omits the check. Measuring two instead of three must
    not refuse and must not invent a row for the one left out."""
    root = _repo(
        tmp_path,
        {"tests/test_base.py": GREEN},
        checks={"tests": CHECKS["tests"], "lint": CHECKS["lint"]},
    )

    run = _factory(tmp_path, root)

    assert HEADLINE not in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["refused"] is False
    assert {row["name"] for row in record["checks"]} == {"stack:tests", "stack:lint"}
    assert record["notMeasured"] == []


def test_no_verify_skips_the_measurement_and_records_why(tmp_path: Path) -> None:
    """T7. With no [stack] at all, --no-verify runs no gate, so nothing is
    measured and no isolation rung is needed (#696, #700): this is the one
    test in the file with no [stack], so it is the one that needs no
    @needs_nono."""
    root = _repo(tmp_path, {"tests/test_base.py": RED}, checks=None)

    run = _factory(tmp_path, root, "--no-verify")

    assert HEADLINE not in run.out
    assert run.calls == 1, run.out
    record = _record(root)
    assert record["measured"] is False
    assert record["skippedReason"] == "--no-verify: Phase 1 runs no gate"
    assert record["checks"] == []
    assert record["refused"] is False


@needs_nono
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


@needs_nono
def test_a_base_with_no_tests_now_refuses_as_a_measured_failure(tmp_path: Path) -> None:
    """T9 (#696 decision 6): Phase 1 no longer parses what a check's output
    means, only its exit status; pytest's exit 5 ("no tests collected") is
    not 126 or 127, so it is a measured failure like any other, and the
    greenfield base that always produces it now refuses rather than warns."""
    root = _repo(tmp_path, {})

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    record = _record(root)
    assert record["refused"] is True
    row = _row(record, "stack:tests")
    assert (row["passed"], row["measured"]) == (False, True)
    assert "exited 5" in row["message"]


@needs_nono
def test_a_gate_that_times_out_on_the_base_now_refuses_the_run(tmp_path: Path) -> None:
    """T10 (#696 decision 4): refusal no longer carves out a check that
    "measured nothing"; every check that did not pass refuses, a timeout
    included, so this now refuses where it used to warn and proceed."""
    slow = "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"
    root = _repo(tmp_path, {"tests/test_base.py": slow})

    run = _factory(tmp_path, root, env={"KSTRL_TIMEOUT_VERIFY": "2"})

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert run.calls == 0, run.out
    row = _row(_record(root), "stack:tests")
    assert (row["passed"], row["measured"]) == (False, False)
    assert "timed out" in row["message"]


@needs_nono
def test_a_reading_that_cannot_be_recorded_refuses_the_run(tmp_path: Path) -> None:
    """T11. A later phase reads the record, so a run that cannot write it
    must not start (the #436 rule). The [stack]'s setup, which runs on the
    base before the record is written, puts a directory where it goes
    (#696: setup now comes from the confirmed [stack], never a retired
    env var). ``_repo`` always names its repository ``tmp_path / "proj"``,
    so that path is known before the setup command is written into it.
    The run directory is outside the rung's default trees (#700), so this
    names it in ``writable``: without that the setup's write is denied by
    nono itself, a different (and also real) refusal from the one this
    test is about."""
    root_path = tmp_path / "proj"
    runs_dir = root_path / ".kstrl" / "runs"
    block = f"for d in '{runs_dir}'/*/; do mkdir -p \"$d/base-gates.json\"; done"
    root = _repo(tmp_path, {"tests/test_base.py": GREEN}, setup=block, writable=(str(runs_dir),))
    assert root == root_path

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert "the base reading cannot be recorded" in run.out
    assert run.calls == 0, run.out


# --- slice 2: `ks doctor --measure` takes the same reading ----------------


def _doctor(root: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
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


def _doctor_json(root: Path, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    code, out = _doctor(root, "--measure", "--json", env=env)
    document: dict[str, Any] = json.loads(out)
    return code, document


def _doctor_row(document: dict[str, Any]) -> dict[str, Any]:
    (row,) = [row for row in document["checks"] if row["name"] == "base_gates"]
    return row


def test_doctor_measure_reports_a_red_base_not_ready(tmp_path: Path) -> None:
    """D1, the T1 repository: the reading `ks factory` refuses on is a
    failed row, named on stdout by its check and exit code (#696 decision
    6: no parsed failure detail, exit status only). `ks doctor --measure`
    proves no isolation rung (its VerifyConfig carries no rung), so this
    needs no @needs_nono even though T1 does."""
    # A red base fails the stack's clean replay, which the first
    # `ks doctor --measure` records (#700 slice 3): a second reading of the
    # same repository would be refused for that, so each form gets its own.
    for name in ("text", "json"):
        (tmp_path / name).mkdir()
    text_root = _repo(tmp_path / "text", {"tests/test_base.py": RED})
    root = _repo(tmp_path / "json", {"tests/test_base.py": RED})

    text_code, text = _doctor(text_root, "--measure")
    code, document = _doctor_json(root)

    assert text_code == 1, text
    assert "[fail] base_gates" in text
    assert "stack:tests fails on main" in text
    assert "exited 1" in text
    assert "xfail(strict=True)" in text
    assert code == 1, document
    assert document["verdict"] == "not-ready"
    reading = document["base_gates"]
    assert reading["refused"] is True
    assert reading["baseSha"] == _git(root, "rev-parse", "main").strip()
    assert "exited 1" in _row(reading, "stack:tests")["message"]


def test_doctor_measure_reports_a_green_base_with_three_measured_rows(tmp_path: Path) -> None:
    """D2, the T2 repository and the control for D1: the fix committed."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})

    code, document = _doctor_json(root)

    assert code == 0, document
    assert _doctor_row(document)["status"] == "ok"
    reading = document["base_gates"]
    assert reading["refused"] is False
    assert {(r["name"], r["passed"], r["measured"]) for r in reading["checks"]} == {
        ("stack:tests", True, True),
        ("stack:typecheck", True, True),
        ("stack:lint", True, True),
    }
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_doctor_measure_reports_a_base_with_no_tests_as_not_ready(tmp_path: Path) -> None:
    """D9 (#696 decision 6), the T9 repository: the same exit-status-only
    measurement that makes `ks factory` refuse a base with no tests makes
    `ks doctor --measure` read it not-ready instead of warning."""
    root = _repo(tmp_path, {})

    code, document = _doctor_json(root)

    assert code == 1, document
    row = _doctor_row(document)
    assert row["status"] == "fail"
    reading = document["base_gates"]
    assert reading["refused"] is True
    test_row = _row(reading, "stack:tests")
    assert (test_row["passed"], test_row["measured"]) == (False, True)
    assert "exited 5" in test_row["message"]


def test_doctor_measure_fails_a_base_it_cannot_resolve(tmp_path: Path) -> None:
    """D10: no branch git would name as the base, so nothing was measured,
    and an agent-ready verdict must rest on a reading."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    git_in(root, "branch", "-m", "main", "work")

    code, document = _doctor_json(root)

    assert code == 1, document
    row = _doctor_row(document)
    assert row["status"] == "fail"
    assert "the base branch main was not measured" in row["detail"]
    assert document["base_gates"]["checks"] == []


def test_doctor_runs_a_repo_command_only_under_measure(tmp_path: Path) -> None:
    """The control: without --measure, `ks doctor` runs none of the
    [stack]'s checks. The "tests" check appends to a log, so --measure is
    the only invocation that leaves a trace of having run it."""
    log = tmp_path / "ran.log"
    root = _repo(
        tmp_path,
        {"tests/test_base.py": GREEN},
        checks={"tests": f"echo ran >> '{log}'", "typecheck": "true", "lint": "true"},
    )

    plain_code, plain = _doctor(root, "--json")
    after_plain = log.read_text(encoding="utf-8") if log.exists() else ""
    _doctor(root, "--measure", "--json")

    assert plain_code == 0, plain
    assert json.loads(plain)["base_gates"] is None
    assert after_plain == ""
    assert log.read_text(encoding="utf-8") == "ran\n"


def test_doctor_measure_reads_the_base_branch_not_the_checkout(tmp_path: Path) -> None:
    """D3, the doctor's T3b: a fix committed on the branch the checkout is
    on does not make a red base green, because components are cut from the
    base branch."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    git_in(root, "checkout", "-q", "-b", "work")
    _commit(root, "tests/test_base.py", GREEN)

    code, document = _doctor_json(root)

    assert code == 1, document
    assert document["base_gates"]["baseBranch"] == "main"
    assert document["base_gates"]["baseSha"] == _git(root, "rev-parse", "main").strip()


@needs_nono
def test_doctor_measure_takes_the_reading_ks_factory_takes(tmp_path: Path) -> None:
    """D11, addendum item 9: agent-ready is keyed on (baseSha, verifyDigest,
    setupCommand). The doctor's reading and the one `ks factory` records on
    the same repository agree on all three, and on the refusal. The
    [stack]'s setup is baked in once, read by both; this test needs
    @needs_nono because the `ks factory` half proves the rung, even though
    the doctor half does not."""
    root = _repo(tmp_path, {"tests/test_base.py": RED}, setup="true")

    # `ks factory` first: the doctor's clean replay of a red base fails and is
    # recorded, after which the factory would refuse on the stack (#700 slice 3).
    run = _factory(tmp_path, root)
    _code, document = _doctor_json(root)

    assert run.code == 2, run.out
    record = _record(root)
    keys = ("baseSha", "verifyDigest", "setupCommand", "refused")
    assert {k: document["base_gates"][k] for k in keys} == {k: record[k] for k in keys}
    assert document["base_gates"]["setupCommand"] == "true"
    assert (
        _row(document["base_gates"], "stack:tests")["message"]
        == _row(record, "stack:tests")["message"]
    )


def test_doctor_measure_still_measures_under_a_config_that_only_warns(tmp_path: Path) -> None:
    """A kstrl.toml whose only problem is a section kstrl continues without
    ([evolution]) is a warning on kstrl_config, not a failure: every
    command still starts on it, so `ks doctor --measure` still measures."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    # `ks init`'s scaffold already declares [evolution] (commented out, so
    # it is a real but empty table): uncomment its one key with a bad
    # value, rather than appending a second [evolution] header, which TOML
    # refuses as the same table declared twice.
    toml_text = (root / "kstrl.toml").read_text(encoding="utf-8")
    assert "# lookback_runs = 10" in toml_text, toml_text
    (root / "kstrl.toml").write_text(
        toml_text.replace("# lookback_runs = 10", 'lookback_runs = "many"'), encoding="utf-8"
    )
    git_in(root, "add", "kstrl.toml")
    git_in(root, "commit", "-q", "-m", "evolution section")

    code, document = _doctor_json(root)

    (config_row,) = [row for row in document["checks"] if row["name"] == "kstrl_config"]
    assert config_row["status"] == "warn", config_row
    assert code == 0, document
    assert _doctor_row(document)["status"] == "ok"
    assert document["base_gates"]["refused"] is False
