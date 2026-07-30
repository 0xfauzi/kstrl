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
        SpendLedger(tmp_path).charge(10.0, covered_calls=1, total_calls=1)
        result = _invoke(["serve", "--dry-run"], tmp_path)
        assert "BLOCKS" in result.output
        assert "daily budget reached" in result.output

    def test_dry_run_labels_a_floor_total(self, tmp_path: Path) -> None:
        """H4: never let a floor read as a measurement."""
        SpendLedger(tmp_path).charge(3.0, covered_calls=1, total_calls=3)
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

    def test_an_exhausted_infra_verdict_also_exits_nonzero(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """#186 F10: the case the may_retry filter let through.

        An infrastructure verdict whose last attempt is spent is poisoned
        by serve_cycle, but Verdict.RETRY_INFRA.may_retry stays true - so
        the old filter excluded it and `ks serve --once` exited 0 on work
        that was waiting for a human.
        """
        from kstrl.findings import Finding
        from kstrl.manifest import Component, ComponentStatus, Manifest

        _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "1"], tmp_path,
        )
        run_id = "factory-20260730-000000.000000-aaa"
        comp = Component("comp-a", "A", "", [], "a.json", "b/a")
        comp.status = ComponentStatus("failed")
        comp.findings = [Finding.infrastructure_error("review", "cli died")]
        manifest_path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        Manifest(
            version="1", spec_file="s.md", project_name="p",
            base_branch="main", single_pr=False, components=[comp],
            run_id=run_id,
        ).save(manifest_path)

        def fake_runner(**kwargs: object) -> RunOutcome:
            run_dir = tmp_path / ".kstrl" / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").touch()
            return RunOutcome(returncode=1)

        with patch("kstrl.serve.subprocess_factory_runner", fake_runner):
            result = _invoke(["serve", "--once"], tmp_path)
        assert _queue(tmp_path).items()[0].state is ItemState.POISON
        assert result.exit_code == 1, "poisoned work must not report success"

    def test_a_reaped_poison_with_no_item_run_exits_nonzero(
        self, tmp_path: Path, spec_file: Path,
    ) -> None:
        """A path that sets no ran_item, which the old filter also skipped."""
        from kstrl.workqueue import QueueConfig as _QC

        _invoke(
            ["queue", "add", str(spec_file), "--max-attempts", "1"], tmp_path,
        )
        queue = Queue(tmp_path, _QC(max_attempts=1))
        queue.start(queue.lease(queue.items()[0], pid=999999))
        with patch(
            "kstrl.serve.subprocess_factory_runner",
            return_value=RunOutcome(returncode=0),
        ):
            result = _invoke(["serve", "--once"], tmp_path)
        assert _queue(tmp_path).items()[0].state is ItemState.POISON
        assert result.exit_code == 1

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


class TestLaunchdPlist:
    """R8.6 PR 4: the plist must be valid to macOS, not just to us.

    Every structural assertion here is paired with a `plutil -lint` check
    where the platform provides one: a plist that satisfies our own string
    matching but not launchd's parser is worthless.
    """

    @staticmethod
    def _plist(root: Path, *args: str) -> str:
        result = CliRunner().invoke(
            cli, ["serve", "--print-plist", "--root", str(root), *args],
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_the_plist_parses_with_macos_own_parser(
        self, tmp_path: Path,
    ) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        assert parsed["Label"].startswith("com.kstrl.serve.")
        assert parsed["RunAtLoad"] is True
        assert parsed["ProcessType"] == "Background"

    def test_keepalive_mode_is_the_default(self, tmp_path: Path) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        assert parsed["KeepAlive"] is True
        assert "StartInterval" not in parsed
        assert "--once" not in parsed["ProgramArguments"]

    def test_interval_mode_schedules_one_shot_runs(
        self, tmp_path: Path,
    ) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(
            tmp_path, "--plist-mode", "interval", "--plist-interval", "600",
        ).encode())
        assert parsed["StartInterval"] == 600
        assert "KeepAlive" not in parsed
        assert parsed["ProgramArguments"][-1] == "--once", (
            "interval mode must run a single cycle per fire"
        )

    def test_the_restart_throttle_is_set(self, tmp_path: Path) -> None:
        """launchd's 10s default would restart a crash-loop 6x a minute."""
        import plistlib

        from kstrl.serve import LAUNCHD_THROTTLE_SECONDS

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        assert parsed["ThrottleInterval"] == LAUNCHD_THROTTLE_SECONDS
        assert LAUNCHD_THROTTLE_SECONDS >= 60, "a spend control, not politeness"

    def test_an_interval_below_the_throttle_is_refused(
        self, tmp_path: Path,
    ) -> None:
        """A 10s interval against a 60s throttle silently does not happen."""
        result = CliRunner().invoke(cli, [
            "serve", "--print-plist", "--root", str(tmp_path),
            "--plist-mode", "interval", "--plist-interval", "30",
        ])
        assert result.exit_code != 0

    def test_PATH_includes_the_interpreter_and_homebrew(
        self, tmp_path: Path,
    ) -> None:
        """A LaunchAgent inherits no shell env; gh and git must be findable.

        Getting this wrong yields a daemon that runs and silently fails
        every poll, which is the hardest kind of setup bug to see.
        """
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        path = parsed["EnvironmentVariables"]["PATH"]
        assert "/usr/bin" in path
        assert "/opt/homebrew/bin" in path
        assert str(Path(parsed["ProgramArguments"][0]).parent) in path

    def test_paths_are_absolute_and_root_scoped(self, tmp_path: Path) -> None:
        import plistlib

        parsed = plistlib.loads(self._plist(tmp_path).encode())
        root = str(tmp_path.resolve())
        assert parsed["WorkingDirectory"] == root
        assert parsed["StandardOutPath"].startswith(root)
        assert parsed["StandardErrorPath"].startswith(root)
        assert "--root" in parsed["ProgramArguments"]

    def test_the_label_is_unique_per_checkout(self, tmp_path: Path) -> None:
        """Two checkouts must not fight over one launchd job.

        launchd keeps only the last job loaded for a given Label, silently,
        so a shared label means one checkout stops being served.
        """
        from kstrl.serve import launchd_label

        a = tmp_path / "checkout-a"
        b = tmp_path / "checkout-b"
        a.mkdir()
        b.mkdir()
        assert launchd_label(a) != launchd_label(b)

    def test_the_label_is_stable_for_one_checkout(self, tmp_path: Path) -> None:
        from kstrl.serve import launchd_label

        assert launchd_label(tmp_path) == launchd_label(tmp_path)

    def test_the_log_directory_is_created(self, tmp_path: Path) -> None:
        """launchd creates the log file but not its parent."""
        from kstrl.serve import launchd_log_dir

        assert not launchd_log_dir(tmp_path).exists()
        self._plist(tmp_path)
        assert launchd_log_dir(tmp_path).is_dir()

    def test_printing_a_plist_spends_nothing_and_starts_no_daemon(
        self, tmp_path: Path,
    ) -> None:
        with patch("kstrl.serve.serve") as loop:
            with patch("kstrl.serve.subprocess_factory_runner") as runner:
                self._plist(tmp_path)
        assert loop.call_count == 0
        assert runner.call_count == 0

    def test_xml_special_characters_are_escaped(self, tmp_path: Path) -> None:
        """A path containing & or < must not produce a corrupt plist."""
        import plistlib

        awkward = tmp_path / "a & b <test>"
        awkward.mkdir()
        parsed = plistlib.loads(self._plist(awkward).encode())
        assert parsed["WorkingDirectory"] == str(awkward.resolve())
