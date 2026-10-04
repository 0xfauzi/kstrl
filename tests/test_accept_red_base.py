"""#654 slice 4: `ks factory --accept-red-base <sha12>` runs on a base whose gates fail.

Slice 1 refuses a run whose base branch already fails a gate Phase 1 runs.
The escape the owner chose (decision 2 (b)) is bound to the measured commit
and to one run: the value must be at least 12 characters of the sha the run
measures, anything else refuses before any engineer call and names both, and
`ks retry` replays it from the launch record, so a retry on a base that moved
refuses again. The accepted value and what it waived go in base-gates.json.

Under a ``[stack]`` the acceptance waives the checks that ran and measurably
failed and nothing else: a failed setup ran no check and a check that exited
126 or 127 or timed out measured nothing, so there is no red reading to
accept, and what a check leaves in ``git status`` would be an out-of-scope
edit in every engineer's diff whatever the engineer did.

End to end: a real git repository after the real ``ks init``, and the real
``ks factory`` and ``ks retry`` as subprocesses in their own process group
under a fuse, with a stub engineer that appends one line per call to a log.
The harnesses are the slice 1 and #696 ones, imported rather than copied.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests import test_stack_e2e as stack_e2e
from tests.helpers.procs import kill_group
from tests.test_red_base_preflight import (
    FUSE_SECONDS,
    GREEN,
    HEADLINE,
    PY,
    RED,
    _child_env,
    _commit,
    _factory,
    _git,
    _repo,
)

FLAG = "--accept-red-base"
ACCEPTED = "Accepted by --accept-red-base"


def _records(root: Path) -> list[dict[str, Any]]:
    """Every base-gates record the runs under ``root`` wrote, oldest first."""
    paths = sorted(
        (root / ".kstrl" / "runs").glob("*/base-gates.json"), key=lambda p: p.stat().st_mtime
    )
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _retry(root: Path, component: str) -> tuple[int, str]:
    """The real `ks retry <component>` in its own process group, killed on the fuse."""
    child = subprocess.Popen(
        [PY, "-m", "kstrl", "retry", component, "--root", str(root)]
        + ["--yes", "--ui", "plain", "--no-color"],
        cwd=root,
        env=_child_env(None),
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
        pytest.fail(f"`ks retry` outlived its {FUSE_SECONDS}s fuse (hung, not failed)")
    return child.returncode, out


@pytest.mark.parametrize("digits", [12, 40], ids=["twelve-characters", "full-sha"])
def test_a_red_base_whose_sha_is_accepted_proceeds_and_records_it(
    tmp_path: Path, digits: int
) -> None:
    """The T1 repository, with the acceptance: the engineer runs, the
    refusal T1 shows is printed as accepted, and the record says so. Any
    prefix of 12 characters or more names the base, the full sha included."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    sha = _git(root, "rev-parse", "main").strip()
    value = sha[:digits]

    run = _factory(tmp_path, root, FLAG, value)

    assert HEADLINE not in run.out, run.out
    assert run.calls == 1, run.out
    assert ACCEPTED in run.out and "test_broken" in run.out, run.out
    (record,) = _records(root)
    assert record["refused"] is False, record
    assert record["reasons"] == [], record
    assert record["baseSha"] == sha
    assert record["acceptRedBase"] == value
    assert len(record["accepted"]) == 1 and "test_broken" in record["accepted"][0], record


def _not_a_prefix(sha: str) -> str:
    """Twelve hex characters that are not the start of ``sha``."""
    return ("1" if sha[0] == "0" else "0") + sha[1:12]


def _wrong_tail(sha: str) -> str:
    """Thirteen characters whose first 12 are the start of ``sha`` and whose
    13th is not: a prefix of the 12 the refusal prints, not of the sha."""
    return sha[:12] + ("1" if sha[12] == "0" else "0")


@pytest.mark.parametrize(
    "spell",
    [lambda sha: sha[:11], _not_a_prefix, _wrong_tail],
    ids=["eleven-characters", "not-a-prefix", "right-start-wrong-tail"],
)
def test_an_acceptance_that_does_not_name_the_measured_base_is_refused(
    tmp_path: Path, spell: Any
) -> None:
    """Fewer than 12 characters, or 12 that are not the measured sha's
    start: exit 2 before any engineer call, naming the value and the sha."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    sha = _git(root, "rev-parse", "main").strip()
    value = spell(sha)

    run = _factory(tmp_path, root, FLAG, value)

    assert run.code == 2, run.out
    assert HEADLINE in run.out
    assert f"{FLAG} {value} does not name the measured base" in run.out, run.out
    assert sha in run.out
    assert run.calls == 0, run.out
    (record,) = _records(root)
    assert record["refused"] is True
    assert record["acceptRedBase"] == value
    assert record["accepted"] == []


def test_an_acceptance_of_a_base_that_moved_is_refused(tmp_path: Path) -> None:
    """The acceptance binds to the commit, not to the failures: a new commit
    on the base, still red the same way, refuses the old acceptance."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    old = _git(root, "rev-parse", "main").strip()
    _commit(root, "NOTES.md", "moved\n")
    new = _git(root, "rev-parse", "main").strip()

    run = _factory(tmp_path, root, FLAG, old[:12])

    assert run.code == 2, run.out
    assert f"{FLAG} {old[:12]} does not name the measured base" in run.out, run.out
    assert new in run.out
    assert run.calls == 0, run.out
    (record,) = _records(root)
    assert (record["baseSha"], record["refused"]) == (new, True)


def test_an_acceptance_with_no_verify_is_refused(tmp_path: Path) -> None:
    """Under --no-verify no base is measured, so no value can name it."""
    root = _repo(tmp_path, {"tests/test_base.py": GREEN})
    sha = _git(root, "rev-parse", "main").strip()

    run = _factory(tmp_path, root, "--no-verify", FLAG, sha[:12], gates={})

    assert run.code == 2, run.out
    assert f"{FLAG} {sha[:12]} does not name the measured base" in run.out, run.out
    assert run.calls == 0, run.out
    (record,) = _records(root)
    assert (record["measured"], record["refused"]) == (False, True)


def test_ks_retry_replays_the_acceptance(tmp_path: Path) -> None:
    """The accepted run reaches Phase 1, which fails the component on the
    red test; `ks retry` re-enters the factory with the acceptance from the
    launch record, so the retry reaches the engineer too."""
    root = _repo(tmp_path, {"tests/test_base.py": RED})
    sha = _git(root, "rev-parse", "main").strip()

    first = _factory(tmp_path, root, FLAG, sha[:12])
    code, out = _retry(root, "greeter")

    assert (first.code, first.calls) == (1, 1), first.out
    assert HEADLINE not in out, out
    calls = (tmp_path / "engineer.calls").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, out
    assert code == 1, out
    records = _records(root)
    assert [r["acceptRedBase"] for r in records] == [sha[:12], sha[:12]], records
    assert [r["refused"] for r in records] == [False, False], records


def test_under_a_stack_an_acceptance_waives_a_failing_check(tmp_path: Path) -> None:
    """The #696 red base: a check exiting 101 that no kstrl parser reads."""
    root = stack_e2e._repo(tmp_path, stack_e2e._stack({"tests": stack_e2e.CARGO_RED}))
    sha = _git(root, "rev-parse", "main").strip()

    run = stack_e2e._factory(tmp_path, root, FLAG, sha[:12])

    assert HEADLINE not in run.out, run.out
    assert run.calls == 1, run.out
    (record,) = _records(root)
    assert record["refused"] is False, record
    assert any("stack:tests fails on main" in line for line in record["accepted"]), record


@pytest.mark.parametrize(
    ("checks", "setup", "reason"),
    [
        ({"tests": "true"}, "echo cannot-install >&2; exit 3", "the setup fails on main"),
        (
            {"build": "mkdir -p out && echo built > out/artifact.txt"},
            "",
            "git status after the checks on main",
        ),
        ({"tests": "exit 127"}, "", "stack:tests fails on main"),
    ],
    ids=["failed-setup", "untracked-output", "check-measured-nothing"],
)
def test_under_a_stack_an_acceptance_never_waives_what_was_not_measured_red(
    tmp_path: Path, checks: dict[str, str], setup: str, reason: str
) -> None:
    """A failed setup ran no check, a check whose command was not found
    (exit 127) measured nothing, and untracked output would be an
    out-of-scope edit in every engineer's diff: a matching acceptance still
    refuses all three, before any engineer call."""
    root = stack_e2e._repo(tmp_path, stack_e2e._stack(checks, setup=setup))
    sha = _git(root, "rev-parse", "main").strip()

    run = stack_e2e._factory(tmp_path, root, FLAG, sha[:12])

    assert run.code == 2, run.out
    assert run.calls == 0, run.out
    (record,) = _records(root)
    assert record["refused"] is True, record
    assert any(reason in line for line in record["reasons"]), record
    assert not any(reason in line for line in record["accepted"]), record
