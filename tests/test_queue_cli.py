"""R8.6 PR 1: `ks queue` CLI tests.

The CLI is the human's half of the safety story, so the behaviors pinned
here are the ones that stop an operator spending money by accident: a
poisoned item that has used its attempts refuses to requeue without an
explicit authorization flag, `rm` will not delete a live run, and the
auto-merge opt-in has to be typed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.workqueue import (
    ItemState,
    MergeDisposition,
    Queue,
    QueueConfig,
)


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "feature.md"
    path.write_text("# Feature\n\nDo the thing.\n")
    return path


def _queue(root: Path) -> Queue:
    return Queue(root, QueueConfig())


def _invoke(args: list[str], root: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--no-color"])


class TestQueueHelp:
    def test_group_is_registered(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "queue" in result.output

    def test_group_help_lists_the_verbs(self) -> None:
        result = CliRunner().invoke(cli, ["queue", "--help"])
        assert result.exit_code == 0
        for verb in ("add", "ls", "show", "retry", "rm", "pause", "resume"):
            assert verb in result.output


class TestAdd:
    def test_add_enqueues(self, tmp_path: Path, spec_file: Path) -> None:
        result = _invoke(["queue", "add", str(spec_file)], tmp_path)
        assert result.exit_code == 0
        items = _queue(tmp_path).items()
        assert len(items) == 1
        assert items[0].title == "feature"
        assert items[0].state is ItemState.QUEUED

    def test_add_defaults_to_stop_at_pr(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        item = _queue(tmp_path).items()[0]
        assert item.merge_disposition is MergeDisposition.STOP_AT_PR
        assert "stop_at_pr" in _invoke(
            ["queue", "show", item.item_id], tmp_path,
        ).output

    def test_auto_merge_must_be_typed(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file), "--auto-merge"], tmp_path)
        item = _queue(tmp_path).items()[0]
        assert item.merge_disposition is MergeDisposition.AUTO_MERGE

    def test_priority_and_max_attempts_are_carried(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(
            ["queue", "add", str(spec_file), "--priority", "4",
             "--max-attempts", "1"],
            tmp_path,
        )
        item = _queue(tmp_path).items()[0]
        assert item.priority == 4
        assert item.max_attempts == 1

    def test_add_warns_while_paused(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "pause", "--reason", "budget"], tmp_path)
        result = _invoke(["queue", "add", str(spec_file)], tmp_path)
        assert result.exit_code == 0
        assert "paused" in result.output.lower()

    def test_missing_spec_is_rejected(self, tmp_path: Path) -> None:
        result = _invoke(["queue", "add", str(tmp_path / "nope.md")], tmp_path)
        assert result.exit_code != 0

    def test_empty_spec_is_rejected(self, tmp_path: Path) -> None:
        blank = tmp_path / "blank.md"
        blank.write_text("\n\n")
        result = _invoke(["queue", "add", str(blank)], tmp_path)
        assert result.exit_code == 1
        assert "empty spec" in result.output

    def test_zero_max_attempts_is_rejected_by_the_option(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """#185 F4: this used to exit 0 and admit an unrunnable item.

        Asserts the OPTION rejects it (exit 2, a click usage error) rather
        than merely that something did. `Queue.add` refuses the same value
        as defence in depth, so a weaker assertion would pass with the
        option constraint removed and prove only the library guard.
        """
        result = _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "0"], tmp_path,
        )
        assert result.exit_code == 2, "click usage error, not a runtime error"
        assert "is not in the range" in result.output
        assert _queue(tmp_path).items() == []

    def test_add_sweeps_an_abandoned_staging_dir(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """#185 F3: enqueue under the lock is the staging recovery point."""
        queue = _queue(tmp_path)
        queue.ensure_dirs()
        (queue.staging_path / "q-ghost").mkdir(parents=True)

        result = _invoke(["queue", "add", str(spec_file)], tmp_path)
        assert result.exit_code == 0
        assert list(_queue(tmp_path).staging_path.iterdir()) == []
        assert len(_queue(tmp_path).items()) == 1


class TestLs:
    def test_empty_queue(self, tmp_path: Path) -> None:
        result = _invoke(["queue", "ls"], tmp_path)
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_lists_items_in_run_order(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file), "--title", "low"], tmp_path)
        _invoke(
            ["queue", "add", str(spec_file), "--title", "high", "--priority", "9"],
            tmp_path,
        )
        result = _invoke(["queue", "ls"], tmp_path)
        assert result.output.index("high") < result.output.index("low")

    def test_state_filter(self, tmp_path: Path, spec_file: Path) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        queue.finish_ok(queue.start(queue.lease(queue.items()[0])))
        assert "empty" in _invoke(
            ["queue", "ls", "--state", "queued"], tmp_path,
        ).output.lower()
        assert "done" in _invoke(
            ["queue", "ls", "--state", "done"], tmp_path,
        ).output

    def test_unknown_state_filter_is_an_error(self, tmp_path: Path) -> None:
        result = _invoke(["queue", "ls", "--state", "wat"], tmp_path)
        assert result.exit_code == 1

    def test_paused_queue_is_announced(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        _invoke(["queue", "pause", "--reason", "daily budget"], tmp_path)
        result = _invoke(["queue", "ls"], tmp_path)
        assert "PAUSED" in result.output
        assert "daily budget" in result.output


class TestShow:
    def test_show_reports_the_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        item = _queue(tmp_path).items()[0]
        result = _invoke(["queue", "show", item.item_id], tmp_path)
        assert result.exit_code == 0
        assert item.item_id in result.output
        assert "feature" in result.output

    def test_show_includes_transition_history(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        queue.start(queue.lease(queue.items()[0]))
        item = queue.items()[0]
        result = _invoke(["queue", "show", item.item_id], tmp_path)
        assert "queued -> leased" in result.output
        assert "leased -> running" in result.output

    def test_show_flags_an_expired_lease(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        item = queue.lease(queue.items()[0])
        meta = queue.item_dir(item) / "meta.json"
        data = json.loads(meta.read_text())
        data["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        meta.write_text(json.dumps(data))
        result = _invoke(["queue", "show", item.item_id], tmp_path)
        assert "EXPIRED" in result.output

    def test_unknown_id(self, tmp_path: Path) -> None:
        result = _invoke(["queue", "show", "q-nope"], tmp_path)
        assert result.exit_code == 1
        assert "No queue item" in result.output

    def test_ambiguous_prefix_is_an_error(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        result = _invoke(["queue", "show", "q-"], tmp_path)
        assert result.exit_code == 1
        assert "matches multiple items" in result.output


class TestRetry:
    def test_retry_requeues_a_failed_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        queue.finish_failed(queue.start(queue.lease(queue.items()[0])), error="infra")
        item = queue.items()[0]

        result = _invoke(["queue", "retry", item.item_id], tmp_path)
        assert result.exit_code == 0
        assert _queue(tmp_path).items()[0].state is ItemState.QUEUED

    def test_retry_does_not_reset_attempts(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        queue.finish_failed(queue.start(queue.lease(queue.items()[0])))
        _invoke(["queue", "retry", queue.items()[0].item_id], tmp_path)
        assert _queue(tmp_path).items()[0].attempts == 1

    def test_exhausted_item_refuses_without_explicit_authorization(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """Spending again after the budget is used up must be deliberate."""
        _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "1"], tmp_path,
        )
        queue = _queue(tmp_path)
        queue.finish_failed(queue.start(queue.lease(queue.items()[0])))
        item = queue.items()[0]

        result = _invoke(["queue", "retry", item.item_id], tmp_path)
        assert result.exit_code == 1
        assert "--reset-attempts" in result.output
        assert _queue(tmp_path).items()[0].state is ItemState.FAILED

    def test_reset_attempts_authorizes_it(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "1"], tmp_path,
        )
        queue = _queue(tmp_path)
        queue.finish_failed(queue.start(queue.lease(queue.items()[0])))
        item = queue.items()[0]

        result = _invoke(
            ["queue", "retry", item.item_id, "--reset-attempts"], tmp_path,
        )
        assert result.exit_code == 0
        reread = _queue(tmp_path).items()[0]
        assert reread.state is ItemState.QUEUED
        assert reread.attempts == 0

    def test_retry_refuses_a_queued_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        item = _queue(tmp_path).items()[0]
        result = _invoke(["queue", "retry", item.item_id], tmp_path)
        assert result.exit_code == 1
        assert "only failed or poisoned" in result.output

    def test_retry_of_a_poisoned_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        queue.poison(queue.items()[0], reason="spec ambiguous")
        item = queue.items()[0]
        result = _invoke(
            ["queue", "retry", item.item_id, "--reset-attempts"], tmp_path,
        )
        assert result.exit_code == 0
        assert _queue(tmp_path).items()[0].poison_reason == ""


class TestRm:
    def test_rm_with_yes_deletes(self, tmp_path: Path, spec_file: Path) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        item = _queue(tmp_path).items()[0]
        result = _invoke(["queue", "rm", item.item_id, "--yes"], tmp_path)
        assert result.exit_code == 0
        assert _queue(tmp_path).items() == []

    def test_rm_declined_leaves_the_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        item = _queue(tmp_path).items()[0]
        result = CliRunner().invoke(
            cli,
            ["queue", "rm", item.item_id, "--root", str(tmp_path), "--no-color"],
            input="n\n",
        )
        assert result.exit_code == 0
        assert len(_queue(tmp_path).items()) == 1

    def test_rm_reports_a_failed_deletion_as_failure(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """#185 F6: the operator must not be told an item is gone."""
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        item = _queue(tmp_path).items()[0]
        with patch(
            "kstrl.workqueue.shutil.rmtree",
            side_effect=PermissionError("read-only"),
        ):
            result = _invoke(["queue", "rm", item.item_id, "--yes"], tmp_path)
        assert result.exit_code == 1
        assert "Could not remove" in result.output
        assert len(_queue(tmp_path).items()) == 1

    def test_rm_refuses_a_running_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        queue = _queue(tmp_path)
        queue.start(queue.lease(queue.items()[0]))
        item = queue.items()[0]
        result = _invoke(["queue", "rm", item.item_id, "--yes"], tmp_path)
        assert result.exit_code == 1
        assert "is running" in result.output
        assert len(_queue(tmp_path).items()) == 1


class TestPauseResume:
    def test_pause_then_resume(self, tmp_path: Path, spec_file: Path) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        assert _invoke(["queue", "pause"], tmp_path).exit_code == 0
        assert _queue(tmp_path).is_paused()
        assert _queue(tmp_path).next_ready() is None

        result = _invoke(["queue", "resume"], tmp_path)
        assert result.exit_code == 0
        assert "1 item" in result.output
        assert not _queue(tmp_path).is_paused()

    def test_pause_records_the_reason(self, tmp_path: Path) -> None:
        _invoke(["queue", "pause", "--reason", "daily budget"], tmp_path)
        assert _queue(tmp_path).pause_state().reason == "daily budget"
