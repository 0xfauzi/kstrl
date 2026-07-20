"""Approved fixtures - pre-approved input/output pairs for behavioral verification.

Provides behavioral verification independent of agent-generated tests.
Fixtures are defined in the PRD and checked during Phase 1 mechanical
verification when ``[fixtures].enabled`` is set (R7.2; default off per
the roadmap user decision).

Threat model (R7.2 / CRIT-3): the PRD is LLM-emitted, so every fixture
definition is untrusted input. Function fixtures therefore execute in a
subprocess with the R2.6 scrubbed environment - the harness process
never imports agent code - and CLI fixtures run with ``shell=False`` so
metacharacters in a PRD-supplied command are literal arguments, never
shell syntax. What sandboxing does NOT claim: agent code runs inside the
fixture subprocess and could forge the result line, but that grants no
power beyond hardcoding the function's return value, which fixtures
legitimately accept - they verify behavior, they are not a defense
against a malicious implementation.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl import envcompat
from kstrl.prd import PRD
from kstrl.verify import CheckResult, run_scrubbed


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
    """Configuration for the fixtures check (``[fixtures]`` in ralph.toml).

    ``enabled`` defaults to False (R7.2 user decision 4): fixtures run
    PRD-defined commands and import PRD-named modules, so the operator
    must opt in explicitly.
    """

    enabled: bool = False
    snapshot_on_success: bool = True
    snapshot_dir: Path = field(default_factory=lambda: Path(".kstrl/snapshots"))
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> FixturesConfig:
        """Load fixtures config from environment variables."""
        from kstrl.config import _parse_bool

        return cls(
            enabled=_parse_bool(envcompat.get("KSTRL_FIXTURES_ENABLED")),
            snapshot_on_success=_parse_bool(
                envcompat.get("KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS", "1")
            ),
            snapshot_dir=Path(
                envcompat.get("KSTRL_FIXTURES_SNAPSHOT_DIR", ".kstrl/snapshots")
            ),
            timeout=float(envcompat.get("KSTRL_FIXTURES_TIMEOUT", "30")),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FixturesConfig:
        """Load fixtures config with precedence: env > toml > defaults.

        A relative ``snapshot_dir`` resolves against ``root_dir`` (the
        operator's repo), NOT the component worktree: worktrees are
        recreated across runs, so a worktree-relative snapshot would be
        wiped before the next run could compare against it.
        """
        from kstrl.config import _parse_bool, load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "fixtures")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "snapshot_on_success" in section:
            config.snapshot_on_success = bool(section["snapshot_on_success"])
        if "snapshot_dir" in section:
            config.snapshot_dir = Path(str(section["snapshot_dir"]))
        if "timeout" in section:
            config.timeout = float(section["timeout"])
        if envcompat.contains("KSTRL_FIXTURES_ENABLED"):
            config.enabled = _parse_bool(envcompat.require("KSTRL_FIXTURES_ENABLED"))
        if envcompat.contains("KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS"):
            config.snapshot_on_success = _parse_bool(
                envcompat.require("KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS")
            )
        if envcompat.contains("KSTRL_FIXTURES_SNAPSHOT_DIR"):
            config.snapshot_dir = Path(envcompat.require("KSTRL_FIXTURES_SNAPSHOT_DIR"))
        if envcompat.contains("KSTRL_FIXTURES_TIMEOUT"):
            config.timeout = float(envcompat.require("KSTRL_FIXTURES_TIMEOUT"))
        if not config.snapshot_dir.is_absolute():
            config.snapshot_dir = root_dir / config.snapshot_dir
        return config


def run_cli_fixture(
    fixture: Fixture, cwd: Path, timeout: float,
) -> FixtureResult:
    """Run a CLI fixture by executing a command and checking output expectations.

    Checks exit_code, stdout_contains, and stdout_not_contains from expected.

    The command string is tokenized with ``shlex.split`` and executed
    with ``shell=False``: shell features (pipes, redirection, ``&&``,
    variable expansion, globbing) are NOT supported, and metacharacters
    in the PRD-supplied command reach the program as literal arguments.
    """
    command = fixture.input_data.get("command")
    if not command or not isinstance(command, str):
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="No 'command' in input_data",
        )

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Could not parse command ({exc}); note that shell "
            "features are unsupported - the command is split with shlex "
            "and executed without a shell",
        )
    if not argv:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="Command is empty after parsing",
        )

    try:
        result = run_scrubbed(argv, cwd=cwd, timeout=timeout)
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


# Result line prefix the function-fixture runner prints. The parent scans
# stdout for the LAST line with this prefix, so module-level prints from
# agent code cannot shadow the runner's genuine verdict as long as the
# runner completes (see the module docstring for the forgery equivalence
# argument).
_RESULT_MARKER = "RALPH-FIXTURE-RESULT-V1:"

# Source of the subprocess that imports and calls the agent-written
# function. Composed with the marker constant so the two cannot drift.
# It mirrors the pass/fail semantics the in-process runner used to have:
# expected exception, unexpected exception, expected return, no expected
# return (vacuous pass - rejected earlier by PRD validation).
_FUNCTION_FIXTURE_RUNNER = (
    "_MARKER = " + repr(_RESULT_MARKER) + "\n"
    + '''
import importlib
import json
import sys


def _emit(passed, actual, message):
    sys.stdout.flush()
    sys.stdout.write(
        "\\n" + _MARKER + json.dumps(
            {"passed": passed, "actual": actual, "message": message}
        ) + "\\n"
    )
    sys.stdout.flush()


def _main():
    spec = json.loads(sys.argv[1])
    expected = spec.get("expected", {})
    args = spec.get("args", [])
    kwargs = spec.get("kwargs", {})
    try:
        mod = importlib.import_module(spec["module"])
    except BaseException as exc:
        _emit(False, "", "Failed to import module %r: %s: %s"
              % (spec["module"], type(exc).__name__, exc))
        return
    func = getattr(mod, spec["function"], None)
    if func is None:
        _emit(False, "", "Function %r not found in module %r"
              % (spec["function"], spec["module"]))
        return
    expected_raises = expected.get("raises")
    if expected_raises:
        try:
            func(*args, **kwargs)
        except Exception as exc:
            name = type(exc).__name__
            if name == expected_raises:
                _emit(True, "raised " + name,
                      "Function fixture passed - expected exception raised")
            else:
                _emit(False, "raised " + name,
                      "Expected %s, got %s" % (expected_raises, name))
            return
        _emit(False, "no exception raised",
              "Expected %s but no exception was raised" % expected_raises)
        return
    try:
        actual = func(*args, **kwargs)
    except Exception as exc:
        _emit(False, "raised %s: %s" % (type(exc).__name__, exc),
              "Unexpected exception: %s: %s" % (type(exc).__name__, exc))
        return
    if "returns" not in expected:
        _emit(True, repr(actual),
              "Function fixture passed (no expected return specified)")
        return
    expected_value = expected["returns"]
    if actual == expected_value:
        _emit(True, repr(actual), "Function fixture passed")
    else:
        _emit(False, repr(actual),
              "Expected %r, got %r" % (expected_value, actual))


_main()
'''
)


def _parse_runner_result(stdout: str) -> dict[str, Any] | None:
    """Extract the runner's verdict from subprocess stdout, last line wins."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(_RESULT_MARKER):
            try:
                payload = json.loads(line[len(_RESULT_MARKER):])
            except json.JSONDecodeError:
                return None
            if isinstance(payload, dict):
                return payload
            return None
    return None


def run_function_fixture(
    fixture: Fixture, cwd: Path, timeout: float,
) -> FixtureResult:
    """Run a function fixture in a subprocess and check its reported result.

    The spec (module, function, args, kwargs, expected) travels as a JSON
    argv argument to ``sys.executable -c <runner>``; the runner imports
    the module from ``cwd`` (the worktree), calls the function, compares
    against ``expected`` in-process (Python ``==``), and prints a single
    marker-prefixed JSON verdict line. The harness process never imports
    agent code; the subprocess gets the R2.6 scrubbed environment, runs
    in its own session, and is group-killed on timeout (``run_scrubbed``).

    Two documented limitations: the fixture runs under the HARNESS's
    Python interpreter (``sys.executable``), not the project's venv, so
    fixtures must not need project-only third-party imports; and the
    ``returns`` comparison is JSON-shaped (a function returning a tuple
    will not equal a JSON array).
    """
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
    if not isinstance(args, list):
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="'args' must be an array",
        )
    if not isinstance(kwargs, dict):
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="'kwargs' must be an object",
        )

    spec = {
        "module": module_name,
        "function": function_name,
        "args": args,
        "kwargs": kwargs,
        "expected": fixture.expected,
    }
    try:
        spec_json = json.dumps(spec)
    except (TypeError, ValueError) as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Fixture spec is not JSON-serializable: {exc}",
        )

    argv = [sys.executable, "-c", _FUNCTION_FIXTURE_RUNNER, spec_json]
    try:
        result = run_scrubbed(argv, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Function fixture timed out after {timeout}s",
        )
    except OSError as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Failed to launch fixture subprocess: {exc}",
        )

    payload = _parse_runner_result(result.stdout)
    if payload is None:
        stderr_tail = result.stderr.strip()[-500:]
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=(
                f"Fixture subprocess exited {result.returncode} without "
                f"reporting a result (module-level crash or hard exit); "
                f"stderr tail: {stderr_tail!r}"
            ),
        )
    return FixtureResult(
        fixture=fixture,
        passed=bool(payload.get("passed", False)),
        actual=str(payload.get("actual", "")),
        message=str(payload.get("message", "")),
    )


def run_file_fixture(fixture: Fixture, cwd: Path) -> FixtureResult:
    """Run a file fixture by checking file existence and content.

    Checks expected existence, contains, and not_contains expectations.
    The path must stay inside ``cwd``: PRD-supplied paths are untrusted,
    and a traversal or symlink escape would leak file content outside
    the worktree into ``actual`` (which flows into retry prompts and PR
    bodies).
    """
    rel_path = fixture.input_data.get("path")
    if not rel_path or not isinstance(rel_path, str):
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message="No 'path' in input_data",
        )

    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=(
                f"Path {rel_path!r} must be relative to the worktree "
                "with no '..' components"
            ),
        )

    full_path = cwd / rel
    resolved_cwd = cwd.resolve()
    resolved = full_path.resolve()
    if resolved != resolved_cwd and resolved_cwd not in resolved.parents:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Path {rel_path!r} escapes the worktree (symlink?)",
        )
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
        return run_function_fixture(fixture, cwd, timeout)
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
    component_id: str | None = None,
) -> CheckResult:
    """Run all fixtures and return a single CheckResult for the verification pipeline.

    Each fixture is dispatched to the appropriate runner based on
    fixture_type. Results are aggregated into one CheckResult compatible
    with verify.py; failure details name the fixture and what diverged
    so ``VerificationResult.as_context()`` carries actionable retry
    context.

    When ``component_id`` is given, snapshot regression runs behind the
    same ``[fixtures].enabled`` flag: current results are compared
    against the component's saved snapshot (a previously-passing fixture
    that now fails, or whose output changed, fails the check), and a
    fully-passing run refreshes the snapshot when
    ``config.snapshot_on_success`` is set.
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
        line = f"[{status}] {fixture.description}: {result.message}"
        if not result.passed and result.actual:
            line += f" (actual: {result.actual[:200]!r})"
        details.append(line)

    passed_count = sum(1 for r in results if r.passed)
    total = len(results)
    all_passed = passed_count == total
    message = f"{passed_count}/{total} fixtures passed"

    if component_id is not None:
        snapshot_dir = (
            config.snapshot_dir
            if config.snapshot_dir.is_absolute()
            else cwd / config.snapshot_dir
        )
        regressions = check_snapshot_regression(
            component_id, results, snapshot_dir,
        )
        if regressions:
            all_passed = False
            message += f"; {len(regressions)} snapshot regression(s)"
            details.extend(f"[REGRESSION] {r}" for r in regressions)
            details.append(
                "If the behavior change is intentional, delete "
                f"{snapshot_dir / (component_id + '.json')} to reset the "
                "baseline."
            )
        elif all_passed and config.snapshot_on_success:
            save_snapshot(component_id, fixtures, results, snapshot_dir)

    return CheckResult(
        name="fixtures",
        passed=all_passed,
        message=message,
        details=details,
        duration_seconds=time.monotonic() - start,
    )


def check_fixtures_from_prd(
    prd_path: Path,
    cwd: Path,
    config: FixturesConfig,
    component_id: str | None = None,
) -> CheckResult:
    """Phase 1 entry point: load fixtures from the PRD on disk and run them.

    Fails CLOSED on an unreadable or schema-invalid PRD: fixtures are the
    independent oracle against agent-authored tests (H-6), so "could not
    determine which fixtures to run" must never read as "fixtures
    passed". A PRD without a ``fixtures`` key passes vacuously - that is
    a legitimate "none defined", not an infrastructure failure.
    """
    start = time.monotonic()
    try:
        with open(prd_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="fixtures",
            passed=False,
            message=(
                "PRD could not be read for the fixtures check "
                "(failing closed)"
            ),
            details=[f"Error: {exc}"],
            duration_seconds=time.monotonic() - start,
        )
    errors = PRD.validate_schema(data)
    if errors:
        return CheckResult(
            name="fixtures",
            passed=False,
            message=(
                "PRD failed schema validation; fixture definitions cannot "
                "be trusted (failing closed)"
            ),
            details=errors[:10],
            duration_seconds=time.monotonic() - start,
        )
    fixtures = load_fixtures_from_prd_data(data)
    return check_fixtures(fixtures, cwd, config, component_id=component_id)


def load_fixtures_from_prd_data(prd_data: dict[str, Any]) -> list[Fixture]:
    """Parse the optional 'fixtures' array from PRD JSON data.

    Returns an empty list if no fixtures field is present. Malformed
    entries raise ``ValueError`` naming the entry - an oracle that
    silently drops a fixture is a silent degradation, and this codebase
    fails loudly instead. Callers that need full strict validation run
    ``PRD.validate_schema`` first (as ``check_fixtures_from_prd`` does).
    """
    raw_fixtures = prd_data.get("fixtures") or []
    if not isinstance(raw_fixtures, list):
        raise ValueError("'fixtures' must be an array")

    fixtures: list[Fixture] = []
    for i, entry in enumerate(raw_fixtures):
        if not isinstance(entry, dict):
            raise ValueError(f"fixtures[{i}]: must be an object")
        try:
            fixture = Fixture(
                description=entry["description"],
                fixture_type=entry["fixture_type"],
                input_data=entry.get("input_data", {}),
                expected=entry.get("expected", {}),
            )
        except KeyError as exc:
            raise ValueError(
                f"fixtures[{i}]: missing required key {exc.args[0]!r}"
            ) from exc
        fixtures.append(fixture)

    return fixtures


def save_snapshot(
    component_id: str,
    fixtures: list[Fixture],
    results: list[FixtureResult],
    snapshot_dir: Path,
) -> None:
    """Save successful fixture outputs as a JSON snapshot for regression detection.

    Only saves results for fixtures that passed. The snapshot captures the actual
    output so future runs can detect behavioral regressions. Written
    atomically (mkstemp + os.replace) per the codebase convention.
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

    fd, tmp_name = tempfile.mkstemp(
        dir=snapshot_dir, prefix=f".{component_id}-", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(snapshot_data, indent=2) + "\n")
        os.replace(tmp_name, snapshot_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
