"""Approved fixtures - pre-approved input/output pairs for behavioral verification.

Provides behavioral verification independent of agent-generated tests.
Fixtures are defined in the PRD and checked during Phase 1 mechanical verification.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from ralph_py.verify import CheckResult, run_scrubbed


@dataclass
class Fixture:
    """A single approved fixture - an input/output pair for behavioral verification."""

    description: str
    fixture_type: str  # "cli", "function", "file"
    input_data: dict[str, Any]  # type-specific input configuration
    expected: dict[str, Any]  # type-specific expected output


@dataclass
class FixtureResult:
    """Result of running a single fixture."""

    fixture: Fixture
    passed: bool
    actual: str = ""
    message: str = ""


@dataclass
class FixturesConfig:
    """Configuration for the fixtures check."""

    enabled: bool = False
    snapshot_on_success: bool = True
    snapshot_dir: Path = field(default_factory=lambda: Path(".ralph/snapshots"))
    timeout: float = 30.0


def run_cli_fixture(
    fixture: Fixture, cwd: Path, timeout: float,
) -> FixtureResult:
    """Run a CLI fixture by executing a command and checking output expectations.

    Checks exit_code, stdout_contains, and stdout_not_contains from expected.
    """
    command = fixture.input_data.get("command")
    if not command:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="No 'command' in input_data",
        )

    try:
        result = run_scrubbed(command, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Command timed out after {timeout}s",
        )
    except OSError as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Failed to run command: {exc}",
        )

    stdout = result.stdout
    failures: list[str] = []

    # Check exit code
    if "exit_code" in fixture.expected:
        expected_code = fixture.expected["exit_code"]
        if result.returncode != expected_code:
            failures.append(
                f"exit code: expected {expected_code}, got {result.returncode}"
            )

    # Check stdout_contains
    for substring in fixture.expected.get("stdout_contains", []):
        if substring not in stdout:
            failures.append(f"stdout missing expected string: {substring!r}")

    # Check stdout_not_contains
    for substring in fixture.expected.get("stdout_not_contains", []):
        if substring in stdout:
            failures.append(f"stdout contains forbidden string: {substring!r}")

    if failures:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            actual=stdout,
            message="; ".join(failures),
        )

    return FixtureResult(
        fixture=fixture,
        passed=True,
        actual=stdout,
        message="CLI fixture passed",
    )


def run_function_fixture(fixture: Fixture, cwd: Path) -> FixtureResult:
    """Run a function fixture by importing a module and calling a function.

    Checks expected return value or expected exception type.
    """
    import sys

    module_name = fixture.input_data.get("module")
    function_name = fixture.input_data.get("function")
    args = fixture.input_data.get("args", [])
    kwargs = fixture.input_data.get("kwargs", {})

    if not isinstance(module_name, str) or not module_name:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="Missing or invalid 'module' in input_data",
        )
    if not isinstance(function_name, str) or not function_name:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="Missing or invalid 'function' in input_data",
        )

    cwd_str = str(cwd)
    added_to_path = cwd_str not in sys.path
    if added_to_path:
        sys.path.insert(0, cwd_str)

    try:
        mod = importlib.import_module(module_name)

        func = getattr(mod, function_name, None)
        if func is None:
            return FixtureResult(
                fixture=fixture,
                passed=False,
                message=f"Function '{function_name}' not found in module '{module_name}'",
            )

        # Check if we expect an exception
        expected_raises = fixture.expected.get("raises")
        if expected_raises:
            try:
                func(*args, **kwargs)
            except Exception as exc:
                exc_type_name = type(exc).__name__
                if exc_type_name == expected_raises:
                    return FixtureResult(
                        fixture=fixture,
                        passed=True,
                        actual=f"raised {exc_type_name}",
                        message="Function fixture passed - expected exception raised",
                    )
                return FixtureResult(
                    fixture=fixture,
                    passed=False,
                    actual=f"raised {exc_type_name}",
                    message=f"Expected {expected_raises}, got {exc_type_name}",
                )
            return FixtureResult(
                fixture=fixture,
                passed=False,
                actual="no exception raised",
                message=f"Expected {expected_raises} but no exception was raised",
            )

        # Normal return value check
        try:
            actual = func(*args, **kwargs)
        except Exception as exc:
            return FixtureResult(
                fixture=fixture,
                passed=False,
                actual=f"raised {type(exc).__name__}: {exc}",
                message=f"Unexpected exception: {type(exc).__name__}: {exc}",
            )

        if "returns" not in fixture.expected:
            return FixtureResult(
                fixture=fixture,
                passed=True,
                actual=repr(actual),
                message="Function fixture passed (no expected return specified)",
            )

        expected_value = fixture.expected["returns"]
        if actual == expected_value:
            return FixtureResult(
                fixture=fixture,
                passed=True,
                actual=repr(actual),
                message="Function fixture passed",
            )
        return FixtureResult(
            fixture=fixture,
            passed=False,
            actual=repr(actual),
            message=f"Expected {expected_value!r}, got {actual!r}",
        )

    except ImportError as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Failed to import module '{module_name}': {exc}",
        )
    finally:
        # Clean up sys.modules to force re-import on next call
        if module_name in sys.modules:
            del sys.modules[module_name]
        if added_to_path:
            try:
                sys.path.remove(cwd_str)
            except ValueError:
                pass


def run_file_fixture(fixture: Fixture, cwd: Path) -> FixtureResult:
    """Run a file fixture by checking file existence and content.

    Checks expected existence, contains, and not_contains expectations.
    """
    rel_path = fixture.input_data.get("path")
    if not rel_path:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="No 'path' in input_data",
        )

    full_path = cwd / rel_path
    file_exists = full_path.exists()

    # Check existence expectation
    expected_exists = fixture.expected.get("exists", True)
    if not expected_exists and not file_exists:
        return FixtureResult(
            fixture=fixture,
            passed=True,
            actual=f"{rel_path} does not exist (as expected)",
            message="File fixture passed",
        )

    if expected_exists and not file_exists:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            actual=f"{rel_path} does not exist",
            message=f"Expected file '{rel_path}' to exist but it does not",
        )

    if not expected_exists and file_exists:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            actual=f"{rel_path} exists",
            message=f"Expected file '{rel_path}' to not exist but it does",
        )

    # File exists and was expected to exist - check content
    try:
        content = full_path.read_text()
    except OSError as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Failed to read file '{rel_path}': {exc}",
        )

    failures: list[str] = []

    for substring in fixture.expected.get("contains", []):
        if substring not in content:
            failures.append(f"file missing expected string: {substring!r}")

    for substring in fixture.expected.get("not_contains", []):
        if substring in content:
            failures.append(f"file contains forbidden string: {substring!r}")

    if failures:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            actual=content[:500],
            message="; ".join(failures),
        )

    return FixtureResult(
        fixture=fixture,
        passed=True,
        actual=f"{rel_path} exists with expected content",
        message="File fixture passed",
    )


def _dispatch_fixture(
    fixture: Fixture, cwd: Path, timeout: float,
) -> FixtureResult:
    """Dispatch a fixture to the appropriate runner."""
    if fixture.fixture_type == "cli":
        return run_cli_fixture(fixture, cwd, timeout)
    if fixture.fixture_type == "function":
        return run_function_fixture(fixture, cwd)
    if fixture.fixture_type == "file":
        return run_file_fixture(fixture, cwd)
    return FixtureResult(
        fixture=fixture,
        passed=False,
        message=f"Unknown fixture type: {fixture.fixture_type}",
    )


def check_fixtures(
    fixtures: list[Fixture],
    cwd: Path,
    config: FixturesConfig,
) -> CheckResult:
    """Run all fixtures and return a single CheckResult for the verification pipeline.

    Each fixture is dispatched to the appropriate runner based on fixture_type.
    Results are aggregated into one CheckResult compatible with verify.py.
    """
    start = time.monotonic()

    if not fixtures:
        return CheckResult(
            name="fixtures",
            passed=True,
            message="No fixtures defined",
            duration_seconds=time.monotonic() - start,
        )

    results: list[FixtureResult] = []
    details: list[str] = []

    for fixture in fixtures:
        result = _dispatch_fixture(fixture, cwd, config.timeout)
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        details.append(f"[{status}] {fixture.description}: {result.message}")

    passed_count = sum(1 for r in results if r.passed)
    total = len(results)
    all_passed = passed_count == total

    return CheckResult(
        name="fixtures",
        passed=all_passed,
        message=f"{passed_count}/{total} fixtures passed",
        details=details,
        duration_seconds=time.monotonic() - start,
    )


def load_fixtures_from_prd_data(prd_data: dict[str, Any]) -> list[Fixture]:
    """Parse the optional 'fixtures' array from PRD JSON data.

    Returns an empty list if no fixtures field is present.
    Each fixture entry should have: description, fixture_type, input_data, expected.
    """
    raw_fixtures = prd_data.get("fixtures", [])
    fixtures: list[Fixture] = []

    for entry in raw_fixtures:
        try:
            fixture = Fixture(
                description=entry["description"],
                fixture_type=entry["fixture_type"],
                input_data=entry.get("input_data", {}),
                expected=entry.get("expected", {}),
            )
            fixtures.append(fixture)
        except KeyError:
            continue

    return fixtures


def save_snapshot(
    component_id: str,
    fixtures: list[Fixture],
    results: list[FixtureResult],
    snapshot_dir: Path,
) -> None:
    """Save successful fixture outputs as a JSON snapshot for regression detection.

    Only saves results for fixtures that passed. The snapshot captures the actual
    output so future runs can detect behavioral regressions.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{component_id}.json"

    snapshot_entries = []
    for fixture, result in zip(fixtures, results, strict=True):
        if result.passed:
            snapshot_entries.append({
                "description": fixture.description,
                "fixture_type": fixture.fixture_type,
                "actual": result.actual,
            })

    snapshot_data = {
        "component_id": component_id,
        "fixture_count": len(snapshot_entries),
        "entries": snapshot_entries,
    }

    snapshot_path.write_text(json.dumps(snapshot_data, indent=2) + "\n")


def check_snapshot_regression(
    component_id: str,
    results: list[FixtureResult],
    snapshot_dir: Path,
) -> list[str]:
    """Compare current results against saved snapshots to detect regressions.

    Returns a list of regression descriptions. An empty list means no regressions.
    A regression is detected when a fixture that previously passed now fails,
    or when its actual output has changed.
    """
    snapshot_path = snapshot_dir / f"{component_id}.json"

    if not snapshot_path.exists():
        return []

    try:
        snapshot_data = json.loads(snapshot_path.read_text())
    except (json.JSONDecodeError, OSError):
        return ["Failed to read snapshot file - cannot check for regressions"]

    previous_entries = {
        entry["description"]: entry
        for entry in snapshot_data.get("entries", [])
    }

    regressions: list[str] = []

    for result in results:
        description = result.fixture.description
        previous = previous_entries.get(description)

        if previous is None:
            # New fixture - no regression possible
            continue

        # Previously passed but now fails
        if not result.passed:
            regressions.append(
                f"Regression in '{description}': previously passed, now fails "
                f"- {result.message}"
            )
            continue

        # Output changed from previous snapshot
        if result.actual != previous.get("actual", ""):
            regressions.append(
                f"Output changed in '{description}': "
                f"previous={previous.get('actual', '')!r}, "
                f"current={result.actual!r}"
            )

    return regressions
