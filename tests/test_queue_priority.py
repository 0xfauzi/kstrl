"""`ks queue priority` reorders a queued item and records who changed it (#650).

Every test drives the real CLI. The serve cycle runs with a stand-in
factory, so nothing is spent. Each refused state is reached through the
real ``Queue`` methods, and the queue lock is held by a child process the
test starts and kills by its own process group, never found by name.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

import kstrl.cli as cli_mod
import kstrl.workqueue as workqueue_mod
from kstrl.cli import cli
from kstrl.serve import RunOutcome, RunSpend
from kstrl.workqueue import ItemState, Queue, QueueConfig, QueueItem, queue_root
from tests.helpers.procs import kill_group, wait_for_line

#: Nothing here is about flow control; the fixture's docstring in
#: tests/conftest.py says why the R10.7 bound has to be held open.
pytestmark = pytest.mark.usefixtures("no_open_prs")

ACTOR = "owner-x"

#: Bound for the lock holder's ready line, so a holder that never locks
#: fails as HUNG instead of hanging the test.
_HOLDER_TIMEOUT_SECONDS = 10.0

#: Bound for the command run against a held lock. A command that waits on
#: the lock instead of refusing fails here as HUNG, not as a stuck suite.
_COMMAND_TIMEOUT_SECONDS = 60.0

_HOLDER = """
import fcntl, sys, time
fp = open(sys.argv[1], "a+")
fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(120)
"""


@pytest.fixture(autouse=True)
def _no_spend_and_a_named_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kstrl.serve.read_run_spend", lambda root, run_id: RunSpend())
    monkeypatch.setenv("USER", ACTOR)


def _queue(root: Path) -> Queue:
    return Queue(root, QueueConfig())


def _invoke(args: list[str], root: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--no-color"])


def _add(root: Path, name: str, priority: int) -> QueueItem:
    spec = root / f"{name}.md"
    spec.write_text(f"# {name}\n\nDo {name}.\n", encoding="utf-8")
    result = _invoke(["queue", "add", str(spec), "--priority", str(priority)], root)
    assert result.exit_code == 0, result.output
    return next(item for item in _queue(root).items() if item.title == name)


def _snapshot(root: Path) -> dict[str, str]:
    """The sha256 of every meta.json and of the journal, keyed by path under the queue.

    The key carries the state directory, so an item that moved, or a
    second copy of one, changes the snapshot as surely as a changed byte.
    """
    base = queue_root(root)
    return {
        str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(base.rglob("*"))
        if path.is_file() and path.name in ("meta.json", "journal.jsonl")
    }


def _serve_once(root: Path) -> list[str]:
    """Run one real serve cycle with a stand-in factory; return the item ids it ran."""
    claimed: list[str] = []

    def runner(**kwargs: Any) -> RunOutcome:
        claimed.append(kwargs["spec_path"].parent.name)
        return RunOutcome(returncode=0)

    with patch("kstrl.serve.subprocess_factory_runner", runner):
        result = _invoke(["serve", "--once"], root)
    assert result.exit_code == 0, result.output
    return claimed


def test_a_raised_priority_is_claimed_first_and_shown_with_its_actor(tmp_path: Path) -> None:
    """The issue's acceptance: 0 and 5 queued, the first raised to 10, serve runs it."""
    first = _add(tmp_path, "a", 0)
    second = _add(tmp_path, "b", 5)

    result = _invoke(["queue", "priority", first.item_id, "--to", "10"], tmp_path)
    assert result.exit_code == 0, result.output
    assert first.item_id in result.output
    assert "priority 0 -> 10" in result.output

    assert _serve_once(tmp_path) == [first.item_id]
    states = {item.item_id: item.state for item in _queue(tmp_path).items()}
    assert states == {first.item_id: ItemState.DONE, second.item_id: ItemState.QUEUED}

    show = _invoke(["queue", "show", first.item_id], tmp_path)
    assert show.exit_code == 0, show.output
    assert f"queued -> queued  (priority 0 -> 10)  by {ACTOR}" in show.output
    rows = [
        entry
        for entry in _queue(tmp_path).journal_entries(first.item_id)
        if entry["from"] == "queued" and entry["to"] == "queued"
    ]
    assert [row["detail"] for row in rows] == [{"priority_from": 0, "priority_to": 10}]


def _lease(queue: Queue, item: QueueItem) -> None:
    queue.lease(item, actor="serve")


def _run(queue: Queue, item: QueueItem) -> QueueItem:
    return queue.start(queue.lease(item, actor="serve"), actor="serve")


def _finish_ok(queue: Queue, item: QueueItem) -> None:
    queue.finish_ok(_run(queue, item), actor="serve")


def _finish_failed(queue: Queue, item: QueueItem) -> None:
    queue.finish_failed(_run(queue, item), error="boom", actor="serve")


def _poison(queue: Queue, item: QueueItem) -> None:
    queue.poison(item, reason="refused", actor="serve")


def _park(queue: Queue, item: QueueItem) -> None:
    queue.await_approval(_run(queue, item), reason="merge gate", run_id="factory-x", actor="serve")


@pytest.mark.parametrize(
    ("move", "named"),
    [
        (_lease, "is leased"),
        (_run, "is running"),
        (_finish_ok, "is done"),
        (_finish_failed, "is failed"),
        (_poison, "is poison"),
        (_park, "is awaiting_approval"),
    ],
    ids=["leased", "running", "done", "failed", "poison", "awaiting_approval"],
)
def test_an_item_that_is_not_queued_is_refused_by_name_and_nothing_changes(
    tmp_path: Path, move: Any, named: str
) -> None:
    item = _add(tmp_path, "a", 0)
    move(_queue(tmp_path), item)
    before = _snapshot(tmp_path)

    result = _invoke(["queue", "priority", item.item_id, "--to", "10"], tmp_path)

    assert result.exit_code == 2, result.output
    assert named in result.output
    assert _snapshot(tmp_path) == before


def test_an_unknown_item_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    _add(tmp_path, "a", 0)
    before = _snapshot(tmp_path)

    result = _invoke(["queue", "priority", "q-nope", "--to", "10"], tmp_path)

    assert result.exit_code == 2, result.output
    assert "No queue item matching" in result.output
    assert _snapshot(tmp_path) == before


def test_an_item_serve_leases_after_the_lookup_is_refused_and_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serve wins the race between the command's lookup and its lock."""
    item = _add(tmp_path, "a", 0)
    real_resolve = cli_mod._resolve_queue_item
    after_lease: dict[str, str] = {}

    def resolve_then_lose_the_race(queue: Any, item_id: str, ui_impl: Any) -> Any:
        found = real_resolve(queue, item_id, ui_impl)
        # A separate copy, so the object the command holds stays "queued".
        serve_copy = _queue(tmp_path).get(found.item_id)
        assert serve_copy is not None
        _queue(tmp_path).lease(serve_copy, actor="serve")
        after_lease.update(_snapshot(tmp_path))
        return found

    monkeypatch.setattr(cli_mod, "_resolve_queue_item", resolve_then_lose_the_race)
    result = _invoke(["queue", "priority", item.item_id, "--to", "10"], tmp_path)

    assert result.exit_code == 2, result.output
    assert "is leased" in result.output
    assert _snapshot(tmp_path) == after_lease
    listing = _invoke(["queue", "ls"], tmp_path)
    assert listing.exit_code == 0, listing.output
    assert listing.output.count(item.item_id[:12]) == 1, listing.output


def _child_env() -> dict[str, str]:
    """The test's environment minus any kstrl setting an operator exported."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.update(USER=ACTOR, KSTRL_NO_TUI="1", KSTRL_AGENT_PROBE="0")
    return env


def test_a_held_queue_lock_refuses_at_once_and_nothing_changes(tmp_path: Path) -> None:
    item = _add(tmp_path, "a", 0)
    before = _snapshot(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(queue_root(tmp_path) / "queue.lock")],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        wait_for_line(holder, "locked", _HOLDER_TIMEOUT_SECONDS)
        command = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "kstrl",
                "queue",
                "priority",
                item.item_id,
                "--to",
                "10",
                "--root",
                str(tmp_path),
                "--no-color",
            ],
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = command.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_group(command.pid)
            command.wait(timeout=10)
            pytest.fail(
                f"HUNG: ks queue priority waited {_COMMAND_TIMEOUT_SECONDS}s on a held lock"
            )
    finally:
        kill_group(holder.pid)
        holder.wait(timeout=10)

    assert command.returncode == 2, output
    assert "locked" in output
    assert _snapshot(tmp_path) == before


def test_a_negative_priority_parses_and_runs_last(tmp_path: Path) -> None:
    first = _add(tmp_path, "a", 0)
    second = _add(tmp_path, "b", 0)

    result = _invoke(["queue", "priority", first.item_id, "--to", "-3"], tmp_path)

    assert result.exit_code == 0, result.output
    assert _serve_once(tmp_path) == [second.item_id]


def test_setting_the_current_priority_writes_nothing(tmp_path: Path) -> None:
    item = _add(tmp_path, "a", 5)
    before = _snapshot(tmp_path)

    result = _invoke(["queue", "priority", item.item_id, "--to", "5"], tmp_path)

    assert result.exit_code == 0, result.output
    assert "nothing changed" in result.output
    assert _snapshot(tmp_path) == before


def test_a_priority_that_cannot_be_written_is_refused_and_leaves_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A meta.json write that fails exits 2 and journals nothing, because the write comes first."""
    item = _add(tmp_path, "a", 0)
    before = _snapshot(tmp_path)
    real_write = workqueue_mod.atomic_write

    def refuse_meta(target: Path, content: str) -> None:
        if target.name == "meta.json":
            raise PermissionError(13, "Permission denied", str(target))
        real_write(target, content)

    monkeypatch.setattr(workqueue_mod, "atomic_write", refuse_meta)
    result = _invoke(["queue", "priority", item.item_id, "--to", "10"], tmp_path)

    assert result.exit_code == 2, result.output
    assert "Permission denied" in result.output
    assert _snapshot(tmp_path) == before


def test_the_prefix_queue_ls_prints_is_accepted_and_the_change_is_stamped_when_made(
    tmp_path: Path,
) -> None:
    """An operator types the 12-character id `ks queue ls` prints; the row carries its own time."""
    item = _add(tmp_path, "a", 0)
    prefix = item.item_id[:12]
    listing = _invoke(["queue", "ls"], tmp_path)
    assert prefix in listing.output, listing.output

    result = _invoke(["queue", "priority", prefix, "--to", "7"], tmp_path)

    assert result.exit_code == 0, result.output
    assert f"{item.item_id} (a) priority 0 -> 7" in result.output
    changed = _queue(tmp_path).get(item.item_id)
    assert changed is not None
    assert changed.priority == 7
    rows = _queue(tmp_path).journal_entries(item.item_id)
    added = next(row for row in rows if row["to"] == "queued" and not row.get("from"))
    moved = [row for row in rows if row["from"] == "queued" and row["to"] == "queued"]
    assert len(moved) == 1, rows
    assert datetime.fromisoformat(moved[0]["ts"]) > datetime.fromisoformat(added["ts"]), rows
    assert changed.updated_at == moved[0]["ts"]
