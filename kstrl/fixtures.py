"""Approved fixtures - pre-approved input/output pairs for behavioral verification.

Provides behavioral verification independent of agent-generated tests.
Fixtures are defined in the PRD and checked during Phase 1 mechanical
verification when ``[fixtures].enabled`` is set (R7.2; default off per
the roadmap user decision).

Threat model (R7.2 / CRIT-3): the PRD is LLM-emitted, so every fixture
definition is untrusted input. CLI fixtures run in a subprocess with the
R2.6 scrubbed environment and ``shell=False``, so metacharacters in a
PRD-supplied command are literal arguments, never shell syntax. A file
fixture only reads a path inside the worktree. The ``function`` type,
which imported one language's module with kstrl's own interpreter, was
removed by #696 slice 8; a PRD naming it fails schema validation, so the
check fails closed. A cli fixture whose command is a harness the engineer
writes expresses the same oracle in any language.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.config_numbers import check_numbers
from kstrl.fixture_expect import canonical_text, judge
from kstrl.fixtures_snapshot import check_snapshot_regression, save_snapshot
from kstrl.jsonread import read_json_file
from kstrl.prd import _FIXTURE_INPUT_KEYS, PRD
from kstrl.verify import CheckResult, ChildOutputDecodeError, run_scrubbed


@dataclass
class Fixture:
    """A single approved fixture - an input/output pair for behavioral verification."""

    description: str
    fixture_type: str  # "cli", "file"
    input_data: dict[str, Any]  # type-specific input configuration
    expected: dict[str, Any]  # type-specific expected output


@dataclass
class FixtureResult:
    """Result of running a single fixture."""

    fixture: Fixture
    passed: bool
    actual: str = ""
    message: str = ""
    # #227, the same field and the same rule as `verify.CheckResult.measured`,
    # one level down. False when this fixture produced a row having measured
    # NOTHING about the software: the command timed out, or the process could
    # not be launched at all. `check_fixtures` folds these into the one
    # `fixtures` CheckResult with `all()`, so one unmeasured fixture makes the
    # whole row unmeasured - the clearing side has to be the narrow one.
    #
    # A malformed fixture DEFINITION is deliberately still measured=True: it
    # is a stable, reproducible property of the PRD, and its disappearance is
    # a real fix. This field marks the environment failing, not the artifact.
    measured: bool = True


@dataclass
class FixturesConfig:
    """Configuration for the fixtures check (``[fixtures]`` in kstrl.toml).

    ``enabled`` defaults to False (R7.2 user decision 4): fixtures run
    PRD-defined commands, so the operator must opt in explicitly.
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
            enabled=_parse_bool(os.environ.get("KSTRL_FIXTURES_ENABLED")),
            snapshot_on_success=_parse_bool(
                os.environ.get("KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS", "1")
            ),
            snapshot_dir=Path(os.environ.get("KSTRL_FIXTURES_SNAPSHOT_DIR", ".kstrl/snapshots")),
            timeout=float(os.environ.get("KSTRL_FIXTURES_TIMEOUT", "30")),
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
        if "KSTRL_FIXTURES_ENABLED" in os.environ:
            config.enabled = _parse_bool(os.environ["KSTRL_FIXTURES_ENABLED"])
        if "KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS" in os.environ:
            config.snapshot_on_success = _parse_bool(
                os.environ["KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS"]
            )
        if "KSTRL_FIXTURES_SNAPSHOT_DIR" in os.environ:
            config.snapshot_dir = Path(os.environ["KSTRL_FIXTURES_SNAPSHOT_DIR"])
        if "KSTRL_FIXTURES_TIMEOUT" in os.environ:
            config.timeout = float(os.environ["KSTRL_FIXTURES_TIMEOUT"])
        if not config.snapshot_dir.is_absolute():
            config.snapshot_dir = root_dir / config.snapshot_dir
        return check_numbers(config)


def run_cli_fixture(
    fixture: Fixture,
    cwd: Path,
    timeout: float,
) -> FixtureResult:
    """Run a CLI fixture by executing a command and checking output expectations.

    Judges ``expected`` through ``fixture_expect.judge``; ``actual`` is the
    stdout, as canonical JSON when ``stdout_json`` is expected (#632).
    ``input_data.stdin`` is the whole of the command's stdin, empty when
    absent, and never kstrl's own stdin (#632).

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
        result = run_scrubbed(
            argv, cwd=cwd, timeout=timeout, stdin_text=fixture.input_data.get("stdin", "")
        )
    except subprocess.TimeoutExpired:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Command timed out after {timeout}s",
            measured=False,
        )
    # ChildOutputDecodeError shares OSError's clause rather than getting its
    # own: this function is at cyclomatic 13 and cognitive 16, and one more
    # branch fails both pre-commit ratchets (#416, measured). A wider tuple
    # is not a branch. The row already says measured=False, and the
    # exception's own text names the codec, the byte and the offset.
    except (OSError, ChildOutputDecodeError) as exc:
        return FixtureResult(
            fixture=fixture,
            passed=False,
            message=f"Failed to run command: {exc}",
            measured=False,
        )

    observed = {"exit code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    failures = judge("cli", fixture.expected, observed)
    # A snapshot compares `actual` byte for byte, so JSON output is recorded
    # canonical: key order and whitespace are not a change in behaviour.
    stdout = canonical_text(result.stdout) if "stdout_json" in fixture.expected else result.stdout

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


def _fixture_file_text(fixture: Fixture, full_path: Path, rel_path: str) -> str | FixtureResult:
    """The file's text, or the failing result explaining why there is none.

    #320's one site failing both halves of the rule at once. utf-8 is
    pinned because the caller's ``contains`` expectations are substring
    tests, so decoding the same bytes two ways makes the SAME artifact
    pass on one machine and fail on the next with no message saying why.
    The decode gets its own message because this string IS the fixture's
    verdict, where "failed to read" would send the reader to permissions
    that are fine. Not ``errors="replace"``: that lets an assertion answer
    against a character nobody wrote.

    Both rows measure NOTHING (#227). The caller reached here having seen
    the file exist, and its ``contains`` expectations are substring tests
    it could not run: the environment took the file away or handed back
    bytes no expectation could be evaluated against. That is the same
    pair, for the same reason, as ``verify._self_critique_text``.
    """
    try:
        return full_path.read_text(encoding="utf-8")
    except OSError as exc:
        return FixtureResult(
            fixture,
            False,
            message=f"Failed to read file '{rel_path}': {exc}",
            measured=False,
        )
    except UnicodeDecodeError as exc:
        return FixtureResult(
            fixture,
            False,
            message=f"File '{rel_path}' is not valid UTF-8: {exc}",
            measured=False,
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
            message=(f"Path {rel_path!r} must be relative to the worktree with no '..' components"),
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
    content = _fixture_file_text(fixture, full_path, rel_path)
    if isinstance(content, FixtureResult):
        return content
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


#: fixture_type -> its runner. The check below refuses to import a type the
#: PRD validator accepts with no runner here, or a runner for a type it refuses.
_RUNNERS: dict[str, Callable[[Fixture, Path, float], FixtureResult]] = {
    "cli": run_cli_fixture,
    "file": lambda fixture, cwd, _timeout: run_file_fixture(fixture, cwd),
}
if set(_RUNNERS) != set(_FIXTURE_INPUT_KEYS):
    raise RuntimeError(
        f"fixture types {sorted(_FIXTURE_INPUT_KEYS)} and runners {sorted(_RUNNERS)} differ"
    )


def _dispatch_fixture(
    fixture: Fixture,
    cwd: Path,
    timeout: float,
) -> FixtureResult:
    """Dispatch a fixture to the appropriate runner."""
    if fixture.fixture_type in _RUNNERS:
        return _RUNNERS[fixture.fixture_type](fixture, cwd, timeout)
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
            # #227: a vacuous pass, the same shape as diff_scope with no
            # allowed paths. It ran no oracle, so it cannot prove one stopped
            # failing.
            measured=False,
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
            config.snapshot_dir if config.snapshot_dir.is_absolute() else cwd / config.snapshot_dir
        )
        regressions = check_snapshot_regression(
            component_id,
            results,
            snapshot_dir,
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
        # #227: one fixture that measured nothing makes the whole row
        # unmeasured. `all` and not `any`, because this field gates the
        # CLEARING side of the baseline and that side has to be the narrow
        # one: a run in which one fixture timed out cannot prove that another
        # baseline signature went away.
        measured=all(r.measured for r in results),
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
        with open(prd_path, encoding="utf-8") as f:
            data = read_json_file(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return CheckResult(
            name="fixtures",
            passed=False,
            message=("PRD could not be read for the fixtures check (failing closed)"),
            details=[f"Error: {exc}"],
            duration_seconds=time.monotonic() - start,
            # #227: the check could not learn which fixtures to run, so it ran
            # none. Failing closed decides the verdict; measured=False is what
            # stops the absent signatures reading as fixed.
            measured=False,
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
            measured=False,
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
            raise ValueError(f"fixtures[{i}]: missing required key {exc.args[0]!r}") from exc
        fixtures.append(fixture)

    return fixtures
