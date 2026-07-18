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
from ralph_py.parsers import (
    ParsedOutput,
    add_source_context,
    generate_fix_hint,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
)
from ralph_py.prd import PRD


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    parsed: ParsedOutput | None = None


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
    dead_code_cleanup: bool = False
    dead_code_command: str | None = None
    mutation_testing: bool = False
    mutation_threshold: float = 50.0
    mutation_timeout: float = 600.0
    subprocess_timeout: float = 300.0
    # Mechanical enforcement of the engineer prompt's "## Self-Critique"
    # mandate. Off by default to keep this opt-in; set to True (or
    # RALPH_VERIFY_REQUIRE_SELF_CRITIQUE=1) to fail Phase 1 when an
    # iteration's progress.txt entry omits the block.
    require_self_critique: bool = False
    self_critique_min_bullets: int = 3
    progress_file_path: str = "scripts/ralph/progress.txt"

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            test_command=os.environ.get("RALPH_VERIFY_TEST_CMD"),
            typecheck_command=os.environ.get("RALPH_VERIFY_TYPECHECK_CMD"),
            lint_command=os.environ.get("RALPH_VERIFY_LINT_CMD"),
            dead_code_cleanup=os.environ.get("RALPH_DEAD_CODE_CLEANUP", "") == "1",
            dead_code_command=os.environ.get("RALPH_DEAD_CODE_CMD"),
            mutation_testing=os.environ.get("RALPH_MUTATION_TESTING", "") == "1",
            mutation_threshold=float(
                os.environ.get("RALPH_MUTATION_THRESHOLD", "50")
            ),
            mutation_timeout=float(
                os.environ.get("RALPH_MUTATION_TIMEOUT", "600")
            ),
            subprocess_timeout=float(
                os.environ.get("RALPH_TIMEOUT_VERIFY", "300")
            ),
            require_self_critique=os.environ.get(
                "RALPH_VERIFY_REQUIRE_SELF_CRITIQUE", "",
            ) == "1",
            self_critique_min_bullets=int(
                os.environ.get("RALPH_VERIFY_SELF_CRITIQUE_MIN_BULLETS", "3"),
            ),
            progress_file_path=os.environ.get(
                "RALPH_VERIFY_PROGRESS_FILE",
                "scripts/ralph/progress.txt",
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> VerifyConfig:
        """Load verify config with precedence: env > toml > defaults."""
        from ralph_py.config import load_toml_section
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(root_dir / "ralph.toml", "verify")
        if "test_command" in section:
            config.test_command = str(section["test_command"]) or None
        if "typecheck_command" in section:
            config.typecheck_command = str(section["typecheck_command"]) or None
        if "lint_command" in section:
            config.lint_command = str(section["lint_command"]) or None
        if "check_diff_scope" in section:
            config.check_diff_scope = bool(section["check_diff_scope"])
        if "check_bad_patterns" in section:
            config.check_bad_patterns = bool(section["check_bad_patterns"])
        if "dead_code_cleanup" in section:
            config.dead_code_cleanup = bool(section["dead_code_cleanup"])
        if "mutation_testing" in section:
            config.mutation_testing = bool(section["mutation_testing"])
        if "mutation_threshold" in section:
            config.mutation_threshold = float(section["mutation_threshold"])
        if "subprocess_timeout" in section:
            config.subprocess_timeout = float(section["subprocess_timeout"])
        if "require_self_critique" in section:
            config.require_self_critique = bool(section["require_self_critique"])
        if "self_critique_min_bullets" in section:
            config.self_critique_min_bullets = int(
                section["self_critique_min_bullets"]
            )
        if "progress_file_path" in section:
            config.progress_file_path = str(section["progress_file_path"])
        # Env overrides
        env = cls.from_env()
        for f in (
            "test_command", "typecheck_command", "lint_command",
            "dead_code_cleanup", "dead_code_command", "mutation_testing",
            "mutation_threshold", "mutation_timeout", "subprocess_timeout",
            "require_self_critique", "self_critique_min_bullets",
            "progress_file_path",
        ):
            # only override when env-derived value differs from default()
            env_val = getattr(env, f)
            default_val = getattr(cls(), f)
            if env_val != default_val:
                setattr(config, f, env_val)
        return config


# Patterns that suggest secrets in source code
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                     # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),                  # OpenAI/Stripe key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                  # GitHub PAT
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),  # Private keys
    re.compile(r"xox[bpoas]-[a-zA-Z0-9-]+"),             # Slack tokens
]


# Engineer prompt mandates the EXACT heading `## Self-Critique`.
# Accept also `- **Self-Critique:**` (common bullet-in-list form) and
# `## Self Critique` (loose hyphen-space variant). Reject prose like
# "the self-critique above" so we don't false-positive on body text.
# Both forms must START the line after at most a list marker + whitespace.
_SELF_CRITIQUE_HEADING_RE = re.compile(
    r"""^
    (?:
        \#{2,3}\s+                  # H2 / H3: '## ' or '### '
      | [\-*]\s+\*{2}\s*            # '- **' or '* **'
    )
    Self[-\s]Critique
    (?:
        \s*\*{2}                    # '**' (close bold)
      | \s*:                        # ':'
      | \s*\*{2}\s*:                # '**:'
      | \s*$                        # end-of-line
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def check_self_critique(
    progress_path: Path, min_bullets: int = 3,
) -> CheckResult:
    """Confirm the latest progress.txt entry contains a Self-Critique
    block with at least ``min_bullets`` bullet points.

    The check looks at the LAST occurrence of a Self-Critique heading
    (line matching `_SELF_CRITIQUE_HEADING_RE`, e.g. `## Self-Critique`
    or `- **Self-Critique:**`) and counts the bullet lines that follow
    until the next heading or end-of-file.

    Without this mechanical check, the engineer prompt's mandate to
    list >=3 failure modes can silently rot - the only enforcement
    path otherwise is the reviewer noticing, which is unreliable.
    """
    start = time.monotonic()
    try:
        text = progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Could not read progress file: {exc}",
            duration_seconds=time.monotonic() - start,
        )

    lines = text.splitlines()
    # Find the LAST self-critique heading. Walking from the end lets
    # multiple iterations accumulate without earlier ones masking the
    # current iteration's block.
    heading_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _SELF_CRITIQUE_HEADING_RE.match(lines[i]):
            heading_idx = i
            break

    if heading_idx is None:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                "No '## Self-Critique' block found in progress file. "
                "Engineer prompt mandates >=3 failure-mode bullets "
                "before declaring done."
            ),
            duration_seconds=time.monotonic() - start,
        )

    # Count bullets after the heading until the next heading (^##) or
    # the next list-style heading (e.g. - **Learnings:**).
    bullet_count = 0
    bullet_lines: list[str] = []
    for line in lines[heading_idx + 1:]:
        stripped = line.strip()
        # Stop at next major heading
        if stripped.startswith("##"):
            break
        # Stop at the next labeled bullet header (e.g. "- **Interpretations:**")
        if stripped.startswith("- **") and stripped.rstrip(":*").lower().endswith(
            ("**", "**:"),
        ) and "self" not in stripped.lower():
            break
        # Count substantive bullets (require non-trivial content after the marker)
        if stripped.startswith("- ") or stripped.startswith("* "):
            body = stripped[2:].strip()
            if body and not body.lower().startswith(("tbd", "todo", "n/a")):
                bullet_count += 1
                bullet_lines.append(body[:80])

    if bullet_count < min_bullets:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"Self-Critique block has {bullet_count} bullets; "
                f"minimum required is {min_bullets}"
            ),
            details=bullet_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="self_critique",
        passed=True,
        message=f"{bullet_count} failure modes listed",
        duration_seconds=time.monotonic() - start,
    )


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
        parsed = parse_pytest_output(output)
        for failure in parsed.failures:
            add_source_context(failure, cwd)
            if not failure.fix_hint:
                failure.fix_hint = generate_fix_hint(failure)
        return CheckResult(
            name="test_suite",
            passed=False,
            message=f"Tests failed (exit code {result.returncode})",
            details=parsed.format_for_prompt(),
            duration_seconds=time.monotonic() - start,
            parsed=parsed,
        )

    return CheckResult(
        name="test_suite",
        passed=True,
        message="Tests passed",
        duration_seconds=time.monotonic() - start,
    )


def _default_typecheck_command(cwd: Path) -> str:
    """Choose a sensible default mypy invocation for ``cwd``.

    Generic ``uv run mypy .`` is hostile to projects whose pyproject.toml
    deliberately scopes mypy via ``[tool.mypy] files`` or ``packages``:
    the ``.`` argument overrides those settings and pulls in test files
    or vendored code that the project never intended to typecheck. When
    the project has configured its own mypy scope, defer to it by
    invoking ``uv run mypy`` with no path argument (mypy then reads the
    config). When no such config is present, fall back to the broad
    ``uv run mypy .`` so a green-field project still gets coverage.

    This is the Gap 2 fix from the end-to-end factory validation run:
    the factory's verify command was overriding the project's CLAUDE.md
    typecheck contract, leading to Phase 1 failures on diffs that were
    actually fine.
    """
    import tomllib

    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        try:
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError):
            return "uv run mypy ."
        mypy_section = data.get("tool", {}).get("mypy", {})
        if isinstance(mypy_section, dict):
            # Acknowledged edge case: this heuristic does not consult
            # ``[[tool.mypy.overrides]]`` (per-module relaxation) or
            # modules-only configs. If a project relaxes via overrides
            # but doesn't set ``files``/``packages``, the broad
            # ``uv run mypy .`` default would override the relaxation.
            # Real-world rare. Users can always override explicitly via
            # ``--typecheck-command`` or env var.
            if mypy_section.get("files") or mypy_section.get("packages"):
                return "uv run mypy"
    return "uv run mypy ."


def check_typecheck(
    cwd: Path, command: str | None = None, timeout: float = 300.0,
) -> CheckResult:
    """Run typecheck independently."""
    start = time.monotonic()
    cmd = command or _default_typecheck_command(cwd)

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
        parsed = parse_mypy_output(output)
        for failure in parsed.failures:
            add_source_context(failure, cwd)
            if not failure.fix_hint:
                failure.fix_hint = generate_fix_hint(failure)
        return CheckResult(
            name="typecheck",
            passed=False,
            message=f"Typecheck failed (exit code {result.returncode})",
            details=parsed.format_for_prompt(),
            duration_seconds=time.monotonic() - start,
            parsed=parsed,
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
        parsed = parse_ruff_output(output)
        for failure in parsed.failures:
            add_source_context(failure, cwd)
            if not failure.fix_hint:
                failure.fix_hint = generate_fix_hint(failure)
        return CheckResult(
            name="linter",
            passed=False,
            message=f"Linter failed (exit code {result.returncode})",
            details=parsed.format_for_prompt(),
            duration_seconds=time.monotonic() - start,
            parsed=parsed,
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
    allowed_paths_error: str | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope.

    ``allowed_paths_error`` marks an infrastructure failure loading the
    scope configuration (the PRD carrying allowedPaths was missing or
    unparseable). The check then fails CLOSED: the diff cannot be
    proven in-scope, and silently skipping the guard is exactly the
    hole R1.5 closes. This is distinct from ``allowed_paths=None``,
    which means no scope was configured -- a legitimate pass.
    """
    start = time.monotonic()

    if allowed_paths_error:
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                "Scope configuration could not be loaded; failing closed "
                "(infrastructure error, not a diff violation)"
            ),
            details=[
                f"Error: {allowed_paths_error}",
                "The PRD carrying allowedPaths failed to load, so the "
                "diff cannot be proven in-scope. Restore a valid PRD "
                "file; do not treat this as permission to widen the "
                "diff.",
            ],
            duration_seconds=time.monotonic() - start,
        )

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
        # R0.4: name the base branch and the FULL allowed-paths list in the
        # failure details. Without them the retry agent has to guess both;
        # the recorded e2e run guessed `main` as base and reverted
        # base-branch content with `git checkout main -- ...`, failing
        # again. Base branch and allowed paths are single detail entries at
        # the head of the list so VerificationResult.as_context()'s
        # details[:10] slice carries them into the retry prompt verbatim.
        shown = violations[:15]
        violation_lines = [f"  - {v}" for v in shown]
        if len(violations) > len(shown):
            violation_lines.append(
                f"  ... and {len(violations) - len(shown)} more"
            )
        details = [
            f"Base branch: {base_branch} "
            f"(scope is judged on `git diff {base_branch}...HEAD`; "
            f"do NOT `git checkout {base_branch} -- <path>`, revert only "
            "your own out-of-scope commits/edits)",
            f"Allowed paths (complete list): {', '.join(allowed_paths)}",
            # One multi-line entry so as_context()'s details[:10] slice
            # cannot drop violations or the truncation marker.
            "Files outside allowed scope:\n" + "\n".join(violation_lines),
        ]
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                f"{len(violations)} files outside allowed scope "
                f"(diff vs base branch '{base_branch}')"
            ),
            details=details,
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


def check_mutation_score(
    cwd: Path,
    base_branch: str,
    threshold: float = 50.0,
    timeout: float = 600.0,
) -> CheckResult:
    """Run mutation testing on changed files using mutmut.

    Only mutates Python files changed relative to base_branch.
    Returns FAIL if mutation score is below threshold.
    Requires mutmut to be installed (pip install mutmut).
    """
    import shutil

    start = time.monotonic()

    if not shutil.which("mutmut"):
        return CheckResult(
            name="mutation_testing",
            passed=True,
            message="mutmut not installed, skipping",
            duration_seconds=time.monotonic() - start,
        )

    changed = git.get_diff_names(base_branch, cwd)
    py_files = [f for f in changed if f.endswith(".py") and not f.startswith("test")]
    if not py_files:
        return CheckResult(
            name="mutation_testing",
            passed=True,
            message="No non-test Python files changed",
            duration_seconds=time.monotonic() - start,
        )

    # Run mutmut on changed files only
    paths_arg = " ".join(py_files)
    try:
        result = subprocess.run(
            f"mutmut run --paths-to-mutate={paths_arg} --no-progress",
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="mutation_testing",
            passed=True,
            message=f"Mutation testing timed out after {timeout}s, skipping",
            duration_seconds=time.monotonic() - start,
        )

    # Parse mutmut results
    try:
        results_proc = subprocess.run(
            "mutmut results",
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = results_proc.stdout
    except subprocess.TimeoutExpired:
        output = result.stdout

    # Parse score from mutmut junitxml or text output
    killed = 0
    survived = 0
    for line in (result.stdout + result.stderr + output).splitlines():
        lower = line.lower().strip()
        if "killed" in lower:
            parts = lower.split()
            for i, p in enumerate(parts):
                if p == "killed" and i > 0:
                    try:
                        killed = int(parts[i - 1])
                    except ValueError:
                        pass
        if "survived" in lower:
            parts = lower.split()
            for i, p in enumerate(parts):
                if p == "survived" and i > 0:
                    try:
                        survived = int(parts[i - 1])
                    except ValueError:
                        pass

    total = killed + survived
    if total == 0:
        return CheckResult(
            name="mutation_testing",
            passed=True,
            message="No mutations generated",
            duration_seconds=time.monotonic() - start,
        )

    score = (killed / total) * 100
    details = [
        f"Killed: {killed}, Survived: {survived}, Total: {total}",
        f"Score: {score:.1f}% (threshold: {threshold}%)",
    ]

    if score < threshold:
        return CheckResult(
            name="mutation_testing",
            passed=False,
            message=f"Mutation score {score:.1f}% below threshold {threshold}%",
            details=details,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="mutation_testing",
        passed=True,
        message=f"Mutation score {score:.1f}% (threshold: {threshold}%)",
        details=details,
        duration_seconds=time.monotonic() - start,
    )


def check_dead_code(
    cwd: Path,
    base_branch: str,
    command: str | None = None,
    timeout: float = 300.0,
) -> CheckResult:
    """Remove dead code with ruff auto-fix, then detect remaining dead code with vulture.

    Two-phase approach:
    1. ruff --fix --select F401,F811,F841 auto-removes unused imports, redefined
       unused names, and unused local variables. Changes are staged and committed.
    2. vulture scans for deeper dead code (unreachable functions, unused classes,
       unused attributes). If a custom command is provided, it runs instead.

    If ruff fixes anything, those fixes are committed automatically so the worktree
    stays clean for subsequent checks. Vulture findings (if any) are reported as
    failures for the agent to fix on retry.
    """
    import shutil

    start = time.monotonic()

    # --- Phase A: ruff auto-fix for unused imports/variables ---
    ruff_cmd = "ruff check --fix --select F401,F811,F841 ."
    ruff_fixed_count = 0

    if shutil.which("ruff"):
        try:
            ruff_result = subprocess.run(
                ruff_cmd, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=timeout,
            )
            # Count fixes from ruff output (lines like "Found X errors (Y fixed, ...)")
            for line in (ruff_result.stdout + ruff_result.stderr).splitlines():
                if "fixed" in line.lower():
                    import re as _re
                    match = _re.search(r"(\d+)\s+fix", line.lower())
                    if match:
                        ruff_fixed_count = int(match.group(1))
        except subprocess.TimeoutExpired:
            pass  # Non-fatal: continue to vulture

        # If ruff made changes, stage and commit them
        if ruff_fixed_count > 0:
            try:
                # Stage all changes ruff made
                subprocess.run(
                    "git add -A", shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=30,
                )
                subprocess.run(
                    'git commit -m "chore: auto-remove dead code (ruff F401/F811/F841)"',
                    shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                pass  # Non-fatal

    # --- Phase B: vulture or custom dead code detection ---
    if command:
        # User-provided dead code detection command
        detect_cmd = command
    elif shutil.which("vulture"):
        # Default: vulture on changed Python files only
        changed = git.get_diff_names(base_branch, cwd)
        py_files = [f for f in changed if f.endswith(".py") and not f.startswith("test")]
        if not py_files:
            msg = f"No dead code issues (ruff auto-fixed {ruff_fixed_count})"
            return CheckResult(
                name="dead_code",
                passed=True,
                message=msg,
                duration_seconds=time.monotonic() - start,
            )
        detect_cmd = f"vulture {' '.join(py_files)} --min-confidence 80"
    else:
        # Neither vulture nor custom command available
        if ruff_fixed_count > 0:
            return CheckResult(
                name="dead_code",
                passed=True,
                message=f"ruff auto-fixed {ruff_fixed_count} issues (vulture not installed)",
                duration_seconds=time.monotonic() - start,
            )
        return CheckResult(
            name="dead_code",
            passed=True,
            message="Skipped: neither vulture nor custom command available",
            duration_seconds=time.monotonic() - start,
        )

    try:
        result = subprocess.run(
            detect_cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="dead_code",
            passed=True,
            message=f"Dead code scan timed out after {timeout}s, skipping",
            duration_seconds=time.monotonic() - start,
        )

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 and output:
        # vulture returns exit code 1 when it finds dead code
        lines = output.splitlines()
        # Filter out common false positives (e.g., __all__, __init__)
        real_issues = [
            line for line in lines
            if line.strip()
            and not line.strip().startswith("#")
            and "__all__" not in line
        ]
        if real_issues:
            prefix = f"ruff auto-fixed {ruff_fixed_count}, " if ruff_fixed_count else ""
            return CheckResult(
                name="dead_code",
                passed=False,
                message=f"{prefix}{len(real_issues)} dead code issues remaining",
                details=real_issues[:20],
                duration_seconds=time.monotonic() - start,
            )

    msg_parts: list[str] = []
    if ruff_fixed_count:
        msg_parts.append(f"ruff auto-fixed {ruff_fixed_count}")
    msg_parts.append("no remaining dead code")
    return CheckResult(
        name="dead_code",
        passed=True,
        message=", ".join(msg_parts),
        duration_seconds=time.monotonic() - start,
    )


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
    allowed_paths_error: str | None = None,
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
            allowed_paths_error=allowed_paths_error,
        ))

    if config.check_bad_patterns:
        checks.append(check_bad_patterns(worktree_path, base_branch))

    if config.dead_code_cleanup:
        checks.append(check_dead_code(
            worktree_path, base_branch,
            config.dead_code_command, config.subprocess_timeout,
        ))

    if config.mutation_testing:
        checks.append(check_mutation_score(
            worktree_path, base_branch,
            config.mutation_threshold, config.mutation_timeout,
        ))

    if config.require_self_critique:
        progress_path = worktree_path / config.progress_file_path
        checks.append(check_self_critique(
            progress_path, config.self_critique_min_bullets,
        ))

    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks)
