"""R8.6 PR 2: `ks serve` CLI tests.

`--dry-run` is the surface an operator uses to answer "would this spend
money right now, and why not", so the tests below pin that it reports the
same gates the loop evaluates. A dry run that disagrees with the real
cycle is worse than no dry run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.serve import RunOutcome, RunSpend, SpendLedger
from kstrl.workqueue import ItemState, Queue, QueueConfig


def _queue(root: Path) -> Queue:
    return Queue(root, QueueConfig())


def _invoke(args: list[str], root: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--no-color"])


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "feature.md"
    path.write_text("# Feature\n\nDo the thing.\n")
    return path


@pytest.fixture(autouse=True)
def _no_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kstrl.serve.read_run_spend", lambda root, run_id: RunSpend(),
    )


class TestServeHelp:
    def test_the_command_is_registered(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_help_documents_once_and_dry_run(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--once" in result.output
        assert "--dry-run" in result.output

    def test_help_states_the_retry_rule(self) -> None:
        """The most consequential behaviour should be discoverable."""
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert "infrastructure" in result.output
        assert "poison" in result.output


class TestDryRun:
    def test_dry_run_spends_nothing(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        with patch("kstrl.serve.subprocess_factory_runner") as runner:
            result = _invoke(["serve", "--dry-run"], tmp_path)
        assert result.exit_code == 0
        assert runner.call_count == 0
        assert _queue(tmp_path).items()[0].state is ItemState.QUEUED

    def test_dry_run_names_the_next_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        item = _queue(tmp_path).items()[0]
        assert item.item_id[:12] in result.output

    def test_dry_run_on_an_empty_queue(self, tmp_path: Path) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert result.exit_code == 0
        assert "nothing ready" in result.output

    def test_dry_run_reports_every_gate(self, tmp_path: Path) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        for gate in ("poison breaker", "cost coverage", "budget", "inbox cap"):
            assert gate in result.output

    def test_dry_run_reports_a_blocking_budget(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 5.0\nallow_uncovered_cost = true\n"
        )
        SpendLedger(tmp_path).charge(10.0)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "BLOCKS" in result.output
        assert "daily budget reached" in result.output

    def test_dry_run_labels_a_floor_total(self, tmp_path: Path) -> None:
        """H4: never let a floor read as a measurement."""
        SpendLedger(tmp_path).charge(3.0, lower_bound=True, uncovered_calls=2)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "FLOOR" in result.output

    def test_dry_run_reports_a_paused_queue(self, tmp_path: Path) -> None:
        _invoke(["queue", "pause", "--reason", "operator"], tmp_path)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "operator" in result.output

    def test_dry_run_reports_an_unset_budget_as_uncapped(
        self, tmp_path: Path,
    ) -> None:
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "unset (no cap)" in result.output


class TestServeOnce:
    def test_once_drains_a_single_item(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        with patch(
            "kstrl.serve.subprocess_factory_runner",
            return_value=RunOutcome(returncode=0),
        ):
            result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 0
        assert _queue(tmp_path).items()[0].state is ItemState.DONE

    def test_a_poisoned_item_exits_nonzero(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """launchd reads the exit status; a poisoned item is not success."""
        _invoke(["queue", "add", str(spec_file)], tmp_path)
        with patch(
            "kstrl.serve.subprocess_factory_runner",
            return_value=RunOutcome(returncode=1),
        ):
            result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 1
        assert _queue(tmp_path).items()[0].state is ItemState.POISON

    def test_an_empty_queue_exits_zero(self, tmp_path: Path) -> None:
        result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 0

    def test_a_held_singleton_lock_exits_two(self, tmp_path: Path) -> None:
        pytest.importorskip("fcntl")
        from kstrl.serve import serve_lock

        with serve_lock(tmp_path):
            result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 2
        assert "another ks serve" in result.output

    def test_an_invalid_config_exits_two(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\nmax_consecutive_poison = 0\n"
        )
        result = _invoke(["serve", "--once"], tmp_path)
        assert result.exit_code == 2
        assert "max_consecutive_poison" in result.output

    def test_the_banner_reports_the_effective_settings(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[serve]\ndaily_budget_usd = 7.5\ncaffeinate = false\n"
        )
        result = _invoke(["serve", "--once"], tmp_path)
        assert "$7.50/day" in result.output
        assert "caffeinate off" in result.output
