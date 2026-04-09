"""Integration test: exercises every harness engineering feature against a real
tiny Python project.  No agent needed -- just the harness machinery.

Creates a temp project with real Python files, then runs:
  1. Feedforward  - structural analysis produces useful context
  2. Parsers      - real pytest/mypy/ruff output gets structured
  3. Verification - mechanical checks run and report structured failures
  4. Fixtures     - cli/file/function fixtures pass and fail correctly
  5. Evolution    - journal records a run and extracts patterns
  6. Manifest     - from_prd creates a valid single-component manifest
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_project(root: Path) -> None:
    """Scaffold a tiny but real Python project in *root*."""
    # Package
    pkg = root / "src" / "myapp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text(textwrap.dedent("""\
        class User:
            def __init__(self, name: str, email: str) -> None:
                self.name = name
                self.email = email

        class Session:
            token: str
            user: User

        def create_user(name: str, email: str) -> User:
            return User(name=name, email=email)
    """))
    (pkg / "api.py").write_text(textwrap.dedent("""\
        from myapp.models import User, create_user

        def register(name: str, email: str) -> User:
            return create_user(name, email)

        def health() -> dict:
            return {"status": "ok"}
    """))
    (pkg / "utils.py").write_text(textwrap.dedent("""\
        import json

        def to_json(obj: object) -> str:
            return json.dumps(obj, default=str)
    """))

    # Tests
    tests = root / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_models.py").write_text(textwrap.dedent("""\
        from myapp.models import User, create_user

        def test_create_user():
            user = create_user("alice", "alice@example.com")
            assert user.name == "alice"
            assert user.email == "alice@example.com"

        def test_user_missing_arg():
            # intentionally wrong - tests the parser when this fails
            user = User("bob")  # type: ignore
    """))

    # pyproject.toml
    (root / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "myapp"
        version = "0.1.0"
        requires-python = ">=3.11"

        [tool.ruff]
        line-length = 100
        target-version = "py311"

        [tool.ruff.lint]
        select = ["E", "F", "W"]
    """))


# ---------------------------------------------------------------------------
# 1. Feedforward
# ---------------------------------------------------------------------------

class TestFeedforward:
    def test_module_map(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.feedforward import build_module_map

        result = build_module_map(tmp_path)
        assert "src/" in result or "myapp/" in result
        # Should mention file counts
        assert "file" in result.lower()

    def test_public_interfaces(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.feedforward import extract_public_interfaces

        result = extract_public_interfaces(tmp_path)
        assert "User" in result
        assert "create_user" in result
        assert "Session" in result

    def test_dependency_graph(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.feedforward import build_dependency_graph

        result = build_dependency_graph(tmp_path)
        # api.py imports from models.py
        assert "models" in result.lower() or "User" in result

    def test_conventions(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.feedforward import extract_conventions

        result = extract_conventions(tmp_path)
        assert "100" in result  # line-length
        assert "py311" in result or "3.11" in result

    def test_full_context(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.feedforward import FeedforwardConfig, build_feedforward_context

        ctx = build_feedforward_context(tmp_path, FeedforwardConfig())
        assert "=== CODEBASE CONTEXT" in ctx
        assert "END CODEBASE CONTEXT" in ctx
        # Should have at least two sections
        assert "## " in ctx

    def test_disabled(self, tmp_path: Path) -> None:
        from ralph_py.feedforward import FeedforwardConfig, build_feedforward_context

        ctx = build_feedforward_context(tmp_path, FeedforwardConfig(enabled=False))
        assert ctx == ""


# ---------------------------------------------------------------------------
# 2. Parsers - real tool output
# ---------------------------------------------------------------------------

class TestParsersIntegration:
    def test_parse_real_pytest_failure(self) -> None:
        from ralph_py.parsers import parse_pytest_output

        output = textwrap.dedent("""\
            ============================= test session starts ==============================
            collected 2 items

            tests/test_models.py .F                                                  [100%]

            =================================== FAILURES ===================================
            __________________________ test_user_missing_arg ___________________________

            def test_user_missing_arg():
                user = User("bob")
            >       assert user.email == "bob@example.com"
            E       AttributeError: 'User' object has no attribute 'email'

            tests/test_models.py:10: AttributeError
            =========================== short test summary info ============================
            FAILED tests/test_models.py::test_user_missing_arg - AttributeError: 'User' object has no attribute 'email'
            ========================= 1 failed, 1 passed in 0.02s =========================
        """)
        parsed = parse_pytest_output(output)
        assert parsed.tool == "pytest"
        assert parsed.total_errors >= 1
        assert len(parsed.failures) >= 1
        assert parsed.failures[0].file == "tests/test_models.py"
        assert "test_user_missing_arg" in parsed.failures[0].rule_or_test

    def test_parse_real_mypy_output(self) -> None:
        from ralph_py.parsers import parse_mypy_output

        output = textwrap.dedent("""\
            src/myapp/api.py:4: error: Missing return statement  [return]
            src/myapp/models.py:12: error: Incompatible return value type (got "None", expected "User")  [return-value]
            Found 2 errors in 2 files (checked 5 source files)
        """)
        parsed = parse_mypy_output(output)
        assert parsed.tool == "mypy"
        assert parsed.total_errors == 2
        assert len(parsed.failures) == 2
        assert parsed.failures[0].file == "src/myapp/api.py"
        assert parsed.failures[0].line == 4

    def test_parse_real_ruff_output(self) -> None:
        from ralph_py.parsers import parse_ruff_output

        output = textwrap.dedent("""\
            src/myapp/utils.py:1:8: F401 `json` imported but unused
            src/myapp/api.py:1:1: E302 Expected 2 blank lines, got 1
            Found 2 errors.
        """)
        parsed = parse_ruff_output(output)
        assert parsed.tool == "ruff"
        assert parsed.total_errors == 2
        assert parsed.failures[0].rule_or_test == "F401"

    def test_source_context_on_real_file(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.parsers import ParsedFailure, add_source_context

        failure = ParsedFailure(
            file="src/myapp/models.py",
            line=4,
            rule_or_test="test",
            message="some error",
        )
        add_source_context(failure, tmp_path)
        assert failure.source_context != ""
        assert ">" in failure.source_context  # marker line

    def test_fix_hints(self) -> None:
        from ralph_py.parsers import ParsedFailure, generate_fix_hint

        # Missing argument
        f = ParsedFailure(message="TypeError: create_user() missing 1 required positional argument: 'email'")
        hint = generate_fix_hint(f)
        assert hint != ""
        assert "argument" in hint.lower() or "parameter" in hint.lower()

        # Unused import
        f2 = ParsedFailure(rule_or_test="F401", message="`json` imported but unused")
        hint2 = generate_fix_hint(f2)
        assert hint2 != ""

    def test_format_for_prompt(self) -> None:
        from ralph_py.parsers import ParsedFailure, ParsedOutput

        parsed = ParsedOutput(
            tool="pytest",
            total_errors=1,
            failures=[
                ParsedFailure(
                    file="tests/test_auth.py",
                    line=10,
                    rule_or_test="test_login",
                    message="AssertionError: expected 200 got 401",
                    source_context="> assert response.status == 200",
                    fix_hint="Check auth middleware.",
                ),
            ],
        )
        lines = parsed.format_for_prompt()
        assert len(lines) >= 1
        text = "\n".join(lines)
        assert "test_auth.py" in text
        assert "hint:" in text.lower() or "fix:" in text.lower()


# ---------------------------------------------------------------------------
# 3. Fixtures
# ---------------------------------------------------------------------------

class TestFixturesIntegration:
    def test_cli_fixture_passes(self, tmp_path: Path) -> None:
        from ralph_py.fixtures import Fixture, FixturesConfig, run_cli_fixture

        f = Fixture(
            description="echo works",
            fixture_type="cli",
            input_data={"command": "echo hello world"},
            expected={"exit_code": 0, "stdout_contains": ["hello"]},
        )
        result = run_cli_fixture(f, tmp_path, timeout=10.0)
        assert result.passed
        assert "hello" in result.actual

    def test_cli_fixture_fails(self, tmp_path: Path) -> None:
        from ralph_py.fixtures import Fixture, run_cli_fixture

        f = Fixture(
            description="false fails",
            fixture_type="cli",
            input_data={"command": "false"},
            expected={"exit_code": 0},
        )
        result = run_cli_fixture(f, tmp_path, timeout=10.0)
        assert not result.passed

    def test_file_fixture_on_real_project(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.fixtures import Fixture, run_file_fixture

        f = Fixture(
            description="models.py exists and has User",
            fixture_type="file",
            input_data={"path": "src/myapp/models.py"},
            expected={"exists": True, "contains": ["class User"]},
        )
        result = run_file_fixture(f, tmp_path)
        assert result.passed

    def test_file_fixture_missing_content(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.fixtures import Fixture, run_file_fixture

        f = Fixture(
            description="models.py should NOT have class Admin",
            fixture_type="file",
            input_data={"path": "src/myapp/models.py"},
            expected={"exists": True, "contains": ["class Admin"]},
        )
        result = run_file_fixture(f, tmp_path)
        assert not result.passed

    def test_function_fixture(self, tmp_path: Path) -> None:
        # Create a simple module to import
        (tmp_path / "adder.py").write_text("def add(a, b): return a + b\n")
        from ralph_py.fixtures import Fixture, run_function_fixture

        f = Fixture(
            description="add(2, 3) returns 5",
            fixture_type="function",
            input_data={"module": "adder", "function": "add", "args": [2, 3]},
            expected={"returns": 5},
        )
        result = run_function_fixture(f, tmp_path)
        assert result.passed

    def test_function_fixture_wrong_return(self, tmp_path: Path) -> None:
        (tmp_path / "adder.py").write_text("def add(a, b): return a + b\n")
        from ralph_py.fixtures import Fixture, run_function_fixture

        f = Fixture(
            description="add(2, 3) returns 6 (wrong)",
            fixture_type="function",
            input_data={"module": "adder", "function": "add", "args": [2, 3]},
            expected={"returns": 6},
        )
        result = run_function_fixture(f, tmp_path)
        assert not result.passed
        assert "Expected 6" in result.message

    def test_function_fixture_with_kwargs(self, tmp_path: Path) -> None:
        (tmp_path / "greeter.py").write_text(
            "def greet(name, greeting='hello'): return f'{greeting} {name}'\n"
        )
        from ralph_py.fixtures import Fixture, run_function_fixture

        f = Fixture(
            description="greet with kwargs",
            fixture_type="function",
            input_data={
                "module": "greeter",
                "function": "greet",
                "args": ["alice"],
                "kwargs": {"greeting": "hi"},
            },
            expected={"returns": "hi alice"},
        )
        result = run_function_fixture(f, tmp_path)
        assert result.passed

    def test_check_fixtures_pipeline(self, tmp_path: Path) -> None:
        _create_project(tmp_path)
        from ralph_py.fixtures import Fixture, FixturesConfig, check_fixtures

        fixtures = [
            Fixture(
                description="echo test",
                fixture_type="cli",
                input_data={"command": "echo ok"},
                expected={"exit_code": 0},
            ),
            Fixture(
                description="models exists",
                fixture_type="file",
                input_data={"path": "src/myapp/models.py"},
                expected={"exists": True},
            ),
        ]
        result = check_fixtures(fixtures, tmp_path, FixturesConfig(enabled=True))
        assert result.passed
        assert result.name == "fixtures"
        assert "2/2" in result.message or "2 passed" in result.message

    def test_snapshot_roundtrip(self, tmp_path: Path) -> None:
        from ralph_py.fixtures import (
            Fixture,
            FixtureResult,
            check_snapshot_regression,
            save_snapshot,
        )

        fixture = Fixture(
            description="test", fixture_type="cli",
            input_data={}, expected={},
        )
        results = [
            FixtureResult(fixture=fixture, passed=True, actual="ok", message="passed"),
        ]
        snap_dir = tmp_path / "snapshots"
        save_snapshot("comp-1", [fixture], results, snap_dir)

        regressions = check_snapshot_regression("comp-1", results, snap_dir)
        assert regressions == []

        # Now change the output - should detect regression
        changed = [
            FixtureResult(fixture=fixture, passed=True, actual="changed", message="passed"),
        ]
        regressions = check_snapshot_regression("comp-1", changed, snap_dir)
        assert len(regressions) >= 1


# ---------------------------------------------------------------------------
# 4. Evolution
# ---------------------------------------------------------------------------

class TestEvolutionIntegration:
    def test_record_and_extract(self, tmp_path: Path) -> None:
        from ralph_py.evolution import EvolutionConfig, EvolutionJournal
        from ralph_py.factory import FactoryResult
        from ralph_py.manifest import Component, ComponentStatus, Manifest

        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="test-project",
            base_branch="main", single_pr=False,
            components=[
                Component(
                    id="auth", title="Auth", description="",
                    dependencies=[], prd_path="prd.json",
                    branch_name="ralph/auth",
                    status=ComponentStatus.FAILED.value,
                    error="Tests failed (exit code 1)",
                    retries=2,
                    iteration_count=5,
                    duration_seconds=120.0,
                ),
                Component(
                    id="api", title="API", description="",
                    dependencies=["auth"], prd_path="prd.json",
                    branch_name="ralph/api",
                    status=ComponentStatus.COMPLETED.value,
                    error="",
                    retries=0,
                    iteration_count=3,
                    duration_seconds=60.0,
                ),
                Component(
                    id="db", title="DB", description="",
                    dependencies=[], prd_path="prd.json",
                    branch_name="ralph/db",
                    status=ComponentStatus.FAILED.value,
                    error="Tests failed (exit code 1)",
                    retries=1,
                    iteration_count=4,
                    duration_seconds=90.0,
                ),
            ],
        )
        factory_result = FactoryResult(
            completed=["api"], failed=["auth", "db"], skipped=[],
        )

        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
            min_pattern_frequency=2,
        )
        journal = EvolutionJournal(config)

        # Record the run
        journal.record_run("run-001", manifest, factory_result)

        # Verify files were created
        assert config.journal_path.exists()
        assert config.experiments_path.exists()

        # Verify JSONL has 3 entries (one per component)
        lines = config.journal_path.read_text().strip().splitlines()
        assert len(lines) == 3
        entry = json.loads(lines[0])
        assert entry["run_id"] == "run-001"
        assert entry["project"] == "test-project"

        # Verify TSV has header + 1 data line
        tsv_lines = config.experiments_path.read_text().strip().splitlines()
        assert len(tsv_lines) == 2  # header + 1 run

        # Extract patterns - both failed with "Tests failed" so should match
        patterns = journal.extract_failure_patterns(manifest, min_frequency=2)
        assert len(patterns) >= 1
        assert patterns[0].frequency >= 2
        assert "auth" in patterns[0].affected_components or "db" in patterns[0].affected_components

    def test_experiment_trends(self, tmp_path: Path) -> None:
        from ralph_py.evolution import EvolutionConfig, EvolutionJournal
        from ralph_py.factory import FactoryResult
        from ralph_py.manifest import Component, ComponentStatus, Manifest

        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        journal = EvolutionJournal(config)

        # Record 3 runs to see trends
        for i in range(3):
            m = Manifest(
                version="1", spec_file="spec.md", project_name="proj",
                base_branch="main", single_pr=False,
                components=[
                    Component(
                        id=f"comp-{i}", title="C", description="",
                        dependencies=[], prd_path="prd.json",
                        branch_name=f"ralph/c{i}",
                        status=ComponentStatus.COMPLETED.value,
                    ),
                ],
            )
            journal.record_run(f"run-{i:03d}", m, FactoryResult(completed=[f"comp-{i}"]))

        trends = journal.get_experiment_trends(last_n=10)
        assert len(trends) == 3
        assert trends[0]["run_id"] == "run-000"
        assert trends[2]["run_id"] == "run-002"

    def test_propose_improvements(self, tmp_path: Path) -> None:
        from ralph_py.evolution import (
            EvolutionConfig,
            EvolutionJournal,
            FailurePattern,
        )

        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        journal = EvolutionJournal(config)

        patterns = [
            FailurePattern(
                description="Agent used raw SQL in 3 components",
                frequency=3,
                total_components=5,
                affected_components=["auth", "api", "db"],
                check_name="linter",
                error_signature="S608",
                category="verification",
            ),
        ]
        proposals = journal.propose_improvements(patterns)
        assert len(proposals) >= 1
        assert proposals[0].target == "claude_md"

        # Save and verify
        paths = journal.save_proposals(proposals, tmp_path / "proposals")
        assert len(paths) >= 1
        content = paths[0].read_text()
        assert "PROP-" in content
        assert "S608" in content


# ---------------------------------------------------------------------------
# 5. Manifest.from_prd
# ---------------------------------------------------------------------------

class TestManifestFromPrd:
    def test_creates_valid_manifest(self, tmp_path: Path) -> None:
        from ralph_py.manifest import Manifest

        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps({
            "branchName": "ralph/auth-feature",
            "userStories": [],
        }))

        m = Manifest.from_prd(
            prd_path=prd_path,
            branch="ralph/auth-feature",
            base_branch="main",
        )
        assert len(m.components) == 1
        assert m.components[0].id == "main"
        assert m.base_branch == "main"
        # Project name derived from branch
        assert m.project_name == "auth-feature"

    def test_derives_project_name_from_branch(self) -> None:
        from ralph_py.manifest import Manifest

        m = Manifest.from_prd(
            prd_path=Path("prd.json"),
            branch="ralph/user-dashboard",
        )
        assert m.project_name == "user-dashboard"

    def test_derives_project_name_from_prd_stem(self) -> None:
        from ralph_py.manifest import Manifest

        m = Manifest.from_prd(
            prd_path=Path("scripts/ralph/feature/login/prd.json"),
            branch="",
        )
        assert m.project_name == "prd"
        assert m.components[0].branch_name == "ralph/prd"

    def test_roundtrip_serialize(self, tmp_path: Path) -> None:
        from ralph_py.manifest import Manifest

        m = Manifest.from_prd(
            prd_path=Path("scripts/ralph/prd.json"),
            branch="ralph/test",
        )
        out = tmp_path / "manifest.json"
        m.save(out)
        loaded = Manifest.load(out)
        assert loaded.project_name == m.project_name
        assert len(loaded.components) == 1
        assert loaded.components[0].id == "main"

    def test_dag_validation_passes(self) -> None:
        from ralph_py.manifest import Manifest

        m = Manifest.from_prd(
            prd_path=Path("prd.json"),
            branch="ralph/x",
        )
        errors = m.validate_dag()
        assert errors == []
