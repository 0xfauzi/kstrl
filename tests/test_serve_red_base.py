"""#654 slice 3b: `ks serve` on a base whose gates already fail.

Before this, the factory child's red-base refusal (exit 2, slice 1) reached
serve's exit-2 branch, which knew only two refusals by their printed text,
so it poisoned the item as UNCLASSIFIABLE and counted it toward the
consecutive-poison breaker. A red base is repo-wide: three items in a row
poisoned on it trip the breaker, and each one paid an architect first.

Owner decision 6 (2026-10-04): on a red base serve requeues the item and
pauses claims with ONE inbox item, outside the poison streak. Serve knows
the refusal by the record the child wrote, ``base-gates.json`` with
``refused: true`` in a run directory this launch owns, never by its
output. ``ks queue resume`` lifts the pause, and the journal names who.

End to end: `ks serve --once` in process through ``CliRunner``, spawning a
real `ks factory --spec` child against a temp git repository after the
real ``ks init``, with a confirmed ``[stack]`` whose one check is a
committed shell script, and a fake ``claude`` architect on PATH (the
harness of ``tests/test_stack_confirmation_e2e.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind
from kstrl.serve import SpendLedger
from kstrl.workqueue import ItemState, Queue, QueueConfig, QueueItem
from tests.helpers.gitrepo import git_in
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_isolation_rung import runs_a_stack
from tests.test_prompt_record import ONE_COMPONENT, _spec_project
from tests.test_queue_awaiting_answer import _scripted_claude
from tests.test_stack_e2e import _stack

#: A [stack] run needs nono on macOS; on Linux it runs on the host (#700).
pytestmark = [pytest.mark.usefixtures("no_open_prs"), runs_a_stack]

#: The base's one check: red prints a failure and exits 1, green exits 0.
RED = "echo 'check: FAILED'\nexit 1\n"
GREEN = "exit 0\n"

#: The operator `ks queue resume` records, from $USER.
OPERATOR = "owner-654"


def _project(tmp_path: Path, check: str, rung: dict[str, Any] | None = None) -> Path:
    """A repository `ks factory --spec` accepts, after the real ``ks init``,
    with a confirmed ``[stack]`` whose one check runs check.sh, committed."""
    root = _spec_project(tmp_path, initialised=True)
    with (root / "kstrl.toml").open("a", encoding="utf-8") as fh:
        fh.write("\n" + _stack({"tests": "sh check.sh"}, rung=rung))
    _commit(root, check)
    confirm_stack(root)
    return root


def _commit(root: Path, check: str) -> None:
    (root / "check.sh").write_text(check, encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "-m", "check.sh")


def _serve_once(root: Path) -> Result:
    return CliRunner().invoke(cli, ["serve", "--once", "--root", str(root), "--ui", "plain"])


def _red_base_items(root: Path) -> list[InboxItem]:
    box = Inbox(root, InboxConfig.load(root))
    return [i for i in box.items() if i.dedupe_key.startswith("serve-red-base:")]


def _readings(root: Path) -> list[dict[str, Any]]:
    """Every base-gates record under ``root``, oldest run first."""
    paths = sorted((root / ".kstrl" / "runs").glob("*/base-gates.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def _item(queue: Queue, queued: QueueItem) -> QueueItem:
    item = queue.get(queued.item_id)
    assert item is not None
    return item


def test_a_red_base_requeues_the_item_and_pauses_claims_until_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The red base requeues the item, leaves the poison streak where it was,
    pauses claims, and files one inbox item however many cycles see it.
    `ks queue resume` lifts the pause and the journal names the operator; a
    base still red pauses again and bumps the same item; once the base is
    green the item runs past the base reading."""
    root = _project(tmp_path, RED)
    calls = _scripted_claude(tmp_path, monkeypatch, [ONE_COMPONENT])
    # The plan gate parks the run that gets past the base, so nothing pays
    # an engineer: the plan gate runs after every pre-spend refusal.
    monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
    monkeypatch.setenv("USER", OPERATOR)
    ledger = SpendLedger(root)
    ledger.record_terminal(poisoned=True)
    queue = Queue(root, QueueConfig.load(root))
    queued = queue.add("# Spec\n\nBuild a thing.\n", title="red base", project_name="demo")

    refused = _serve_once(root)
    after_refusal = _item(queue, queued)
    streak_after_refusal = ledger.read_state().consecutive_poison
    paused = _serve_once(root)
    after_pause = _item(queue, queued)
    pause = queue.pause_state()
    architect_calls_while_paused = len(list(calls.iterdir()))

    resumed = CliRunner().invoke(cli, ["queue", "resume", "--root", str(root)])
    refused_again = _serve_once(root)
    after_second_refusal = _item(queue, queued)
    streak_after_second_refusal = ledger.read_state().consecutive_poison

    _commit(root, GREEN)
    resumed_green = CliRunner().invoke(cli, ["queue", "resume", "--root", str(root)])
    ran = _serve_once(root)
    after_green = _item(queue, queued)

    # Requeued, not poisoned, and the streak did not move.
    assert after_refusal.state is ItemState.QUEUED, refused.output
    assert after_refusal.attempts == 1, refused.output
    assert refused.exit_code == 1, refused.output
    assert streak_after_refusal == 1, refused.output
    assert "Queue paused: the base branch main at" in refused.output, refused.output
    # Claims paused: the second cycle claimed nothing. No architect was paid
    # at all: the base is measured before the architect (#696 slice 7).
    assert after_pause.state is ItemState.QUEUED, paused.output
    assert after_pause.attempts == 1, paused.output
    assert architect_calls_while_paused == 0, paused.output
    assert pause.paused, paused.output
    assert "fails its own gates" in pause.reason, pause.reason
    # A resume on a base still red: one more refusal, the same one item.
    assert resumed.exit_code == 0, resumed.output
    assert after_second_refusal.state is ItemState.QUEUED, refused_again.output
    assert after_second_refusal.attempts == 2, refused_again.output
    # `ks queue resume` cleared the streak (#707); the refusal left it at 0.
    assert streak_after_second_refusal == 0, refused_again.output
    (item,) = _red_base_items(root)
    assert item.kind is ItemKind.HALTED_RUN
    assert item.occurrences == 2
    # Green base, resumed: the item runs past the base reading to the plan gate.
    assert resumed_green.exit_code == 0, resumed_green.output
    assert after_green.state is ItemState.AWAITING_APPROVAL, ran.output
    assert after_green.attempts == 3, ran.output
    # Each refusal is recorded in the architect's run, where the base was
    # measured before the architect was paid; the green launch records its
    # one reading there and in the factory run that reused it (#696 slice 7).
    assert [r["refused"] for r in _readings(root)] == [True, True, False, False]
    # The journal names who paused and who resumed.
    rows = [
        r for r in queue.journal_entries() if r["to"] in ("paused", "running") and not r["item_id"]
    ]
    assert [(r["to"], r["actor"]) for r in rows] == [
        ("paused", "serve"),
        ("running", OPERATOR),
        ("paused", "serve"),
        ("running", OPERATOR),
    ], rows
    assert "base" in rows[0]["reason"], rows[0]


def test_a_refusal_that_is_not_a_red_base_is_still_poisoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same headline, a different refusal: the base is green but its
    reading cannot be recorded, because the check makes base-gates.json a
    directory in every run under the project. The child prints the red
    base's headline, "the base branch fails a gate Phase 1 runs, or its
    reading cannot be recorded", but no record says ``refused``, so serve
    classifies it as before: unclassifiable, poisoned, counted."""
    runs = tmp_path / "project" / ".kstrl" / "runs"
    blocker = f"for d in '{runs}'/*/; do mkdir -p \"$d\"base-gates.json; done\nexit 0\n"
    # The check runs inside the rung (#700), so it may write the run
    # directories only because the stack declares them writable.
    root = _project(tmp_path, blocker, rung={"writable": [".kstrl/runs"]})
    # An earlier launch's red-base record, left on disk. This launch does
    # not own it, so it is no evidence about this refusal.
    stale = runs / "factory-20260101-000000.000000-000000"
    stale.mkdir(parents=True)
    (stale / "base-gates.json").write_text(
        json.dumps(
            {"refused": True, "baseBranch": "main", "baseSha": "0" * 40, "reasons": ["stale"]}
        ),
        encoding="utf-8",
    )
    _scripted_claude(tmp_path, monkeypatch, [ONE_COMPONENT])
    ledger = SpendLedger(root)
    queue = Queue(root, QueueConfig.load(root))
    queued = queue.add("# Spec\n\nBuild a thing.\n", title="unrecorded", project_name="demo")

    result = _serve_once(root)
    item = _item(queue, queued)

    # The base is measured before the architect is paid, in the architect's
    # run (#696 slice 7), so that is where the check made the directory.
    assert [p.is_dir() for p in runs.glob("decompose-*/base-gates.json")] == [True]
    assert item.state is ItemState.POISON, result.output
    assert "refusing to guess which refusal it was" in item.poison_reason, item.poison_reason
    assert ledger.read_state().consecutive_poison == 1
    assert not queue.pause_state().paused
    assert _red_base_items(root) == []
