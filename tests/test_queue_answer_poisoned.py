"""#644 slice 2: `ks queue answer` accepts a poisoned item the architect escalated.

When the spec-issues write fails, `ks factory` still exits 2 on an
architect escalation but never prints "Spec issues written to:", the
marker `ks serve` reads, so serve poisons the item. The escalation row the
child filed before the halt still names the item in
``evidence["queue_item"]``. Owner decision 2(c): `ks queue answer` accepts
a poisoned item exactly when an undecided spec_escalation row names it,
and refuses every other poisoned item with exit 2 and the reason. An inbox
that cannot be read in full is a refusal, never an empty read.

Every test drives `ks serve --once` spawning a real `ks factory --spec`
child, with the fake ``claude`` of tests/test_queue_awaiting_answer.py on
PATH, then the real `ks queue answer`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kstrl.inbox import Inbox, InboxItem, ItemKind, ItemStatus
from kstrl.workqueue import ItemState, QueueItem, short_item_id
from tests.test_prompt_record import ONE_COMPONENT, _spec_project
from tests.test_queue_awaiting_answer import (
    ANSWER_LINE,
    SPEC_TEXT,
    _add,
    _call_stdin,
    _item,
    _journal,
    _ks,
    _queue,
    _rows,
    _scripted_claude,
    _streak,
)
from tests.test_serve_architect_spend import BLOCKER

pytestmark = pytest.mark.usefixtures("no_open_prs")

#: Who answers. `ks queue answer` journals ``$USER`` as the actor.
OWNER = "the-owner"

#: The refusal of a poisoned item no escalation row names, after its id.
NO_ROW = "is poison and no undecided spec_escalation row in the inbox names it"


def _escalations(root: Path) -> list[InboxItem]:
    return [row for row in _rows(root) if row.kind is ItemKind.SPEC_ESCALATION]


def _answer_file(tmp_path: Path) -> Path:
    path = tmp_path / "answered.md"
    path.write_text(SPEC_TEXT + "\n" + ANSWER_LINE + "\n", encoding="utf-8")
    return path


def _snapshot(root: Path, item: QueueItem) -> tuple[bytes, bytes]:
    queue = _queue(root)
    current = _item(root, item.item_id)
    return (
        (queue.item_dir(current) / "meta.json").read_bytes(),
        queue.spec_path(current).read_bytes(),
    )


def _poisoned_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, QueueItem, InboxItem]:
    """One `ks serve --once` whose architect escalates while the spec-issues
    write fails: the item is poisoned and the escalation row names it.

    ``scripts/kstrl/spec-issues.json`` is a directory, so the atomic write
    of the audit raises ``OSError`` and the factory exits 2 without the
    marker. Returns the project root, the fake architect's calls directory,
    the item and the row."""
    root = _spec_project(tmp_path)
    calls = _scripted_claude(tmp_path, monkeypatch, [BLOCKER, ONE_COMPONENT])
    (root / "scripts" / "kstrl" / "spec-issues.json").mkdir(parents=True)
    item = _add(root, "escalates")

    served = _ks(root, "serve", "--once")

    assert served.exit_code == 1, served.output
    assert "Spec issues written to:" not in served.output, served.output
    assert _item(root, item.item_id).state is ItemState.POISON, served.output
    assert _streak(root) == 1
    (row,) = _escalations(root)
    assert row.status is ItemStatus.OPEN, row.status
    assert row.evidence.get("queue_item") == item.item_id, row.evidence
    return root, calls, item, row


def _refused(root: Path, item: QueueItem, answer: Path, reason: str) -> None:
    """`ks queue answer` exits 2 naming ``reason`` and writes nothing."""
    before = _snapshot(root, item)

    result = _ks(root, "queue", "answer", item.item_id, str(answer))

    assert result.exit_code == 2, result.output
    assert reason in " ".join(result.output.split()), result.output
    assert _snapshot(root, item) == before
    assert _item(root, item.item_id).state is ItemState.POISON


class TestAnEscalatedPoisonIsAnswered:
    def test_the_answered_item_is_requeued_and_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, calls, item, row = _poisoned_escalation(tmp_path, monkeypatch)
        (root / "scripts" / "kstrl" / "spec-issues.json").rmdir()
        answered = _answer_file(tmp_path)
        monkeypatch.setenv("USER", OWNER)

        result = _ks(root, "queue", "answer", short_item_id(item.item_id), str(answered))

        assert result.exit_code == 0, result.output
        requeued = _item(root, item.item_id)
        assert requeued.state is ItemState.QUEUED
        assert requeued.poison_reason == "", requeued.poison_reason
        (entry,) = _journal(root, item.item_id, "queued")[-1:]
        assert (entry["from"], entry["reason"], entry["actor"]) == ("poison", "answered", OWNER)
        detail = entry["detail"]
        assert isinstance(detail, dict)
        assert detail["escalation_row"] == row.id, detail
        assert detail["escalated_run"] == row.run_id != "", detail
        assert detail["spec_sha256_after"] == hashlib.sha256(answered.read_bytes()).hexdigest()
        assert _streak(root) == 1, "answering leaves the poison streak, as `ks queue retry` does"

        _ks(root, "serve", "--once")

        assert ANSWER_LINE in _call_stdin(calls, 2), "the architect never read the answer"
        (resolved,) = _escalations(root)
        assert resolved.status is ItemStatus.RESOLVED, (resolved.status, resolved.detail)

        # The answered run's engineer fails, so the item is poisoned again,
        # and the row it resolved admits no second answer.
        assert _item(root, item.item_id).state is ItemState.POISON
        _refused(root, item, answered, f"{item.item_id} {NO_ROW}")


class TestEveryOtherPoisonIsRefused:
    def test_an_engineer_failure_poison_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _spec_project(tmp_path, initialised=True)
        _scripted_claude(tmp_path, monkeypatch, [ONE_COMPONENT])
        item = _add(root, "engineer fails")
        served = _ks(root, "serve", "--once")
        assert _item(root, item.item_id).state is ItemState.POISON, served.output
        assert _escalations(root) == []

        _refused(
            root,
            item,
            _answer_file(tmp_path),
            f"{item.item_id} {NO_ROW}",
        )

    def test_a_row_naming_another_item_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _spec_project(tmp_path, initialised=True)
        _scripted_claude(tmp_path, monkeypatch, [BLOCKER, ONE_COMPONENT])
        escalated = _add(root, "escalates")
        failed = _add(root, "engineer fails")
        for _cycle in range(2):
            _ks(root, "serve", "--once")
        assert _item(root, escalated.item_id).state is ItemState.AWAITING_ANSWER
        assert _item(root, failed.item_id).state is ItemState.POISON
        (row,) = _escalations(root)
        assert row.status is ItemStatus.OPEN
        assert row.evidence.get("queue_item") == escalated.item_id, row.evidence

        _refused(
            root,
            failed,
            _answer_file(tmp_path),
            f"{failed.item_id} {NO_ROW}",
        )

    @pytest.mark.parametrize(
        ("damage", "reason"),
        [
            (b"\xff\xfe not utf-8\n", "could not be read"),
            (b'{"id": "torn', "could not be parsed"),
        ],
        ids=["undecodable", "torn-line"],
    )
    def test_an_inbox_that_cannot_be_read_in_full_is_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: bytes, reason: str
    ) -> None:
        root, _calls, item, _row = _poisoned_escalation(tmp_path, monkeypatch)
        with Inbox(root).path.open("ab") as handle:
            handle.write(damage)

        _refused(root, item, _answer_file(tmp_path), reason)
