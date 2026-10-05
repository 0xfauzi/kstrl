"""#700 slice 3: ``[stack] up``, the clean replay of the recipe, and the stage it failed at.

``ks doctor --measure`` replays a ``[stack]``'s recipe in a throwaway
worktree of the base, inside both proven zones: setup, then ``up`` (ready
when it exits 0, never on a timeout), then every check, then the ``up``
process group is stopped by the id recorded when it started and the
worktree is removed. The record is the ``replay`` reading of the doctor
report and is filed on the stack's confirmation item; a stack whose
replay failed is never confirmed. Replays on one machine take turns.

End to end: the real ``ks doctor --measure``, ``ks inbox approve`` and
``ks factory`` as subprocesses on a real git repository after the real
``ks init`` (the harness of ``tests/test_stack_e2e.py``), with nono 0.79
or later on macOS (``needs_nono``); the boundary test uses a fake nono
and needs only macOS; the digest test needs neither.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kstrl.procgroup import read_group_liveness
from kstrl.stack import load_stack
from tests.helpers.procs import kill_group
from tests.test_isolation_rung import needs_nono, on_macos
from tests.test_isolation_stack import _ignoring_nono
from tests.test_stack_confirmation_e2e import NOT_CONFIRMED, _restack, _stack_items
from tests.test_stack_e2e import (
    FUSE_SECONDS,
    PY,
    _child_env,
    _factory,
    _measure,
    _repo,
    _spawn,
    _stack,
)


def _replay(document: dict[str, Any]) -> dict[str, Any]:
    reading: dict[str, Any] = document["replay"]
    return reading


def _replay_row(document: dict[str, Any]) -> dict[str, Any]:
    (row,) = [row for row in document["checks"] if row["name"] == "replay"]
    return row


def _stage(reading: dict[str, Any], name: str) -> dict[str, Any]:
    (stage,) = [stage for stage in reading["stages"] if stage["name"] == name]
    return stage


def _no_replay_worktree(root: Path) -> bool:
    return sorted((root / ".kstrl" / "contract").glob("replay-*")) == []


@needs_nono
def test_an_up_that_exits_1_fails_the_replay_with_its_log_tail_and_is_never_confirmed(
    tmp_path: Path,
) -> None:
    """``up`` writes the operator's home directory, which the test zone
    refuses, then exits 1. The replay fails at ``up_failed:1`` with the
    last lines ``up`` printed, nothing reached $HOME, and the record goes
    on the confirmation item. A later replay below a refused rung ran
    nothing, so it does not lift that failure: approving the item confirms
    nothing, and ``ks factory`` still refuses, naming the stage."""
    escape = Path.home() / f"kstrl-escape-up-{secrets.token_hex(6)}"
    up = f'echo up-started; echo x > "$HOME/{escape.name}"; echo up-continued; exit 1'
    root = _repo(tmp_path, _stack({"tests": "true"}, rung={"up": up}), confirm=False)

    try:
        code, document = _measure(root)
        escaped = escape.exists()
    finally:
        escape.unlink(missing_ok=True)

    assert not escaped, "up wrote the operator's home directory"
    assert code == 1, document
    reading = _replay(document)
    assert reading["failed"] == "up_failed:1", reading
    up_stage = _stage(reading, "up")
    assert up_stage["exit"] == 1, up_stage
    assert "up-started" in up_stage["tail"] and "up-continued" in up_stage["tail"], up_stage
    assert [stage["name"] for stage in reading["stages"]] == ["up"], reading
    row = _replay_row(document)
    assert row["status"] == "fail", row
    assert row["detail"].startswith("up_failed:1: up `"), row
    assert "up-continued" in row["detail"], row
    assert _no_replay_worktree(root)
    (item,) = _stack_items(root)
    assert item.evidence["replay"]["failed"] == "up_failed:1", item.evidence

    _refused_code, refused = _measure(root, _ignoring_nono(tmp_path))
    assert _replay(refused)["failed"] == "boundary_refused", refused
    approved_code, approved = _spawn(
        ["inbox", "approve", item.id[:8], "--root", str(root)], root, None
    )
    run = _factory(tmp_path, root)

    assert approved_code == 0, approved
    assert run.code == 2, run.out
    assert NOT_CONFIRMED in run.out, run.out
    assert "failed its clean replay at up_failed:1" in run.out, run.out
    assert run.calls == 0, run.out


@needs_nono
def test_a_recipe_that_needs_an_untracked_file_fails_the_clean_replay(tmp_path: Path) -> None:
    """The setup reads a file the operator's checkout holds and the base
    does not. The replay runs in a throwaway worktree of the base, so the
    setup fails there; the operator's checkout is untouched and the
    replay's worktree is gone."""
    root = _repo(tmp_path, _stack({"tests": "true"}, setup="test -f local.env"))
    (root / "local.env").write_text("TOKEN=only-here\n", encoding="utf-8")

    code, document = _measure(root)

    assert code == 1, document
    reading = _replay(document)
    assert reading["failed"] == "setup_failed:1", reading
    assert [stage["name"] for stage in reading["stages"]] == ["setup"], reading
    assert (root / "local.env").exists()
    assert _no_replay_worktree(root)


def test_editing_up_changes_the_digest_and_needs_confirmation_again(tmp_path: Path) -> None:
    """``up`` is part of what the stack is: a confirmed stack whose ``up``
    is edited refuses until the new text is confirmed, and an empty ``up``
    is the same stack as none."""
    root = _repo(tmp_path, _stack({"tests": "true"}, rung={"up": "true"}))
    _restack(root, _stack({"tests": "true"}, rung={"up": "exit 0"}))

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert NOT_CONFIRMED in run.out, run.out
    assert "has changed since" in run.out, run.out
    assert run.calls == 0, run.out
    none = _repo(tmp_path / "none", _stack({"tests": "true"}))
    empty = _repo(tmp_path / "empty", _stack({"tests": "true"}, rung={"up": ""}))
    digests = [stack.digest for stack in map(load_stack, (none, empty)) if stack is not None]
    assert len(digests) == 2 and digests[0] == digests[1], digests


@needs_nono
def test_an_up_that_never_becomes_ready_is_up_timeout_and_its_group_is_gone(
    tmp_path: Path,
) -> None:
    """``up`` starts a server in the background and never exits. Under a 3 s
    limit the replay fails at ``up_timeout``, runs no check, and the group
    whose id it recorded when ``up`` started is empty afterwards."""
    # Every process of the group leaves the worktree first, so only the group
    # stop can end it: removing the worktree also kills the group of any
    # process whose cwd is still inside it (`worktree_sweep`).
    up = 'cd / && { sleep 300 & echo "server $!"; exec sleep 300; }'
    check_ran = "check.ran"
    root = _repo(tmp_path, _stack({"tests": f"touch {check_ran}"}, rung={"up": up}), confirm=False)

    code, document = _measure(root, {"KSTRL_TIMEOUT_VERIFY": "3"})
    reading = _replay(document)
    pgid = reading["pgid"]
    try:
        live = read_group_liveness(pgid).live if isinstance(pgid, int) else None
    finally:
        if isinstance(pgid, int) and pgid > 1:
            kill_group(pgid)

    assert code == 1, document
    assert reading["failed"] == "up_timeout", reading
    assert [stage["name"] for stage in reading["stages"]] == ["up"], reading
    assert any(line.startswith("server ") for line in _stage(reading, "up")["tail"]), reading
    assert isinstance(pgid, int) and pgid > 1, reading
    assert live is False, f"the up group {pgid} is still running"
    assert reading["group_gone"] is True, reading
    assert _no_replay_worktree(root)


@needs_nono
def test_a_ready_up_stays_up_through_the_checks_and_its_group_is_stopped_after(
    tmp_path: Path,
) -> None:
    """``up`` starts a server that ignores SIGTERM, outside the worktree, and
    exits 0: ready. The check passes only while the server still runs, and
    after the replay the group whose id was recorded when ``up`` started is
    empty, so the stop happens on a pass too and goes on to SIGKILL."""
    # Out of the worktree (`cd /`) so only the group stop can end it, not the
    # worktree sweep; `trap "" TERM` is inherited by the background loop.
    up = (
        'here="$PWD"; trap "" TERM; cd / && '
        '{ while :; do touch "$here/alive"; sleep 0.1; done & echo "server $!"; }; exit 0'
    )
    root = _repo(
        tmp_path,
        _stack({"tests": "rm -f alive; sleep 1; test -f alive"}, rung={"up": up}),
        confirm=False,  # the base gates run none of it
    )

    _code, document = _measure(root)
    reading = _replay(document)
    pgid = reading["pgid"]
    try:
        live = read_group_liveness(pgid).live if isinstance(pgid, int) else None
    finally:
        if isinstance(pgid, int) and pgid > 1:
            kill_group(pgid)

    assert reading["failed"] == "", reading
    assert [stage["name"] for stage in reading["stages"]] == ["up", "check:tests"], reading
    assert _stage(reading, "up")["exit"] == 0, reading
    assert any(line.startswith("server ") for line in _stage(reading, "up")["tail"]), reading
    assert isinstance(pgid, int) and pgid > 1, reading
    assert live is False, f"the up group {pgid} is still running"
    assert reading["group_gone"] is True, reading
    assert _replay_row(document)["status"] == "ok", document
    assert _no_replay_worktree(root)


@needs_nono
def test_two_replays_on_one_machine_take_turns(tmp_path: Path) -> None:
    """Two projects declare one shared directory writable. Each setup takes
    a marker there with ``mkdir`` (which fails when the marker exists) and
    the check gives it back once the test says so. The first replay holds
    the marker; the second, started then, waits for the lock instead of
    failing its setup, and both replays pass."""
    shared = tmp_path / "shared"
    shared.mkdir()
    held, go = shared / "held", shared / "go"
    table = _stack(
        {"hold": f'while [ ! -e "{go}" ]; do sleep 0.1; done; rmdir "{held}"'},
        setup=f'mkdir "{held}"',
        rung={"writable": [str(shared)]},
    )
    # Unconfirmed: the base gates run none of its commands, only the replay does.
    first = _repo(tmp_path / "a", table, confirm=False)
    second = _repo(tmp_path / "b", table, confirm=False)
    logs = [tmp_path / "first.out", tmp_path / "second.out"]
    children: list[subprocess.Popen[bytes]] = []
    try:
        children.append(_doctor_in_background(first, logs[0]))
        _wait_until(lambda: held.exists(), children[0], logs[0], "the first replay's setup")
        children.append(_doctor_in_background(second, logs[1]))
        _wait_until(
            lambda: "Waiting for another replay" in logs[1].read_text(encoding="utf-8"),
            children[1],
            logs[1],
            "the second replay waiting on the lock",
        )
        go.touch()
        for child in children:
            child.wait(timeout=FUSE_SECONDS)
    finally:
        for child in children:
            if child.poll() is None:
                kill_group(child.pid)
                child.wait()

    readings = [_replay(_document(log)) for log in logs]
    assert [reading["failed"] for reading in readings] == ["", ""], readings
    assert readings[1]["lock_wait_seconds"] > 0, readings
    assert not held.exists()


@on_macos
def test_below_a_proven_rung_the_replay_runs_nothing(tmp_path: Path) -> None:
    """A nono that ignores its policy lets the canaries out, so the rung is
    refused: the replay is ``boundary_refused``, which warns, and neither
    its setup nor ``up`` ran."""
    marker = tmp_path / "ran"
    table = _stack({"tests": "true"}, setup=f"touch '{marker}'", rung={"up": f"touch '{marker}'"})
    root = _repo(tmp_path, table, confirm=False)  # the base gates run none of it

    _code, document = _measure(root, _ignoring_nono(tmp_path))

    reading = _replay(document)
    assert reading["failed"] == "boundary_refused", reading
    assert "write_outside (escaped)" in reading["detail"], reading
    assert reading["stages"] == [], reading
    assert _replay_row(document)["status"] == "warn", document
    assert not marker.exists()


def _doctor_in_background(root: Path, log: Path) -> subprocess.Popen[bytes]:
    """The real `ks doctor --measure --json` in its own process group, its
    output in ``log``."""
    with log.open("wb") as handle:
        return subprocess.Popen(
            [PY, "-m", "kstrl", "doctor", "--root", str(root), "--measure", "--json"],
            cwd=root,
            env=_child_env(None),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _wait_until(
    condition: Callable[[], bool], child: subprocess.Popen[bytes], log: Path, what: str
) -> None:
    """Poll ``condition`` under the fuse; fail naming ``what`` when the child
    exits first or the fuse runs out."""
    deadline = time.monotonic() + FUSE_SECONDS
    while not condition():
        output = log.read_text(encoding="utf-8")
        assert child.poll() is None, f"exited before {what}:\n{output}"
        assert time.monotonic() < deadline, f"hung waiting for {what}:\n{output}"
        time.sleep(0.1)


def _document(log: Path) -> dict[str, Any]:
    out = log.read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(out[out.index("{") :])
    return document
