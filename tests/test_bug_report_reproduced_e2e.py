"""#700, owner decision of 2026-10-09: a bug report that the designer cannot
reproduce must not run as verified.

A bug report (`ks serve` launches it for an issue with the `bug` label,
#654) runs `ks factory --design-acceptance --bug-report`. The base replay
removes a designed check that passes on the base it says fails on (#753).
When no check that the base kept fails on the base, nothing reproduces the
bug: the run is refused before the first engineer with exit 2, and a
halted_run inbox item names every check and its base result. Approving
that item runs the bug report without a reproduction, for that plan on
that base only. A run that is not a bug report keeps the behaviour of
``tests/test_acceptance_design_e2e.py::
test_a_designed_plan_whose_only_check_passes_on_the_base_has_no_designed_check``.

End to end: the real ``ks factory`` and ``ks inbox approve`` as
subprocesses on a real git repository, with the stub designer and engineer
of ``tests/test_acceptance_design_e2e.py``.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from kstrl.inbox import InboxItem
from tests.helpers.gitrepo import git_in
from tests.test_acceptance_design_e2e import (
    REFUSED_BASE,
    _base_record,
    _check,
    _design,
    _designed_plans,
    _entry,
    _greets,
)
from tests.test_acceptance_e2e import COMP, _greeting_repo, _manifest
from tests.test_acceptance_gate_e2e import _halts
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import FUSE_SECONDS, PY, _child_env, _spawn

NOT_REPRODUCED = "Refusing to run: the bug report was not reproduced on the base"
BUG_NEEDS_PLAN = (
    "--bug-report needs acceptance checks that can reproduce the bug: pass --acceptance or "
    "--design-acceptance. Nothing was run."
)


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _plan_id(root: Path) -> str:
    """The digest the run pinned for the designed plan, read from the base
    record of the one run so far."""
    plan_id: str = _base_record(root)["planId"]
    return plan_id


def _approve(root: Path, item: InboxItem) -> str:
    code, said = _spawn(
        ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain", "--no-color"],
        root,
        None,
    )
    assert code == 0, said
    return said


@runs_a_stack
def test_a_bug_report_no_kept_check_reproduces_is_refused_until_a_person_approves_it(
    tmp_path: Path,
) -> None:
    """The vacuous check is removed and the check that passes on the base as
    it says is kept, so no kept check fails on the base. The run exits 2
    with no engineer call and files one item that names both checks and
    their base results. An open item runs nothing. An approval runs the
    plan on that base only: once the base moves, the run is refused again
    and a second item is filed for the new base."""
    root = _greeting_repo(tmp_path)
    steady = {**_check("base-ok", ["true"]), "onBase": "passes"}
    reply = _entry([_check("vacuous", ["true"]), steady])
    base = _head(root)

    first = _design(tmp_path, root, [reply], "--bug-report")

    assert first.code == 2, first.out
    assert NOT_REPRODUCED in first.out, first.out
    assert REFUSED_BASE not in first.out, first.out
    rows = (
        f"{COMP}: the check vacuous (onBase: fails) passes on the base (exit 0, removed)",
        f"{COMP}: the check base-ok (onBase: passes) passes on the base (exit 0)",
    )
    assert all(row in first.out for row in rows), first.out
    assert first.engineer_calls == 0, first.out
    assert "acceptanceDigest" not in _manifest(root)
    (item,) = _halts(root)
    plan_id = _plan_id(root)
    assert item.evidence["unreproduced"] == {"planId": plan_id, "baseSha": base}, item.evidence
    assert all(row in item.detail for row in rows), item.detail

    second = _design(tmp_path, root, [reply], "--bug-report")

    assert second.code == 2, second.out
    assert NOT_REPRODUCED in second.out, second.out
    assert second.engineer_calls == 0, second.out
    assert [open_item.id for open_item in _halts(root)] == [item.id]
    said = _approve(root, item)
    assert "runs the bug report without a reproduction" in said, said
    (root / "NOTES.md").write_text("moved\n", encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "move the base")
    moved = _head(root)

    third = _design(tmp_path, root, [reply], "--bug-report")

    assert third.code == 2, third.out
    assert NOT_REPRODUCED in third.out, third.out
    assert third.engineer_calls == 0, third.out
    old, new = _halts(root)
    assert old.id == item.id, (old, new)
    assert new.evidence["unreproduced"] == {"planId": plan_id, "baseSha": moved}, new.evidence
    _approve(root, new)

    fourth = _design(tmp_path, root, [reply], "--bug-report")

    assert fourth.code == 0, fourth.out
    assert NOT_REPRODUCED not in fourth.out, fourth.out
    assert f"approval {new.id[:8]}" in fourth.out, fourth.out
    assert "runs it without a reproduction" in fourth.out, fourth.out
    assert fourth.engineer_calls == 1, fourth.out
    assert len(fourth.designer) == 1, fourth.out
    assert _manifest(root)["acceptanceDigest"] == plan_id


@runs_a_stack
def test_a_bug_report_whose_kept_check_fails_on_the_base_runs_with_no_item(
    tmp_path: Path,
) -> None:
    """The control: a check that fails on the base as it says reproduces
    the bug, so the run goes on to the engineer and files no item, though
    the vacuous check beside it is removed."""
    root = _greeting_repo(tmp_path)
    reply = _entry([_check("vacuous", ["true"]), _check("greets-ada", _greets("Ada"))])

    run = _design(tmp_path, root, [reply], "--bug-report")

    assert run.code == 0, run.out
    assert NOT_REPRODUCED not in run.out, run.out
    assert run.engineer_calls == 1, run.out
    assert _halts(root) == [], run.out


def test_a_bug_report_with_no_acceptance_plan_is_refused(tmp_path: Path) -> None:
    """--bug-report with neither --acceptance nor --design-acceptance has no
    check that could reproduce the bug: the run exits 2 before anything."""
    root = _greeting_repo(tmp_path)

    code, out = _spawn(
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", "false"),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
            "--bug-report",
        ],
        root,
        None,
    )

    assert code == 2, out
    assert BUG_NEEDS_PLAN in out, out


@runs_a_stack
def test_a_bug_report_whose_only_check_cannot_run_on_the_base_is_refused(
    tmp_path: Path,
) -> None:
    """A check that could not run on the base (exit 127, in a component the
    plan marks as creating the app) did not fail there, so it reproduces
    nothing: the run is refused with no engineer call and one item."""
    root = _greeting_repo(tmp_path)
    reply = _entry([_check("missing", ["kstrl-no-such-check-7c1d"])], creates_app=True)

    run = _design(tmp_path, root, [reply], "--bug-report")

    assert run.code == 2, run.out
    assert NOT_REPRODUCED in run.out, run.out
    row = f"{COMP}: the check missing (onBase: fails) could not run on the base (exit 127)"
    assert row in run.out, run.out
    assert run.engineer_calls == 0, run.out
    (item,) = _halts(root)
    assert row in item.detail, item.detail


@runs_a_stack
def test_an_approval_of_one_plan_does_not_run_another_plan_on_the_same_base(
    tmp_path: Path,
) -> None:
    """An approval holds for the plan it names. A new designed plan on the
    same base is refused again and files its own item."""
    root = _greeting_repo(tmp_path)
    base = _head(root)
    first = _design(tmp_path, root, [_entry([_check("vacuous", ["true"])])], "--bug-report")
    assert first.code == 2, first.out
    (item,) = _halts(root)
    _approve(root, item)
    (designed,) = _designed_plans(root)
    shutil.rmtree(designed)

    again = _design(tmp_path, root, [_entry([_check("other", ["true"])])], "--bug-report")

    assert again.code == 2, again.out
    assert NOT_REPRODUCED in again.out, again.out
    assert again.engineer_calls == 0, again.out
    old, new = _halts(root)
    assert old.id == item.id, (old, new)
    assert new.evidence["unreproduced"]["baseSha"] == base, new.evidence
    assert new.evidence["unreproduced"]["planId"] != item.evidence["unreproduced"]["planId"]


def _on_a_terminal(root: Path, args: list[str], answer: str, env: dict[str, str]) -> str:
    """The real CLI with a pseudo-terminal on stdin, typing ``answer``: the
    only way to confirm a [stack] while the inbox is disabled. Bounded by
    the fuse of ``_spawn``. Its process group is killed if it outlives it."""
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [PY, "-m", "kstrl", *args],
        cwd=root,
        env=_child_env(env),
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    os.close(slave)
    chunks: list[bytes] = []
    try:
        os.write(master, answer.encode("utf-8"))
        deadline = time.monotonic() + FUSE_SECONDS
        assert child.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"ks factory outlived its {FUSE_SECONDS}s fuse (hung)"
            ready, _, _ = select.select([child.stdout, master], [], [], remaining)
            if master in ready:
                try:
                    os.read(master, 4096)  # the terminal's echo; drained, not read
                except OSError:
                    pass
            if child.stdout in ready:
                data = os.read(child.stdout.fileno(), 4096)
                if not data:
                    break
                chunks.append(data)
        child.wait(timeout=FUSE_SECONDS)
    finally:
        os.close(master)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    return f"exit {child.returncode}\n" + b"".join(chunks).decode("utf-8", "replace")


@runs_a_stack
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
def test_a_bug_report_is_refused_when_no_approval_can_be_read(tmp_path: Path) -> None:
    """With the inbox disabled, the operator confirms the [stack] at the
    prompt, no item can be filed and no approval can be read. The run fails
    closed: it is refused before the engineer and says that it read no
    approval, and does not run as if a person approved it."""
    root = _greeting_repo(tmp_path)
    off = {"KSTRL_INBOX_ENABLED": "0"}
    reply = _entry([_check("vacuous", ["true"])])
    unasked = _design(tmp_path, root, [reply], "--bug-report", env=off)
    assert unasked.code == 2, unasked.out
    assert NOT_REPRODUCED not in unasked.out, unasked.out

    out = _on_a_terminal(
        root,
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(tmp_path / "agent.sh")),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color", "--no-prs"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--security-mode", "skip", "--contract-check", "skip"),
            *("--design-acceptance", "--bug-report"),
        ],
        "1\n",
        off,
    )

    assert out.startswith("exit 2\n"), out
    assert NOT_REPRODUCED in out, out
    assert "No approval was read: the inbox is disabled" in out, out
    assert not (tmp_path / "engineer.calls").exists(), out
