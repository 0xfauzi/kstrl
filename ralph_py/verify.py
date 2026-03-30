"""Phase 1: Mechanical verification - independent checks after agent execution."""

from __future__ import annotations

import os
import py_compile
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ralph_py import git
from ralph_py.guards import path_is_allowed
from ralph_py.prd import PRD


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class VerificationResult:
    """Aggregated result of all mechanical checks."""

    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    def as_context(self) -> str:
        """Format failures for injection into retry prompt."""
        lines: list[str] = []
        for check in self.checks:
            if not check.passed:
                lines.append(f"- {check.name}: FAIL - {check.message}")
                for detail in check.details[:10]:
                    lines.append(f"  {detail}")
        return "\n".join(lines)


@dataclass
class VerifyConfig:
    """Configuration for mechanical verification."""

    test_command: str | None = None
    typecheck_command: str | None = None
    lint_command: str | None = None
    check_diff_scope: bool = True
    check_bad_patterns: bool = True
    subprocess_timeout: float = 300.0

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            test_command=os.environ.get("RALPH_VERIFY_TEST_CMD"),
            typecheck_command=os.environ.get("RALPH_VERIFY_TYPECHECK_CMD"),
            lint_command=os.environ.get("RALPH_VERIFY_LINT_CMD"),
            subprocess_timeout=float(
                os.environ.get("RALPH_TIMEOUT_VERIFY", "300")
            ),
        )


# Patterns that suggest secrets in source code
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                     # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),                  # OpenAI/Stripe key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                  # GitHub PAT
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),  # Private keys
    re.compile(r"xox[bpoas]-[a-zA-Z0-9-]+"),             # Slack tokens
]


def check_prd_stories(prd_path: Path) -> CheckResult:
    """Re-read PRD from disk and verify all stories have passes=true."""
    start = time.monotonic()
    try:
        prd = PRD.load(prd_path)
    except Exception as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"Failed to load PRD: {exc}",
            duration_seconds=time.monotonic() - start,
        )

    failing = [s for s in prd.user_stories if not s.passes]
    if failing:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"{len(failing)} stories not marked as passing",
            details=[f"{s.id}: {s.title}" for s in failing],
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="prd_stories",
        passed=True,
        message=f"All {len(prd.user_stories)} stories passing",
        duration_seconds=time.monotonic() - start,
    )


def check_test_suite(
    cwd: Path, command: str | None = None, timeout: float = 300.0,
) -> CheckResult:
    """Run the project's test suite independently."""
    start = time.monotonic()
    cmd = command or "uv run pytest"

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="test_suite",
            passed=False,
            message=f"Test suite timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        last_lines = output.splitlines()[-50:] if output else []
        return CheckResult(
            name="test_suite",
            passed=False,
            message=f"Tests failed (exit code {result.returncode})",
            details=last_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="test_suite",
        passed=True,
        message="Tests passed",
        duration_seconds=time.monotonic() - start,
    )


def check_typecheck(
    cwd: Path, command: str | None = None, timeout: float = 300.0,
) -> CheckResult:
    """Run typecheck independently."""
    start = time.monotonic()
    cmd = command or "uv run mypy ."

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="typecheck",
            passed=False,
            message=f"Typecheck timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        last_lines = output.splitlines()[-30:] if output else []
        return CheckResult(
            name="typecheck",
            passed=False,
            message=f"Typecheck failed (exit code {result.returncode})",
            details=last_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="typecheck",
        passed=True,
        message="Typecheck passed",
        duration_seconds=time.monotonic() - start,
    )


def check_linter(
    cwd: Path, command: str | None = None, timeout: float = 300.0,
) -> CheckResult:
    """Run linter independently."""
    start = time.monotonic()
    cmd = command or "uv run ruff check ."

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="linter",
            passed=False,
            message=f"Linter timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        last_lines = output.splitlines()[-30:] if output else []
        return CheckResult(
            name="linter",
            passed=False,
            message=f"Linter failed (exit code {result.returncode})",
            details=last_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="linter",
        passed=True,
        message="Linter passed",
        duration_seconds=time.monotonic() - start,
    )


def check_diff_scope(
    cwd: Path,
    base_branch: str,
    allowed_paths: list[str] | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope."""
    start = time.monotonic()

    if not allowed_paths:
        return CheckResult(
            name="diff_scope",
            passed=True,
            message="No scope constraints (allowed_paths not set)",
            duration_seconds=time.monotonic() - start,
        )

    changed = git.get_diff_names(base_branch, cwd)
    violations = [f for f in changed if not path_is_allowed(f, allowed_paths)]

    if violations:
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=f"{len(violations)} files outside allowed scope",
            details=violations[:20],
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="diff_scope",
        passed=True,
        message=f"{len(changed)} files, all within scope",
        duration_seconds=time.monotonic() - start,
    )


def check_bad_patterns(cwd: Path, base_branch: str) -> CheckResult:
    """Scan changed files for obvious problems."""
    start = time.monotonic()
    issues: list[str] = []

    changed = git.get_diff_names(base_branch, cwd)
    py_files = [f for f in changed if f.endswith(".py")]

    for rel_path in py_files:
        full_path = cwd / rel_path
        if not full_path.exists():
            continue

        # Empty file check
        content = full_path.read_text()
        if not content.strip():
            issues.append(f"{rel_path}: empty file")
            continue

        # Syntax check
        try:
            py_compile.compile(str(full_path), doraise=True)
        except py_compile.PyCompileError as exc:
            issues.append(f"{rel_path}: syntax error - {exc}")
            continue

        # Secret patterns
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                issues.append(f"{rel_path}: possible secret/credential detected")
                break

    if issues:
        return CheckResult(
            name="bad_patterns",
            passed=False,
            message=f"{len(issues)} issues found in changed files",
            details=issues,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="bad_patterns",
        passed=True,
        message=f"Scanned {len(py_files)} Python files, no issues",
        duration_seconds=time.monotonic() - start,
    )


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
) -> VerificationResult:
    """Run all 6 mechanical checks. All checks run even if earlier ones fail."""
    checks: list[CheckResult] = []

    checks.append(check_prd_stories(prd_path))

    checks.append(check_test_suite(
        worktree_path, config.test_command, config.subprocess_timeout,
    ))

    checks.append(check_typecheck(
        worktree_path, config.typecheck_command, config.subprocess_timeout,
    ))

    checks.append(check_linter(
        worktree_path, config.lint_command, config.subprocess_timeout,
    ))

    if config.check_diff_scope:
        checks.append(check_diff_scope(
            worktree_path, base_branch, allowed_paths,
        ))

    if config.check_bad_patterns:
        checks.append(check_bad_patterns(worktree_path, base_branch))

    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks)
