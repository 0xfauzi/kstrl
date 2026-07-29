"""R8.6 PR 1: work-queue substrate regression tests.

The tests that matter most here are not the CRUD ones. They are the
money-safety invariants, because this substrate is what an unattended
`ks serve` will drive:

- an attempt is charged BEFORE the rename into ``running/``, so an
  interrupted transition over-counts rather than under-counts (an
  uncounted attempt is an unbounded retry loop, at a measured
  $1.70-2.60 per first attempt and $3.99-7.42 per retry);
- the item DIRECTORY is authoritative, so a crash between the sidecar
  write and the rename cannot make state ambiguous;
- corrupt metadata falls back to the GATED value, never to auto-merge;
- an unreadable pause marker reads as PAUSED, never as running.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.workqueue import (
    ItemSource,
    ItemState,
    MergeDisposition,
    PauseState,
    Queue,
    QueueConfig,
    QueueError,
    QueueItem,
    QueueLockedError,
    mint_item_id,
    queue_lock,
    summarize,
)


def _queue(root: Path, **config: object) -> Queue:
    return Queue(root, QueueConfig(**config))  # type: ignore[arg-type]


def _add(queue: Queue, text: str = "# Spec\n\nBuild a thing.\n", **kwargs: object) -> QueueItem:
    return queue.add(text, **kwargs)  # type: ignore[arg-type]


class TestAdd:
    def test_add_text_lands_in_queued_with_spec_and_meta(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue, title="build a thing")

        item_dir = tmp_path / ".kstrl" / "queue" / "queued" / item.item_id
        assert item_dir.is_dir()
        assert (item_dir / "spec.md").read_text() == "# Spec\n\nBuild a thing.\n"
        meta = json.loads((item_dir / "meta.json").read_text())
        assert meta["item_id"] == item.item_id
        assert meta["state"] == "queued"
        assert meta["title"] == "build a thing"

    def test_add_from_path_copies_the_spec(self, tmp_path: Path) -> None:
        """A spec edited after enqueue must not change what runs."""
        source = tmp_path / "feature-x.md"
        source.write_text("original\n")
        queue = _queue(tmp_path)
        item = _add(queue, source)  # type: ignore[arg-type]

        source.write_text("TAMPERED\n")
        assert queue.read_spec(queue.items()[0]) == "original\n"
        assert item.spec_filename == "feature-x.md"
        assert item.title == "feature-x"

    def test_add_refuses_an_empty_spec(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        with pytest.raises(QueueError, match="empty spec"):
            _add(queue, "   \n\n")

    def test_add_defaults_to_stop_at_pr(self, tmp_path: Path) -> None:
        """Continuous intake must not silently delete the merge gate."""
        queue = _queue(tmp_path)
        item = _add(queue)
        assert item.merge_disposition is MergeDisposition.STOP_AT_PR

    def test_add_inherits_max_attempts_from_config(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, max_attempts=7)
        assert _add(queue).max_attempts == 7

    def test_add_leaves_no_staging_dir_when_the_write_fails(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        with patch(
            "kstrl.workqueue._atomic_write", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                _add(queue)
        queued = tmp_path / ".kstrl" / "queue" / "queued"
        assert list(queued.iterdir()) == []

    def test_item_ids_sort_chronologically(self) -> None:
        ids = [mint_item_id() for _ in range(5)]
        assert ids == sorted(ids)


class TestOrdering:
    def test_fifo_within_a_priority_band(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        first = _add(queue, title="first")
        second = _add(queue, title="second")
        assert [i.item_id for i in queue.items()] == [first.item_id, second.item_id]

    def test_higher_priority_runs_first(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, title="normal")
        urgent = _add(queue, title="urgent", priority=5)
        assert queue.items()[0].item_id == urgent.item_id
        assert queue.next_ready() is not None
        ready = queue.next_ready()
        assert ready is not None and ready.item_id == urgent.item_id


class TestLookup:
    def test_get_by_prefix(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        found = queue.get(item.item_id[:10])
        assert found is not None and found.item_id == item.item_id

    def test_ambiguous_prefix_raises_instead_of_guessing(
        self, tmp_path: Path,
    ) -> None:
        """Operating on the wrong unit of work is worse than a retype."""
        queue = _queue(tmp_path)
        _add(queue)
        _add(queue)
        with pytest.raises(QueueError, match="matches multiple items"):
            queue.get("q-")

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        assert _queue(tmp_path).get("nope") is None

    def test_find_by_source_ref(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue, source=ItemSource.GITHUB, source_ref="0xfauzi/kstrl#153")
        assert queue.find_by_source_ref("0xfauzi/kstrl#153") is not None
        assert queue.find_by_source_ref("0xfauzi/kstrl#999") is None
        assert queue.find_by_source_ref("") is None


class TestTransitions:
    def test_full_happy_path(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        item = queue.lease(item)
        assert item.state is ItemState.LEASED
        item = queue.start(item, run_id="factory-1")
        assert item.state is ItemState.RUNNING
        item = queue.finish_ok(item)
        assert item.state is ItemState.DONE
        assert (tmp_path / ".kstrl" / "queue" / "done" / item.item_id).is_dir()
        assert not (tmp_path / ".kstrl" / "queue" / "queued" / item.item_id).exists()

    def test_illegal_transition_raises(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        with pytest.raises(QueueError, match="illegal queue transition"):
            queue.transition(item, ItemState.DONE)

    def test_done_is_terminal(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.finish_ok(queue.start(queue.lease(_add(queue))))
        with pytest.raises(QueueError, match="illegal queue transition"):
            queue.requeue(item)

    def test_transition_rejects_unknown_fields(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        with pytest.raises(QueueError, match="unknown QueueItem field"):
            queue.transition(item, ItemState.LEASED, nonsense=1)

    def test_transition_refuses_when_the_source_dir_is_gone(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        for child in queue.item_dir(item).iterdir():
            child.unlink()
        queue.item_dir(item).rmdir()
        with pytest.raises(QueueError, match="is not in queued"):
            queue.lease(item)

    def test_spec_travels_with_the_item(self, tmp_path: Path) -> None:
        """One rename moves spec and sidecar together."""
        queue = _queue(tmp_path)
        item = queue.start(queue.lease(_add(queue, "# payload\n")))
        assert queue.read_spec(item) == "# payload\n"


class TestAttemptCharging:
    """The single most expensive thing to get wrong (R8.6 safety)."""

    def test_start_charges_the_attempt(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        assert item.attempts == 0
        item = queue.lease(item)
        assert item.attempts == 0, "leasing spends nothing"
        item = queue.start(item)
        assert item.attempts == 1

    def test_attempt_is_durable_before_the_rename_commits(
        self, tmp_path: Path,
    ) -> None:
        """A crash in the commit window must not lose the charge.

        Simulates the process dying between the sidecar write and the
        rename. The item stays in ``leased/`` (the directory is
        authoritative) but the attempt is already recorded on disk, so a
        reaper cannot hand out an uncounted retry.
        """
        queue = _queue(tmp_path)
        item = _add(queue)
        item = queue.lease(item)

        real_replace = os.replace

        def die_on_directory_move(src: object, dst: object) -> None:
            # Only the item-directory rename fails. The sidecar's own
            # atomic commit must still succeed, or the test would prove
            # nothing about the window BETWEEN the two.
            if Path(str(src)).is_dir():
                raise OSError("crash mid-transition")
            real_replace(src, dst)  # type: ignore[arg-type]

        with patch("kstrl.workqueue.os.replace", side_effect=die_on_directory_move):
            with pytest.raises(OSError):
                queue.start(item)

        on_disk = json.loads(
            (
                tmp_path / ".kstrl" / "queue" / "leased" / item.item_id / "meta.json"
            ).read_text()
        )
        assert on_disk["attempts"] == 1, "the attempt must survive the crash"

        reread = queue.items()[0]
        assert reread.state is ItemState.LEASED, "the directory is the truth"
        assert reread.attempts == 1

    def test_a_failed_move_leaves_the_item_object_honest(
        self, tmp_path: Path,
    ) -> None:
        """An uncommitted move must not leave the object claiming success."""
        queue = _queue(tmp_path)
        item = queue.lease(_add(queue))
        real_replace = os.replace

        def die_on_directory_move(src: object, dst: object) -> None:
            if Path(str(src)).is_dir():
                raise OSError("crash mid-transition")
            real_replace(src, dst)  # type: ignore[arg-type]

        with patch("kstrl.workqueue.os.replace", side_effect=die_on_directory_move):
            with pytest.raises(OSError):
                queue.start(item)

        assert item.state is ItemState.LEASED
        assert item.attempts == 1, "the charge stays; over-counting is safe"

    def test_retries_are_bounded_by_max_attempts(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, max_attempts=2)
        item = _add(queue)
        for _ in range(2):
            item = queue.start(queue.lease(item))
            item = queue.finish_failed(item, error="infra")
            if item.attempts_remaining:
                item = queue.requeue(item)
        assert item.attempts == 2
        assert item.attempts_remaining == 0

    def test_requeue_does_not_reset_attempts_by_default(
        self, tmp_path: Path,
    ) -> None:
        """A retry policy that can zero its own bound is not a bound."""
        queue = _queue(tmp_path)
        item = queue.finish_failed(queue.start(queue.lease(_add(queue))))
        item = queue.requeue(item)
        assert item.attempts == 1

    def test_reset_attempts_is_explicit_and_clears_poison(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = queue.start(queue.lease(_add(queue)))
        item = queue.poison(item, reason="spec is ambiguous")
        item = queue.requeue(item, reset_attempts=True)
        assert item.attempts == 0
        assert item.poison_reason == ""


class TestDirectoryIsAuthoritative:
    def test_state_comes_from_the_directory_not_the_sidecar(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        meta_path = tmp_path / ".kstrl" / "queue" / "queued" / item.item_id / "meta.json"
        data = json.loads(meta_path.read_text())
        data["state"] = "done"
        meta_path.write_text(json.dumps(data))

        assert queue.items()[0].state is ItemState.QUEUED

    def test_stale_mirror_self_heals_on_the_next_write(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        meta_path = queue.item_dir(item) / "meta.json"
        data = json.loads(meta_path.read_text())
        data["state"] = "poison"
        meta_path.write_text(json.dumps(data))

        moved = queue.lease(queue.items()[0])
        landed = json.loads((queue.item_dir(moved) / "meta.json").read_text())
        assert landed["state"] == "leased"


class TestCorruptionHandling:
    def test_malformed_meta_is_skipped_with_a_warning(
        self, tmp_path: Path,
    ) -> None:
        """A dropped item is lost work; say so rather than swallowing it."""
        queue = _queue(tmp_path)
        good = _add(queue, title="good")
        broken_dir = tmp_path / ".kstrl" / "queue" / "queued" / "q-broken"
        broken_dir.mkdir(parents=True)
        (broken_dir / "meta.json").write_text("{not json")

        with pytest.warns(RuntimeWarning, match="rejected item"):
            items = queue.items()
        assert [i.item_id for i in items] == [good.item_id]

    def test_missing_meta_is_skipped(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        orphan = tmp_path / ".kstrl" / "queue" / "queued" / "q-orphan"
        orphan.mkdir(parents=True)
        with pytest.warns(RuntimeWarning, match="unreadable meta.json"):
            assert queue.items() == []

    def test_meta_without_identity_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        bad = tmp_path / ".kstrl" / "queue" / "queued" / "q-nameless"
        bad.mkdir(parents=True)
        (bad / "meta.json").write_text(json.dumps({"title": "x"}))
        with pytest.warns(RuntimeWarning, match="missing item_id"):
            assert queue.items() == []

    def test_corrupt_merge_disposition_falls_back_to_gated(self) -> None:
        """A corrupt field must never GRANT a permission."""
        item = QueueItem.from_dict({
            "item_id": "q-1", "spec_filename": "spec.md",
            "merge_disposition": "yolo-merge-everything",
        })
        assert item is not None
        assert item.merge_disposition is MergeDisposition.STOP_AT_PR

    def test_unknown_state_decodes_to_queued(self) -> None:
        item = QueueItem.from_dict({
            "item_id": "q-1", "spec_filename": "spec.md", "state": "wat",
        })
        assert item is not None and item.state is ItemState.QUEUED

    def test_non_integer_priority_falls_back(self) -> None:
        item = QueueItem.from_dict({
            "item_id": "q-1", "spec_filename": "spec.md", "priority": "high",
        })
        assert item is not None and item.priority == 0

    def test_legacy_payload_without_new_fields_decodes(self) -> None:
        """Sidecars written before a field existed must still load."""
        item = QueueItem.from_dict({
            "item_id": "q-1", "spec_filename": "spec.md", "title": "old",
        })
        assert item is not None
        assert item.target_repo == ""
        assert item.max_attempts == 3
        assert item.source is ItemSource.LOCAL

    def test_round_trip(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        original = _add(
            queue, title="round trip", priority=3,
            source=ItemSource.GITHUB, source_ref="o/r#1",
            target_repo="o/r", project_name="proj",
        )
        decoded = QueueItem.from_dict(original.to_dict())
        assert decoded is not None
        assert decoded.to_dict() == original.to_dict()


class TestLeases:
    def test_lease_records_pid_host_and_expiry(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, lease_ttl_seconds=60)
        item = queue.lease(_add(queue))
        assert item.lease_pid == os.getpid()
        assert item.lease_host
        assert not item.lease_expired()

    def test_expired_lease_is_detected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, lease_ttl_seconds=60)
        item = queue.lease(_add(queue))
        future = datetime.now(UTC) + timedelta(seconds=120)
        assert item.lease_expired(future)

    def test_missing_expiry_counts_as_expired(self) -> None:
        """A lease nobody can read must not wedge the queue forever."""
        item = QueueItem(item_id="q-1", title="t", spec_filename="s.md")
        assert item.lease_expired()

    def test_unparseable_expiry_counts_as_expired(self) -> None:
        item = QueueItem(
            item_id="q-1", title="t", spec_filename="s.md",
            lease_expires_at="not-a-date",
        )
        assert item.lease_expired()

    def test_requeue_clears_the_lease(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.requeue(queue.lease(_add(queue)))
        assert item.lease_pid == 0
        assert item.lease_host == ""
        assert item.lease_expires_at == ""


class TestPoison:
    def test_poison_requires_a_reason(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.start(queue.lease(_add(queue)))
        with pytest.raises(QueueError, match="poison requires a reason"):
            queue.poison(item, reason="  ")

    def test_poison_records_why(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.start(queue.lease(_add(queue)))
        item = queue.poison(item, reason="spec-level failure: tests failed")
        assert item.state is ItemState.POISON
        assert item.poison_reason == "spec-level failure: tests failed"
        assert (tmp_path / ".kstrl" / "queue" / "poison" / item.item_id).is_dir()

    def test_a_queued_item_can_be_poisoned_without_running(
        self, tmp_path: Path,
    ) -> None:
        """Admission-time rejection never spends anything."""
        queue = _queue(tmp_path)
        item = queue.poison(_add(queue), reason="rejected at admission")
        assert item.attempts == 0


class TestRemoval:
    def test_remove_deletes_the_item(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        queue.remove(item)
        assert queue.items() == []
        assert not (tmp_path / ".kstrl" / "queue" / "queued" / item.item_id).exists()

    def test_remove_refuses_while_running(self, tmp_path: Path) -> None:
        """Deleting a live item loses the trail for money already spent."""
        queue = _queue(tmp_path)
        item = queue.start(queue.lease(_add(queue)))
        with pytest.raises(QueueError, match="is running"):
            queue.remove(item)


class TestPause:
    def test_default_is_running(self, tmp_path: Path) -> None:
        assert not _queue(tmp_path).is_paused()

    def test_pause_and_resume(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.pause(reason="daily budget")
        assert queue.is_paused()
        assert queue.pause_state().reason == "daily budget"
        queue.resume()
        assert not queue.is_paused()

    def test_next_ready_is_none_while_paused(self, tmp_path: Path) -> None:
        """The pause is an admission gate checked at the claim point."""
        queue = _queue(tmp_path)
        _add(queue)
        queue.pause(reason="stop")
        assert queue.next_ready() is None
        queue.resume()
        assert queue.next_ready() is not None

    def test_resume_after_expires_the_pause(self, tmp_path: Path) -> None:
        """The daily-budget stop clears itself at the next local day."""
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        state = PauseState(paused=True, reason="budget", resume_after=past)
        assert not state.active()

    def test_resume_after_in_the_future_still_pauses(self) -> None:
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        state = PauseState(paused=True, reason="budget", resume_after=future)
        assert state.active()

    def test_unreadable_pause_marker_reads_as_paused(
        self, tmp_path: Path,
    ) -> None:
        """Fail closed: never resume unattended spend on a corrupt file."""
        queue = _queue(tmp_path)
        queue.ensure_dirs()
        queue.pause_path.write_text("{not json")
        assert queue.is_paused()
        assert "unreadable" in queue.pause_state().reason

    def test_non_object_pause_marker_reads_as_paused(
        self, tmp_path: Path,
    ) -> None:
        queue = _queue(tmp_path)
        queue.ensure_dirs()
        queue.pause_path.write_text("[]")
        assert queue.is_paused()


class TestJournal:
    def test_every_transition_is_recorded(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.finish_ok(queue.start(queue.lease(_add(queue))))
        entries = queue.journal_entries(item.item_id)
        assert [(e["from"], e["to"]) for e in entries] == [
            ("", "queued"), ("queued", "leased"),
            ("leased", "running"), ("running", "done"),
        ]

    def test_journal_records_the_attempt_count(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = queue.start(queue.lease(_add(queue)))
        started = [
            e for e in queue.journal_entries(item.item_id) if e["to"] == "running"
        ]
        assert started[0]["attempts"] == 1

    def test_removal_is_journaled(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        queue.remove(item, actor="tester")
        entries = queue.journal_entries(item.item_id)
        assert entries[-1]["to"] == "removed"
        assert entries[-1]["actor"] == "tester"

    def test_pause_is_journaled(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.pause(reason="budget", actor="serve")
        assert any(
            e["to"] == "paused" and e["reason"] == "budget"
            for e in queue.journal_entries()
        )

    def test_a_failed_journal_write_does_not_undo_the_transition(
        self, tmp_path: Path,
    ) -> None:
        """The directory is the truth; the journal is the narration."""
        queue = _queue(tmp_path)
        item = _add(queue)
        with patch("kstrl.workqueue.open", side_effect=OSError("read-only")):
            with pytest.warns(RuntimeWarning, match="journal append failed"):
                moved = queue.lease(item)
        assert moved.state is ItemState.LEASED
        assert queue.items()[0].state is ItemState.LEASED

    def test_corrupt_journal_lines_are_skipped(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        item = _add(queue)
        with open(queue.journal_path, "a", encoding="utf-8") as handle:
            handle.write("{not json\n\n")
        assert len(queue.journal_entries(item.item_id)) == 1


class TestLock:
    def test_a_second_holder_is_refused(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with queue_lock(tmp_path):
            with pytest.raises(QueueLockedError):
                with queue_lock(tmp_path):
                    pass

    def test_the_lock_is_released_on_exit(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        with queue_lock(tmp_path):
            pass
        with queue_lock(tmp_path):
            pass

    def test_the_lock_is_released_when_the_body_raises(
        self, tmp_path: Path,
    ) -> None:
        pytest.importorskip("fcntl")
        with pytest.raises(ValueError):
            with queue_lock(tmp_path):
                raise ValueError("boom")
        with queue_lock(tmp_path):
            pass

    def test_the_queue_lock_is_separate_from_the_factory_lock(
        self, tmp_path: Path,
    ) -> None:
        """Listing the queue must not block on a running factory."""
        from kstrl.workqueue import LOCK_FILENAME, queue_root

        with queue_lock(tmp_path):
            pass
        assert (queue_root(tmp_path) / LOCK_FILENAME).exists()
        assert not (tmp_path / ".kstrl" / "factory.lock").exists()


class TestConfig:
    def test_defaults(self) -> None:
        config = QueueConfig()
        assert config.max_attempts == 3
        assert config.lease_ttl_seconds == 3600.0

    def test_rejects_a_zero_attempt_budget(self) -> None:
        with pytest.raises(QueueError, match="max_attempts must be >= 1"):
            QueueConfig(max_attempts=0)

    def test_rejects_a_nonpositive_ttl(self) -> None:
        with pytest.raises(QueueError, match="lease_ttl_seconds must be > 0"):
            QueueConfig(lease_ttl_seconds=0)

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_QUEUE_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("KSTRL_QUEUE_LEASE_TTL", "120")
        config = QueueConfig.from_env()
        assert config.max_attempts == 5
        assert config.lease_ttl_seconds == 120.0

    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[queue]\nmax_attempts = 9\nlease_ttl_seconds = 30\n"
        )
        config = QueueConfig.load(tmp_path)
        assert config.max_attempts == 9
        assert config.lease_ttl_seconds == 30.0

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[queue]\nmax_attempts = 9\n")
        monkeypatch.setenv("KSTRL_QUEUE_MAX_ATTEMPTS", "2")
        assert QueueConfig.load(tmp_path).max_attempts == 2

    def test_load_without_a_config_file_uses_defaults(
        self, tmp_path: Path,
    ) -> None:
        assert QueueConfig.load(tmp_path).max_attempts == 3


class TestReporting:
    def test_counts(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        queue.finish_ok(queue.start(queue.lease(_add(queue))))
        counts = queue.counts()
        assert counts[ItemState.QUEUED] == 1
        assert counts[ItemState.DONE] == 1
        assert counts[ItemState.POISON] == 0

    def test_summarize_omits_empty_states(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        _add(queue)
        assert summarize(queue.counts()) == "1 queued"

    def test_summarize_of_an_empty_queue(self, tmp_path: Path) -> None:
        assert summarize(_queue(tmp_path).counts()) == "empty"

    def test_next_ready_ignores_non_queued_items(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.start(queue.lease(_add(queue)))
        assert queue.next_ready() is None
