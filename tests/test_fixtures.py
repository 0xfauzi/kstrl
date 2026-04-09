"""Tests for fixtures module."""

from __future__ import annotations

import json
from pathlib import Path

from ralph_py.fixtures import (
    Fixture,
    FixtureResult,
    FixturesConfig,
    check_fixtures,
    check_snapshot_regression,
    load_fixtures_from_prd_data,
    run_cli_fixture,
    run_file_fixture,
    save_snapshot,
)


# ---------------------------------------------------------------------------
# FixturesConfig defaults
# ---------------------------------------------------------------------------


class TestFixtureConfigDefaults:
    def test_fixture_config_defaults(self) -> None:
        config = FixturesConfig()
        assert config.enabled is False
        assert config.snapshot_on_success is True
        assert config.timeout == 30.0
        assert str(config.snapshot_dir).endswith("snapshots")


# ---------------------------------------------------------------------------
# run_cli_fixture
# ---------------------------------------------------------------------------


class TestRunCliFixture:
    def test_run_cli_fixture_success(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="echo test",
            fixture_type="cli",
            input_data={"command": "echo hello"},
            expected={"exit_code": 0, "stdout_contains": ["hello"]},
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=5.0)
        assert result.passed is True
        assert "hello" in result.actual

    def test_run_cli_fixture_failure(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="false command",
            fixture_type="cli",
            input_data={"command": "false"},
            expected={"exit_code": 0},
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=5.0)
        assert result.passed is False
        assert "exit code" in result.message

    def test_run_cli_fixture_no_command(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="no command",
            fixture_type="cli",
            input_data={},
            expected={},
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=5.0)
        assert result.passed is False
        assert "No 'command'" in result.message

    def test_run_cli_fixture_stdout_not_contains(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="check forbidden output",
            fixture_type="cli",
            input_data={"command": "echo secret"},
            expected={"exit_code": 0, "stdout_not_contains": ["secret"]},
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=5.0)
        assert result.passed is False
        assert "forbidden" in result.message


# ---------------------------------------------------------------------------
# run_file_fixture
# ---------------------------------------------------------------------------


class TestRunFileFixture:
    def test_run_file_fixture_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "output.txt"
        target.write_text("generated content\n")

        fixture = Fixture(
            description="check output file exists",
            fixture_type="file",
            input_data={"path": "output.txt"},
            expected={"exists": True},
        )
        result = run_file_fixture(fixture, tmp_path)
        assert result.passed is True

    def test_run_file_fixture_missing(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="check missing file",
            fixture_type="file",
            input_data={"path": "nonexistent.txt"},
            expected={"exists": True},
        )
        result = run_file_fixture(fixture, tmp_path)
        assert result.passed is False
        assert "exist" in result.message

    def test_run_file_fixture_contains(self, tmp_path: Path) -> None:
        target = tmp_path / "readme.md"
        target.write_text("# My Project\nThis is the readme.\n")

        fixture = Fixture(
            description="readme has title",
            fixture_type="file",
            input_data={"path": "readme.md"},
            expected={"exists": True, "contains": ["# My Project"]},
        )
        result = run_file_fixture(fixture, tmp_path)
        assert result.passed is True

    def test_run_file_fixture_contains_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "readme.md"
        target.write_text("# Other Title\n")

        fixture = Fixture(
            description="readme has expected title",
            fixture_type="file",
            input_data={"path": "readme.md"},
            expected={"contains": ["# My Project"]},
        )
        result = run_file_fixture(fixture, tmp_path)
        assert result.passed is False
        assert "missing expected string" in result.message

    def test_run_file_fixture_expected_missing(self, tmp_path: Path) -> None:
        """Test that expecting a file to not exist passes when it is absent."""
        fixture = Fixture(
            description="file should not exist",
            fixture_type="file",
            input_data={"path": "gone.txt"},
            expected={"exists": False},
        )
        result = run_file_fixture(fixture, tmp_path)
        assert result.passed is True


# ---------------------------------------------------------------------------
# load_fixtures_from_prd_data
# ---------------------------------------------------------------------------


class TestLoadFixturesFromPrdData:
    def test_load_fixtures_from_prd_data(self) -> None:
        prd_data = {
            "fixtures": [
                {
                    "description": "echo test",
                    "fixture_type": "cli",
                    "input_data": {"command": "echo hi"},
                    "expected": {"exit_code": 0},
                },
                {
                    "description": "file check",
                    "fixture_type": "file",
                    "input_data": {"path": "out.txt"},
                    "expected": {"exists": True},
                },
            ]
        }
        fixtures = load_fixtures_from_prd_data(prd_data)
        assert len(fixtures) == 2
        assert fixtures[0].fixture_type == "cli"
        assert fixtures[1].fixture_type == "file"

    def test_load_fixtures_from_prd_data_empty(self) -> None:
        prd_data = {"branchName": "test", "userStories": []}
        fixtures = load_fixtures_from_prd_data(prd_data)
        assert fixtures == []

    def test_load_fixtures_from_prd_data_skips_invalid(self) -> None:
        prd_data = {
            "fixtures": [
                {"fixture_type": "cli"},  # missing description
                {
                    "description": "valid",
                    "fixture_type": "file",
                    "input_data": {"path": "x.txt"},
                    "expected": {"exists": True},
                },
            ]
        }
        fixtures = load_fixtures_from_prd_data(prd_data)
        assert len(fixtures) == 1
        assert fixtures[0].description == "valid"


# ---------------------------------------------------------------------------
# check_fixtures integration
# ---------------------------------------------------------------------------


class TestCheckFixturesIntegration:
    def test_check_fixtures_integration(self, tmp_path: Path) -> None:
        target = tmp_path / "hello.txt"
        target.write_text("hello world\n")

        fixtures = [
            Fixture(
                description="echo passes",
                fixture_type="cli",
                input_data={"command": "echo ok"},
                expected={"exit_code": 0},
            ),
            Fixture(
                description="file exists",
                fixture_type="file",
                input_data={"path": "hello.txt"},
                expected={"exists": True, "contains": ["hello"]},
            ),
        ]
        config = FixturesConfig(enabled=True, timeout=5.0)
        result = check_fixtures(fixtures, tmp_path, config)
        assert result.passed is True
        assert result.name == "fixtures"
        assert "2/2" in result.message
        assert len(result.details) == 2

    def test_check_fixtures_empty(self, tmp_path: Path) -> None:
        config = FixturesConfig(enabled=True)
        result = check_fixtures([], tmp_path, config)
        assert result.passed is True
        assert "No fixtures defined" in result.message

    def test_check_fixtures_partial_failure(self, tmp_path: Path) -> None:
        fixtures = [
            Fixture(
                description="echo passes",
                fixture_type="cli",
                input_data={"command": "echo ok"},
                expected={"exit_code": 0},
            ),
            Fixture(
                description="missing file",
                fixture_type="file",
                input_data={"path": "nonexistent.txt"},
                expected={"exists": True},
            ),
        ]
        config = FixturesConfig(enabled=True, timeout=5.0)
        result = check_fixtures(fixtures, tmp_path, config)
        assert result.passed is False
        assert "1/2" in result.message


# ---------------------------------------------------------------------------
# save_snapshot and check_snapshot_regression
# ---------------------------------------------------------------------------


class TestSaveAndCheckSnapshot:
    def test_save_and_check_snapshot(self, tmp_path: Path) -> None:
        snapshot_dir = tmp_path / "snapshots"

        fixture_a = Fixture(
            description="echo test",
            fixture_type="cli",
            input_data={"command": "echo hi"},
            expected={"exit_code": 0},
        )
        result_a = FixtureResult(
            fixture=fixture_a,
            passed=True,
            actual="hi\n",
            message="CLI fixture passed",
        )

        # Save snapshot
        save_snapshot("comp-a", [fixture_a], [result_a], snapshot_dir)
        snapshot_file = snapshot_dir / "comp-a.json"
        assert snapshot_file.exists()
        data = json.loads(snapshot_file.read_text())
        assert data["component_id"] == "comp-a"
        assert data["fixture_count"] == 1
        assert data["entries"][0]["actual"] == "hi\n"

        # Check no regression when output is the same
        regressions = check_snapshot_regression("comp-a", [result_a], snapshot_dir)
        assert regressions == []

        # Check regression when output changes
        result_changed = FixtureResult(
            fixture=fixture_a,
            passed=True,
            actual="bye\n",
            message="CLI fixture passed",
        )
        regressions = check_snapshot_regression("comp-a", [result_changed], snapshot_dir)
        assert len(regressions) == 1
        assert "Output changed" in regressions[0]

    def test_check_snapshot_no_previous(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="test",
            fixture_type="cli",
            input_data={"command": "echo x"},
            expected={},
        )
        result = FixtureResult(fixture=fixture, passed=True, actual="x\n")
        regressions = check_snapshot_regression("new-comp", [result], tmp_path)
        assert regressions == []
