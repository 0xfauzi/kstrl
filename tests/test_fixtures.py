"""Tests for fixtures module (R7.2: sandboxed wiring into Phase 1)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl.fixtures import (
    Fixture,
    FixtureResult,
    FixturesConfig,
    check_fixtures,
    check_fixtures_from_prd,
    check_snapshot_regression,
    load_fixtures_from_prd_data,
    run_cli_fixture,
    run_file_fixture,
    run_function_fixture,
    save_snapshot,
)
from kstrl.prd import PRD
from kstrl.verify import VerifyConfig, run_mechanical_verification


def _story() -> dict[str, Any]:
    return {
        "id": "US-1",
        "title": "Story",
        "acceptanceCriteria": ["works"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }


def _prd_data(
    fixtures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "branchName": "kstrl/fixtures-test",
        "userStories": [_story()],
    }
    if fixtures is not None:
        data["fixtures"] = fixtures
    return data


def _cli_fixture_entry() -> dict[str, Any]:
    return {
        "description": "echo works",
        "fixture_type": "cli",
        "input_data": {"command": "echo hi"},
        "expected": {"exit_code": 0, "stdout_contains": ["hi"]},
    }

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

    def test_load_fixtures_from_prd_data_rejects_invalid(self) -> None:
        # A silently-dropped fixture is a silently-weakened oracle:
        # malformed entries raise instead of being skipped.
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
        with pytest.raises(ValueError, match=r"fixtures\[0\].*description"):
            load_fixtures_from_prd_data(prd_data)


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


# ---------------------------------------------------------------------------
# R7.2: CLI fixtures run without a shell
# ---------------------------------------------------------------------------


class TestCliFixtureNoShell:
    def test_shell_metacharacters_are_literal(self, tmp_path: Path) -> None:
        # Under shell=True `echo hello && touch pwned` would run two
        # commands; with shlex + shell=False the metacharacters reach
        # echo as literal arguments.
        fixture = Fixture(
            description="metachars literal",
            fixture_type="cli",
            input_data={"command": "echo hello && touch pwned.txt"},
            expected={
                "exit_code": 0,
                "stdout_contains": ["hello && touch pwned.txt"],
            },
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=10.0)
        assert result.passed is True, result.message
        assert not (tmp_path / "pwned.txt").exists()

    def test_no_variable_expansion(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="no env expansion",
            fixture_type="cli",
            input_data={"command": "echo $HOME"},
            expected={"exit_code": 0, "stdout_contains": ["$HOME"]},
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=10.0)
        assert result.passed is True, result.message

    def test_unparseable_command_fails_with_hint(self, tmp_path: Path) -> None:
        fixture = Fixture(
            description="unterminated quote",
            fixture_type="cli",
            input_data={"command": "echo 'unterminated"},
            expected={"exit_code": 0},
        )
        result = run_cli_fixture(fixture, tmp_path, timeout=10.0)
        assert result.passed is False
        assert "shell features are unsupported" in result.message


# ---------------------------------------------------------------------------
# R7.2: function fixtures run in a subprocess
# ---------------------------------------------------------------------------


class TestFunctionFixtureSubprocess:
    def test_round_trip_pass(self, tmp_path: Path) -> None:
        (tmp_path / "adder.py").write_text("def add(a, b):\n    return a + b\n")
        fixture = Fixture(
            description="add works",
            fixture_type="function",
            input_data={"module": "adder", "function": "add", "args": [2, 3]},
            expected={"returns": 5},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is True, result.message
        assert result.actual == "5"

    def test_round_trip_fail(self, tmp_path: Path) -> None:
        (tmp_path / "adder.py").write_text("def add(a, b):\n    return a + b\n")
        fixture = Fixture(
            description="add wrong expectation",
            fixture_type="function",
            input_data={"module": "adder", "function": "add", "args": [2, 3]},
            expected={"returns": 6},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is False
        assert "Expected 6, got 5" in result.message

    def test_expected_exception(self, tmp_path: Path) -> None:
        (tmp_path / "boom.py").write_text(
            "def explode():\n    raise ValueError('no')\n"
        )
        fixture = Fixture(
            description="explode raises",
            fixture_type="function",
            input_data={"module": "boom", "function": "explode"},
            expected={"raises": "ValueError"},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is True, result.message

    def test_import_crash_reports_failure(self, tmp_path: Path) -> None:
        (tmp_path / "crasher.py").write_text("raise RuntimeError('boom')\n")
        fixture = Fixture(
            description="crashing module",
            fixture_type="function",
            input_data={"module": "crasher", "function": "anything"},
            expected={"returns": 1},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is False
        assert "Failed to import module" in result.message

    def test_env_scrubbed_inside_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Plant a would-leak secret in the harness env; the fixture
        # subprocess must not see any *_API_KEY / *SECRET* name.
        monkeypatch.setenv("FAKE_API_KEY", "sk-super-secret")
        monkeypatch.setenv("MY_DEPLOY_SECRET", "hunter2")
        (tmp_path / "envprobe.py").write_text(
            "import os\n"
            "def leaked():\n"
            "    return sorted(\n"
            "        k for k in os.environ\n"
            "        if 'API_KEY' in k or 'SECRET' in k\n"
            "    )\n"
        )
        fixture = Fixture(
            description="no secrets visible",
            fixture_type="function",
            input_data={"module": "envprobe", "function": "leaked"},
            expected={"returns": []},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is True, (
            f"secrets leaked into fixture subprocess: {result.actual}"
        )

    def test_module_side_effects_cannot_touch_harness(
        self, tmp_path: Path,
    ) -> None:
        # The module runs arbitrary code at import time. It must run in
        # the SUBPROCESS: the sentinel file proves the code executed,
        # while the harness process's environ and sys.modules stay clean.
        (tmp_path / "evil_side_effect.py").write_text(
            "import os\n"
            "os.environ['KSTRL_FIXTURE_PWNED'] = '1'\n"
            "with open('sentinel.txt', 'w') as f:\n"
            "    f.write('imported')\n"
            "def probe():\n"
            "    return 'ok'\n"
        )
        fixture = Fixture(
            description="side-effecting module",
            fixture_type="function",
            input_data={"module": "evil_side_effect", "function": "probe"},
            expected={"returns": "ok"},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is True, result.message
        # The agent code DID run (in the subprocess, cwd=worktree)...
        assert (tmp_path / "sentinel.txt").read_text() == "imported"
        # ...but never inside the harness process.
        assert "KSTRL_FIXTURE_PWNED" not in os.environ
        assert "evil_side_effect" not in sys.modules

    def test_timeout_kills_subprocess(self, tmp_path: Path) -> None:
        (tmp_path / "sleeper.py").write_text(
            "import time\n"
            "def nap():\n"
            "    time.sleep(60)\n"
        )
        fixture = Fixture(
            description="sleeping function",
            fixture_type="function",
            input_data={"module": "sleeper", "function": "nap"},
            expected={"returns": None},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=1.0)
        assert result.passed is False
        assert "timed out" in result.message

    def test_missing_function_reports_failure(self, tmp_path: Path) -> None:
        (tmp_path / "adder.py").write_text("def add(a, b):\n    return a + b\n")
        fixture = Fixture(
            description="missing function",
            fixture_type="function",
            input_data={"module": "adder", "function": "subtract"},
            expected={"returns": 1},
        )
        result = run_function_fixture(fixture, tmp_path, timeout=60.0)
        assert result.passed is False
        assert "not found" in result.message


# ---------------------------------------------------------------------------
# R7.2: file fixtures stay inside the worktree
# ---------------------------------------------------------------------------


class TestFileFixtureContainment:
    @pytest.mark.parametrize("bad_path", ["/etc/passwd", "../outside.txt"])
    def test_escaping_paths_rejected(
        self, tmp_path: Path, bad_path: str,
    ) -> None:
        fixture = Fixture(
            description="escape attempt",
            fixture_type="file",
            input_data={"path": bad_path},
            expected={"exists": True},
        )
        result = run_file_fixture(fixture, tmp_path)
        assert result.passed is False
        assert "relative to the worktree" in result.message

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret material\n")
        (worktree / "link.txt").symlink_to(outside)
        fixture = Fixture(
            description="symlink escape",
            fixture_type="file",
            input_data={"path": "link.txt"},
            expected={"exists": True, "contains": ["secret"]},
        )
        result = run_file_fixture(fixture, worktree)
        assert result.passed is False
        assert "escapes the worktree" in result.message


# ---------------------------------------------------------------------------
# R7.2: strict PRD schema for the fixtures key
# ---------------------------------------------------------------------------


class TestPrdFixturesSchema:
    def test_readme_examples_accepted(self) -> None:
        data = _prd_data(fixtures=[
            {
                "description": "Login returns token",
                "fixture_type": "cli",
                "input_data": {"command": "curl -s localhost:8000/api/login"},
                "expected": {"exit_code": 0, "stdout_contains": ["token"]},
            },
            {
                "description": "Config is importable",
                "fixture_type": "function",
                "input_data": {
                    "module": "src.config",
                    "function": "get_settings",
                    "args": [],
                },
                "expected": {"returns": {"debug": False}},
            },
            {
                "description": "Migration file exists",
                "fixture_type": "file",
                "input_data": {"path": "migrations/001_users.sql"},
                "expected": {
                    "exists": True,
                    "contains": ["CREATE TABLE users"],
                },
            },
        ])
        assert PRD.validate_schema(data) == []

    @pytest.mark.parametrize(
        ("mutation", "error_fragment"),
        [
            ({"shell": True}, "unexpected keys"),
            ({"fixture_type": "shell"}, "fixture_type"),
            (
                {"expected": {"exit_code": 0, "stdout_containz": ["x"]}},
                "unexpected keys for cli",
            ),
            ({"expected": {"exit_code": True}}, "must be an integer"),
            ({"expected": {}}, "must not be empty"),
            (
                {"input_data": {"command": "echo hi", "shell": True}},
                "unexpected keys for cli",
            ),
            ({"input_data": {}}, "missing required key: command"),
        ],
    )
    def test_bad_cli_entries_rejected(
        self, mutation: dict[str, Any], error_fragment: str,
    ) -> None:
        entry = _cli_fixture_entry()
        entry.update(mutation)
        errors = PRD.validate_schema(_prd_data(fixtures=[entry]))
        assert errors, "expected schema errors"
        assert any(error_fragment in e for e in errors), errors

    @pytest.mark.parametrize("bad_path", ["/etc/passwd", "../secrets.txt"])
    def test_file_fixture_escaping_path_rejected(self, bad_path: str) -> None:
        entry = {
            "description": "escape",
            "fixture_type": "file",
            "input_data": {"path": bad_path},
            "expected": {"exists": True},
        }
        errors = PRD.validate_schema(_prd_data(fixtures=[entry]))
        assert any("relative to the" in e for e in errors), errors

    def test_function_returns_and_raises_mutually_exclusive(self) -> None:
        entry = {
            "description": "conflicting expectations",
            "fixture_type": "function",
            "input_data": {"module": "m", "function": "f"},
            "expected": {"returns": 1, "raises": "ValueError"},
        }
        errors = PRD.validate_schema(_prd_data(fixtures=[entry]))
        assert any("mutually exclusive" in e for e in errors), errors

    def test_empty_fixtures_array_rejected(self) -> None:
        errors = PRD.validate_schema(_prd_data(fixtures=[]))
        assert any("non-empty" in e for e in errors), errors

    def test_prd_round_trip_with_fixtures(self, tmp_path: Path) -> None:
        data = _prd_data(fixtures=[_cli_fixture_entry()])
        data["allowedPaths"] = ["src/"]
        path = tmp_path / "prd.json"
        path.write_text(json.dumps(data))

        prd = PRD.load(path)
        assert prd.fixtures == [_cli_fixture_entry()]

        out_path = tmp_path / "prd_out.json"
        prd.save(out_path)
        reloaded = PRD.load(out_path)
        assert reloaded.fixtures == [_cli_fixture_entry()]
        assert reloaded.allowed_paths == ["src/"]


# ---------------------------------------------------------------------------
# R7.2: snapshot regression wired behind the same flag
# ---------------------------------------------------------------------------


class TestSnapshotWiring:
    def _fixtures(self) -> list[Fixture]:
        return [
            Fixture(
                description="cat output file",
                fixture_type="cli",
                input_data={"command": "cat out.txt"},
                expected={"exit_code": 0},
            ),
        ]

    def test_snapshot_saved_then_regression_detected(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "out.txt").write_text("v1\n")
        config = FixturesConfig(
            enabled=True, snapshot_dir=tmp_path / "snaps", timeout=10.0,
        )

        first = check_fixtures(
            self._fixtures(), tmp_path, config, component_id="comp-a",
        )
        assert first.passed is True
        assert (tmp_path / "snaps" / "comp-a.json").exists()

        # Behavior changes while the fixture still "passes": the
        # snapshot comparison catches it.
        (tmp_path / "out.txt").write_text("v2\n")
        second = check_fixtures(
            self._fixtures(), tmp_path, config, component_id="comp-a",
        )
        assert second.passed is False
        assert any("REGRESSION" in d for d in second.details)
        assert any("delete" in d for d in second.details)

    def test_no_component_id_means_no_snapshot(self, tmp_path: Path) -> None:
        (tmp_path / "out.txt").write_text("v1\n")
        config = FixturesConfig(
            enabled=True, snapshot_dir=tmp_path / "snaps", timeout=10.0,
        )
        result = check_fixtures(self._fixtures(), tmp_path, config)
        assert result.passed is True
        assert not (tmp_path / "snaps").exists()


# ---------------------------------------------------------------------------
# R7.2: FixturesConfig control-plane loader
# ---------------------------------------------------------------------------


_FIXTURES_ENV_VARS = (
    "KSTRL_FIXTURES_ENABLED",
    "KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS",
    "KSTRL_FIXTURES_SNAPSHOT_DIR",
    "KSTRL_FIXTURES_TIMEOUT",
)


class TestFixturesConfigLoad:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in _FIXTURES_ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_defaults(self, tmp_path: Path) -> None:
        config = FixturesConfig.load(tmp_path)
        assert config.enabled is False
        assert config.snapshot_on_success is True
        assert config.timeout == 30.0
        # Relative snapshot_dir resolves against the repo root so
        # snapshots survive worktree recreation between runs.
        assert config.snapshot_dir == tmp_path / ".kstrl/snapshots"

    def test_load_from_toml(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[fixtures]\n"
            "enabled = true\n"
            "snapshot_on_success = false\n"
            'snapshot_dir = "snaps"\n'
            "timeout = 12.5\n"
        )
        config = FixturesConfig.load(tmp_path)
        assert config.enabled is True
        assert config.snapshot_on_success is False
        assert config.snapshot_dir == tmp_path / "snaps"
        assert config.timeout == 12.5

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[fixtures]\nenabled = true\n")
        monkeypatch.setenv("KSTRL_FIXTURES_ENABLED", "0")
        config = FixturesConfig.load(tmp_path)
        assert config.enabled is False


# ---------------------------------------------------------------------------
# R7.2: Phase 1 integration - run_mechanical_verification wiring
# ---------------------------------------------------------------------------


def _stub_verify_config() -> VerifyConfig:
    # Stub out the heavyweight checks so the test exercises only the
    # fixtures wiring; prd_stories still runs against the real PRD file.
    return VerifyConfig(
        test_command="echo tests-ok",
        typecheck_command="echo mypy-ok",
        lint_command="echo lint-ok",
        check_diff_scope=False,
        check_bad_patterns=False,
    )


class TestPhase1Integration:
    def _write_prd(
        self, worktree: Path, fixtures: list[dict[str, Any]],
    ) -> Path:
        prd_path = worktree / "prd.json"
        prd_path.write_text(json.dumps(_prd_data(fixtures=fixtures)))
        return prd_path

    def test_fixtures_check_runs_when_enabled(self, tmp_path: Path) -> None:
        (tmp_path / "adder.py").write_text("def add(a, b):\n    return a + b\n")
        prd_path = self._write_prd(tmp_path, [
            _cli_fixture_entry(),
            {
                "description": "adder works",
                "fixture_type": "function",
                "input_data": {
                    "module": "adder", "function": "add", "args": [2, 3],
                },
                "expected": {"returns": 5},
            },
        ])
        result = run_mechanical_verification(
            tmp_path, prd_path, "main", None, _stub_verify_config(),
            fixtures_config=FixturesConfig(
                enabled=True, snapshot_dir=tmp_path / "snaps", timeout=60.0,
            ),
            component_id="comp-x",
        )
        by_name = {c.name: c for c in result.checks}
        assert "fixtures" in by_name
        assert by_name["fixtures"].passed is True, by_name["fixtures"].message
        assert "2/2" in by_name["fixtures"].message
        assert result.passed is True

    def test_fixtures_check_absent_when_disabled(self, tmp_path: Path) -> None:
        prd_path = self._write_prd(tmp_path, [_cli_fixture_entry()])
        for fixtures_config in (None, FixturesConfig(enabled=False)):
            result = run_mechanical_verification(
                tmp_path, prd_path, "main", None, _stub_verify_config(),
                fixtures_config=fixtures_config,
                component_id="comp-x",
            )
            assert "fixtures" not in {c.name for c in result.checks}

    def test_fixture_failure_yields_retry_context(self, tmp_path: Path) -> None:
        prd_path = self._write_prd(tmp_path, [
            {
                "description": "output has magic token",
                "fixture_type": "cli",
                "input_data": {"command": "echo actual-output"},
                "expected": {"stdout_contains": ["magic-token"]},
            },
        ])
        result = run_mechanical_verification(
            tmp_path, prd_path, "main", None, _stub_verify_config(),
            fixtures_config=FixturesConfig(
                enabled=True, snapshot_dir=tmp_path / "snaps", timeout=60.0,
            ),
            component_id="comp-x",
        )
        assert result.passed is False
        context = result.as_context()
        assert "fixtures" in context
        assert "output has magic token" in context
        assert "magic-token" in context

    def test_unreadable_prd_fails_closed(self, tmp_path: Path) -> None:
        prd_path = tmp_path / "prd.json"
        prd_path.write_text("{not json")
        check = check_fixtures_from_prd(
            prd_path, tmp_path, FixturesConfig(enabled=True),
        )
        assert check.passed is False
        assert "failing closed" in check.message

    def test_invalid_fixture_schema_fails_closed(self, tmp_path: Path) -> None:
        entry = _cli_fixture_entry()
        entry["expected"] = {"stdout_containz": ["x"]}  # misspelled key
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps(_prd_data(fixtures=[entry])))
        check = check_fixtures_from_prd(
            prd_path, tmp_path, FixturesConfig(enabled=True),
        )
        assert check.passed is False
        assert "failing closed" in check.message
        assert any("stdout_containz" in d for d in check.details)
