"""Phase 1: Mechanical verification - independent checks after agent execution."""

from __future__ import annotations

import os
import py_compile
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git, licensing

if TYPE_CHECKING:
    from kstrl.fixtures import FixturesConfig
from kstrl.adequacy import (
    AdequacyConfig,
    evaluate_layer0,
    is_test_path,
    layer0_blocks,
)
from kstrl.config import component_progress_path, relative_to_root
from kstrl.findings import Finding
from kstrl.gateparse import (
    GATE_LINT,
    GATE_TEST,
    GATE_TYPECHECK,
    parse_gate_output,
    validate_tool,
)
from kstrl.guards import path_is_allowed
from kstrl.parsers import (
    ParsedOutput,
    add_source_context,
    generate_fix_hint,
)
from kstrl.policy import (
    PolicyConfig,
    PolicyConfigError,
    PolicyViolation,
    classify_license,
    evaluate_policy,
)
from kstrl.prd import PRD
from kstrl.statedir import STATE_DIR_NAME

# R2.6 env scrub: verification subprocesses execute agent-authored code
# (the project's tests, linters run over agent files, CLI fixtures), so
# they must never inherit the harness's secrets. Allowlist, not denylist:
# only names below (or matching a prefix below) pass through, everything
# else - ANTHROPIC_API_KEY, OPENAI_API_KEY, cloud credentials, gh tokens -
# is dropped. The set was determined empirically: `uv run pytest` with a
# fresh venv succeeds under env -i with only PATH/HOME/TMPDIR/TERM/LANG
# (uv locates its cache via HOME); the rest are the locale, venv, uv, and
# CPython knobs a project's own commands legitimately consume, plus the
# XDG cache/data paths uv honors when set.
SCRUB_ENV_ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "TERM",
        "VIRTUAL_ENV",
        "CI",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    }
)
SCRUB_ENV_ALLOWED_PREFIXES: tuple[str, ...] = ("LC_", "UV_", "PYTHON")

# Belt over the allowlist's braces: an allowed prefix must never smuggle a
# secret through (UV_PUBLISH_TOKEN matches UV_*). Any name containing one
# of these fragments is dropped even when the allowlist admits it.
_SCRUB_ENV_SENSITIVE_FRAGMENTS: tuple[str, ...] = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)


def scrubbed_subprocess_env() -> dict[str, str]:
    """Allowlist-filtered copy of ``os.environ`` for verification subprocesses."""
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name not in SCRUB_ENV_ALLOWED_NAMES and not name.startswith(SCRUB_ENV_ALLOWED_PREFIXES):
            continue
        if any(frag in name for frag in _SCRUB_ENV_SENSITIVE_FRAGMENTS):
            continue
        env[name] = value
    return env


_SCRUB_TERM_GRACE_SECONDS = 5.0


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal the child's whole process group, direct-child fallback.

    The pid/pgid guards are load-bearing: a mocked Popen's pid coerces to
    1 via ``MagicMock.__index__`` and ``killpg(1, sig)`` is ``kill(-1,
    sig)`` - signal every process this user owns. ``start_new_session=True``
    makes the child its own group leader, so a pgid at or below 1 or equal
    to ours means something is wrong and the group kill must not proceed.
    """
    pid = proc.pid
    try:
        if hasattr(os, "killpg") and isinstance(pid, int) and pid > 1:
            pgid = os.getpgid(pid)
            if pgid > 1 and pgid != os.getpgrp():
                os.killpg(pgid, sig)
                return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass


def run_scrubbed(
    cmd: str | list[str],
    *,
    cwd: Path,
    timeout: float,
    term_grace: float = _SCRUB_TERM_GRACE_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a verification subprocess: scrubbed env, own process group.

    Drop-in for the ``subprocess.run(..., capture_output=True, text=True,
    timeout=...)`` calls verification used to make, with two differences
    (R2.6): the child gets :func:`scrubbed_subprocess_env` instead of the
    harness environment, and on timeout the ENTIRE process group is
    signalled (SIGTERM, grace, SIGKILL) so a test that backgrounds a
    server cannot leak it past the deadline. A string ``cmd`` runs through
    the shell exactly as before; a list does not.

    Raises :class:`subprocess.TimeoutExpired` after the group is dead so
    existing callers' timeout handling keeps working unchanged.
    """
    proc = subprocess.Popen(
        cmd,
        shell=isinstance(cmd, str),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=scrubbed_subprocess_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            pass
        # SIGKILL the group even when the direct child honored SIGTERM: a
        # grandchild that ignored it can hold the pipes open and would
        # otherwise block the drain below indefinitely.
        _signal_process_group(proc, signal.SIGKILL)
        try:
            stdout, stderr = proc.communicate(timeout=term_grace)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    parsed: ParsedOutput | None = None
    # R8.1: typed findings this mechanical check produced, lifted into the
    # component's finding stream by the pipeline so a machine-made gate
    # decision lands in the audit trail (PR body, journal) and not only in
    # the retry context. Empty for checks that emit prose only.
    findings: list[Finding] = field(default_factory=list)


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


def _optional_str(value: object) -> str | None:
    """A toml scalar as a string, with the empty string meaning unset."""
    return str(value) or None


#: Every live ``[verify]`` toml key and how its value is coerced onto the
#: dataclass. A table rather than a per-key ``if``: the chain it replaced
#: was fourteen near-identical branches, and its cyclomatic complexity
#: was already twice the repo's ratchet limit before #258 added three
#: more keys to it. Order is the dataclass's, so a reader can diff the
#: two lists by eye. Anything absent from this table is not a toml key.
_VERIFY_TOML_FIELDS: tuple[tuple[str, Callable[[Any], object]], ...] = (
    ("test_command", _optional_str),
    ("typecheck_command", _optional_str),
    ("lint_command", _optional_str),
    # validate_tool already maps the empty string to None (auto) and
    # raises on anything it does not recognise, so it needs no coercion
    # in front of it.
    ("test_tool", partial(validate_tool, GATE_TEST)),
    ("typecheck_tool", partial(validate_tool, GATE_TYPECHECK)),
    ("lint_tool", partial(validate_tool, GATE_LINT)),
    ("check_diff_scope", bool),
    ("check_bad_patterns", bool),
    ("dead_code_cleanup", bool),
    ("dead_code_command", _optional_str),
    ("mutation_testing", bool),
    ("mutation_threshold", float),
    ("mutation_timeout", float),
    ("subprocess_timeout", float),
    ("require_self_critique", bool),
    ("self_critique_min_bullets", int),
    ("progress_file_path", _optional_str),
)


@dataclass
class VerifyConfig:
    """Configuration for mechanical verification."""

    test_command: str | None = None
    typecheck_command: str | None = None
    lint_command: str | None = None
    # Which parser reads each gate's output (#258). None is auto: every
    # parser registered for the gate runs and their findings are unioned,
    # which is what makes a chained command
    # (`uv run pytest && npm run test`) yield BOTH toolchains' failures.
    # Set one to pin the gate to a single parser. Accepted values are
    # kstrl.gateparse.GATE_TOOLS[<gate>]; anything else raises on load.
    test_tool: str | None = None
    typecheck_tool: str | None = None
    lint_tool: str | None = None
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
    # KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE=1) to fail Phase 1 when an
    # iteration's progress.txt entry omits the block.
    require_self_critique: bool = False
    self_critique_min_bullets: int = 3
    # Where check_self_critique looks for the engineer's progress log.
    # None (the default) derives it from the component's PRD
    # (config.component_progress_path), which is where the engineer was
    # actually told to write and the only location inside the
    # component's allowedPaths. An explicit value wins for every
    # component. It is None-defaulted rather than carrying a separate
    # "was it set?" flag because every scalar field of this dataclass is
    # a documented kstrl.toml key (scripts/gen_docs.py probes for that).
    progress_file_path: str | None = None

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            test_command=os.environ.get("KSTRL_VERIFY_TEST_CMD"),
            typecheck_command=os.environ.get("KSTRL_VERIFY_TYPECHECK_CMD"),
            lint_command=os.environ.get("KSTRL_VERIFY_LINT_CMD"),
            test_tool=validate_tool(GATE_TEST, os.environ.get("KSTRL_VERIFY_TEST_TOOL")),
            typecheck_tool=validate_tool(
                GATE_TYPECHECK, os.environ.get("KSTRL_VERIFY_TYPECHECK_TOOL")
            ),
            lint_tool=validate_tool(GATE_LINT, os.environ.get("KSTRL_VERIFY_LINT_TOOL")),
            dead_code_cleanup=os.environ.get("KSTRL_DEAD_CODE_CLEANUP", "") == "1",
            dead_code_command=os.environ.get("KSTRL_DEAD_CODE_CMD"),
            mutation_testing=os.environ.get("KSTRL_MUTATION_TESTING", "") == "1",
            mutation_threshold=float(os.environ.get("KSTRL_MUTATION_THRESHOLD", "50")),
            mutation_timeout=float(os.environ.get("KSTRL_MUTATION_TIMEOUT", "600")),
            subprocess_timeout=float(os.environ.get("KSTRL_TIMEOUT_VERIFY", "300")),
            require_self_critique=os.environ.get("KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE", "") == "1",
            self_critique_min_bullets=int(
                os.environ.get("KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS", "3"),
            ),
            progress_file_path=os.environ.get("KSTRL_VERIFY_PROGRESS_FILE"),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> VerifyConfig:
        """Load verify config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "verify")
        for key, coerce in _VERIFY_TOML_FIELDS:
            if key in section:
                setattr(config, key, coerce(section[key]))
        # Env overrides. Each var is applied only when it is explicitly
        # set in the environment: the previous compare-against-default
        # heuristic silently dropped an env value that happened to equal
        # the dataclass default (e.g. KSTRL_MUTATION_THRESHOLD=50 could
        # not override a toml mutation_threshold), breaking the
        # env-beats-toml precedence contract (R2.1).
        env = cls.from_env()
        env_var_to_field = {
            "KSTRL_VERIFY_TEST_CMD": "test_command",
            "KSTRL_VERIFY_TYPECHECK_CMD": "typecheck_command",
            "KSTRL_VERIFY_LINT_CMD": "lint_command",
            "KSTRL_VERIFY_TEST_TOOL": "test_tool",
            "KSTRL_VERIFY_TYPECHECK_TOOL": "typecheck_tool",
            "KSTRL_VERIFY_LINT_TOOL": "lint_tool",
            "KSTRL_DEAD_CODE_CLEANUP": "dead_code_cleanup",
            "KSTRL_DEAD_CODE_CMD": "dead_code_command",
            "KSTRL_MUTATION_TESTING": "mutation_testing",
            "KSTRL_MUTATION_THRESHOLD": "mutation_threshold",
            "KSTRL_MUTATION_TIMEOUT": "mutation_timeout",
            "KSTRL_TIMEOUT_VERIFY": "subprocess_timeout",
            "KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE": "require_self_critique",
            "KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS": "self_critique_min_bullets",
            "KSTRL_VERIFY_PROGRESS_FILE": "progress_file_path",
        }
        for env_var, field_name in env_var_to_field.items():
            if env_var in os.environ:
                setattr(config, field_name, getattr(env, field_name))
        return config


# Patterns that suggest secrets in source code
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI/Stripe key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),  # GitHub PAT
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),  # Private keys
    re.compile(r"xox[bpoas]-[a-zA-Z0-9-]+"),  # Slack tokens
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

# An iteration entry boundary in progress.txt. The engineer prompt's
# documented format starts each appended entry with
# `## [YYYY-MM-DD] - [Story ID]`; agents also commonly write
# `## Iteration N`. Exactly two hashes: H3 sub-headings inside an
# entry must not be mistaken for a new entry.
_ITERATION_HEADING_RE = re.compile(
    r"""^\#\#\s+
    (?:
        \[?\d{4}-\d{2}-\d{2}        # '## [YYYY-MM-DD] - ...' (documented form)
      | Iteration\b                 # '## Iteration N' (loose variant)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An UNINDENTED bullet opening with a closed bold label, e.g.
# `- **Learnings:**` or `- **Interpretations** (only if ...): ...`.
# In the engineer prompt's entry format these are sibling sections of
# `- **Self-Critique:**`, so one of them terminates the bullet count.
# Applied to the raw line: the Self-Critique block's own nested bullets
# are indented and therefore never match.
_SECTION_BULLET_RE = re.compile(r"^[\-*]\s+\*{2}[^*]+\*{2}")

# Thematic break: the engineer prompt's entry format ends each entry
# with `---`.
_ENTRY_SEPARATOR_RE = re.compile(r"^-{3,}$")


def check_self_critique(
    progress_path: Path,
    min_bullets: int = 3,
) -> CheckResult:
    """Confirm the CURRENT (latest) progress.txt entry contains a
    Self-Critique block with at least ``min_bullets`` bullet points.

    Shape check only (H4): this verifies that a Self-Critique block of
    the right shape exists in the right place. It does NOT verify the
    substance of the bullets - vacuous-but-plausible failure modes
    pass. Substance is the reviewer's job.

    Format assumption (from the engineer prompt's Progress Format):
    each iteration appends an entry starting with an H2 heading of the
    form `## [YYYY-MM-DD] - [Story ID]` (the loose `## Iteration N`
    variant is also recognized), containing `- **Self-Critique:**` (or
    `## Self-Critique`) followed by bullets, sibling bold-label
    sections such as `- **Interpretations:**`, and a closing `---`.

    The check first locates the latest iteration boundary (the LAST
    line matching ``_ITERATION_HEADING_RE``), then requires a
    Self-Critique heading within that entry - a block written by an
    EARLIER iteration does not satisfy the check for the current one.
    If no iteration heading exists anywhere, the whole file is treated
    as a single entry (fallback for free-form progress files; per-
    iteration association is not possible there).

    Bullet counting stops at the next `##` heading, a `---` entry
    separator, or an unindented bold-label bullet (a sibling section
    like `- **Interpretations:**`), so bullets belonging to later
    sections do not inflate the count. Consequence of the format
    assumption: critique bullets themselves must either be indented
    under the `- **Self-Critique:**` bullet (the documented format) or
    not open with a bold label, otherwise they read as a sibling
    section and the check fails loudly rather than over-counting.

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
    # Locate the latest iteration entry: entries are appended, so the
    # LAST iteration heading starts the current iteration's entry.
    entry_start = 0
    entry_found = False
    for i in range(len(lines) - 1, -1, -1):
        if _ITERATION_HEADING_RE.match(lines[i]):
            entry_start = i
            entry_found = True
            break

    # Find the LAST self-critique heading WITHIN the latest entry, so
    # an earlier iteration's block cannot satisfy the current one and
    # repeated blocks inside one entry resolve to the newest.
    heading_idx: int | None = None
    for i in range(len(lines) - 1, entry_start - 1, -1):
        if _SELF_CRITIQUE_HEADING_RE.match(lines[i]):
            heading_idx = i
            break

    if heading_idx is None:
        where = (
            f"in the latest iteration entry (line {entry_start + 1}: "
            f"{lines[entry_start].strip()[:60]!r})"
            if entry_found
            else "in progress file"
        )
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"No '## Self-Critique' block found {where}. "
                "Engineer prompt mandates >=3 failure-mode bullets "
                "before declaring done."
            ),
            duration_seconds=time.monotonic() - start,
        )

    # Count bullets after the heading until the entry's content ends:
    # next `##` heading, `---` separator, or a sibling bold-label
    # bullet section (e.g. `- **Interpretations:**`).
    bullet_count = 0
    bullet_lines: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        # Stop at next major heading
        if stripped.startswith("##"):
            break
        # Stop at the entry separator
        if _ENTRY_SEPARATOR_RE.match(stripped):
            break
        # Stop at the next sibling section: an UNINDENTED bold-label
        # bullet (matched on the raw line so the block's own indented
        # bullets never terminate the count).
        if _SECTION_BULLET_RE.match(line):
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
                f"Self-Critique block has {bullet_count} bullets; minimum required is {min_bullets}"
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


def _tamper_changes(prd: PRD, pre_run_prd_path: Path | None) -> list[str]:
    """How ``prd`` differs from the pre-run copy in ways no engineer may.

    Defence in depth for #264's carve-out, kept deliberately after #269
    made the SCOPE half of this comparison unnecessary. The plan-time
    snapshot (``kstrl.scope``) settles what a component may write, so an
    ``allowedPaths`` the agent edits is inert and is no longer compared:
    see that module for why comparing a value the agent can rewrite is
    the weaker answer.

    What the snapshot does NOT cover is everything else that reads this
    file, and a lot does: ``check_prd_stories`` below, the approved
    fixtures oracle, the acceptance criteria handed to the reviewer, the
    R10.3 set-point sensor. None can be served from a snapshot, because
    the agent setting ``passes`` is the whole job, so the live file has
    to be trusted and a comparison is the only answer available for it.
    Drop this and an agent can delete an acceptance criterion or neuter
    an executable oracle and pass a gate it authored.

    WHICH fields are the engineer's to write is the PRD's own business
    and lives in ``PRD.tamper_changes``, beside the fields it pins.

    Empty when there is nothing to compare, which is not the same as
    waving something through:

    - ``pre_run_prd_path`` is None. The caller has no trustworthy copy
      to offer: ``ks sense`` judges an operator's own working tree.
    - The pre-run copy will not load. A harness or operator condition,
      not something an agent can arrange from inside its worktree.
    - It is the SAME file, which is ``use_worktrees=False``: both reads
      return the same document and the comparison is empty by
      arithmetic rather than by a special case. That mode has no
      isolation boundary, so this check cannot be what gives it one.
      Scope is the part of the answer that does survive there, because
      #269 reads it before the agent starts.
    """
    if pre_run_prd_path is None:
        return []
    try:
        pre_run = PRD.load(pre_run_prd_path)
    except (OSError, ValueError):
        return []
    return prd.tamper_changes(pre_run)


def check_prd_stories(prd_path: Path, pre_run_prd_path: Path | None = None) -> CheckResult:
    """Re-read PRD from disk and verify all stories have passes=true.

    ``pre_run_prd_path`` (#269) is the copy of the same PRD the run
    started with, which lives outside every worktree and so is not
    agent-writable. Given one, this check also refuses a PRD the
    component rewrote in its own favour (``_tamper_changes``).

    This is the check that carries that refusal, rather than
    ``diff_scope``, for two reasons. It is a statement about the
    STORIES, which is what this check reads and what a rewrite attacks;
    scope stopped being the question when #269 made the plan-time
    snapshot the only scope source. And ``diff_scope`` is switchable off
    (``[verify] check_diff_scope``), while this one runs whenever there
    is a PRD at all: defence in depth an unrelated toggle can disable is
    not defence in depth.
    """
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

    tampered = _tamper_changes(prd, pre_run_prd_path)
    if tampered:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message="The PRD is not the one this run started with; failing closed",
            details=[
                f"It {'; '.join(tampered)}. A component may set `passes` "
                "and `notes` on its own stories and nothing else: it may "
                "not rewrite the criteria or the fixtures it is judged "
                "against.",
                "Every gate that reads this file - these stories, the "
                "approved fixtures, the criteria the reviewer is given - "
                "is judging a document the component rewrote. Restore it "
                "to what the run started with; do not treat this as "
                "permission to change what the component is measured "
                "against.",
            ],
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


# ---------------------------------------------------------------------------
# Resolved verification commands (#261)
# ---------------------------------------------------------------------------
#
# The single source of truth for "what will Phase 1 actually run". Both
# the gate (``check_test_suite`` / ``check_typecheck`` / ``check_linter``)
# and the engineer prompt (``loop.run_loop``) answer that question by
# calling the resolvers below, so the agent cannot be told a command the
# gate will not run.
#
# ``ks init`` used to scaffold a second, hardcoded copy of these commands
# into the generated CLAUDE.md. Every copy disagreed with the gate from
# the moment init finished, and loop.run_loop prepends CLAUDE.md into the
# engineer prompt, so the harness mechanically fed the agent the wrong
# commands. The copy is gone; this module is the only source.

#: Gate default when ``[verify] test_command`` is unset.
DEFAULT_TEST_COMMAND = "uv run pytest"

#: Gate default when ``[verify] lint_command`` is unset.
DEFAULT_LINT_COMMAND = "uv run ruff check ."

#: Gate fallback when ``[verify] typecheck_command`` is unset AND the
#: project does not scope mypy itself. ``_default_typecheck_command``
#: prefers ``uv run mypy`` (no path) whenever pyproject.toml does.
DEFAULT_TYPECHECK_COMMAND = "uv run mypy ."

#: What ``_default_typecheck_command`` uses instead when the project has
#: scoped mypy via ``[tool.mypy] files`` or ``packages``.
SCOPED_TYPECHECK_COMMAND = "uv run mypy"

# Harness-authored instruction text injected into the engineer prompt on
# every iteration, so it is enrolled in the H3 version/hash snapshot
# (tests/test_prompt_versions.py) exactly like DEFAULT_PROMPT. Only the
# TEMPLATE is snapshotted: the three command values are the operator's,
# interpolated at run time, and H3 cannot and should not pin those.
VERIFY_COMMANDS_PROMPT_VERSION = "1.0.0"

VERIFY_COMMANDS_PROMPT = """\
# Verification Commands (resolved by kstrl)

These are the exact commands kstrl's mechanical verification gate runs on your
work, resolved from this project's `kstrl.toml` `[verify]` section. Run them
yourself before you report a story complete. They are authoritative: ignore any
other verification command list, including one written in the project context
above.

- Test: `{test}`
- Typecheck: `{typecheck}`
- Lint: `{lint}`

A command may chain several toolchains. Run all of it."""


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
    the factory's verify command was overriding the project's own
    typecheck scope, leading to Phase 1 failures on diffs that were
    actually fine. Gap 2 landed on the gate and not on ``ks init``, which
    kept scaffolding ``mypy src/ --strict`` into CLAUDE.md - the very
    shape it identified as wrong. #261 closed that half.
    """
    import tomllib

    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        try:
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError):
            return DEFAULT_TYPECHECK_COMMAND
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
                return SCOPED_TYPECHECK_COMMAND
    return DEFAULT_TYPECHECK_COMMAND


def resolve_test_command(command: str | None) -> str:
    """The exact test command Phase 1 will run."""
    return command or DEFAULT_TEST_COMMAND


def resolve_typecheck_command(command: str | None, cwd: Path) -> str:
    """The exact typecheck command Phase 1 will run in ``cwd``."""
    return command or _default_typecheck_command(cwd)


def resolve_lint_command(command: str | None) -> str:
    """The exact lint command Phase 1 will run."""
    return command or DEFAULT_LINT_COMMAND


@dataclass(frozen=True)
class ResolvedVerifyCommands:
    """The concrete commands Phase 1 runs, after config and defaults.

    Every field is a shell command line, so a chained polyglot command
    (``uv run pytest -q && cd web && npm run test``) survives verbatim:
    the resolver never splits or rewrites what the operator configured.
    """

    test: str
    typecheck: str
    lint: str

    def format_for_prompt(self) -> str:
        """Render the block injected into the engineer prompt.

        Stated as authoritative because an agent working in a project
        scaffolded before #261 may also be shown a stale CLAUDE.md list,
        and has to know which one binds.
        """
        return VERIFY_COMMANDS_PROMPT.format(
            test=self.test,
            typecheck=self.typecheck,
            lint=self.lint,
        )


def resolve_verify_commands(config: VerifyConfig, cwd: Path) -> ResolvedVerifyCommands:
    """Resolve ``config`` against ``cwd`` into the commands Phase 1 runs.

    ``cwd`` is the directory the gate will run in (the component's
    worktree under the factory), because the typecheck default is a
    function of that directory's pyproject.toml.
    """
    return ResolvedVerifyCommands(
        test=resolve_test_command(config.test_command),
        typecheck=resolve_typecheck_command(config.typecheck_command, cwd),
        lint=resolve_lint_command(config.lint_command),
    )


# A CLAUDE.md verification bullet in the shape ``ks init`` used to
# generate: ``- **Test**: `uv run pytest tests/ -v --tb=short```. Matched
# anywhere in the file rather than under a specific heading, because the
# heading text varies ("## Verification Commands", "## Verification
# commands") while the bullet shape does not.
_CLAUDE_MD_COMMAND_RE = re.compile(
    r"^\s*[-*]\s+\*{2}(Test|Typecheck|Lint)\*{2}\s*:\s*`([^`]+)`\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScrubbedProjectContext:
    """CLAUDE.md text with stale verification bullets removed (#261)."""

    text: str
    #: One human-readable line per removed bullet, for ``ui.warn``.
    divergences: list[str]


def scrub_stale_verify_commands(
    claude_md: str,
    commands: ResolvedVerifyCommands,
) -> ScrubbedProjectContext:
    """Drop CLAUDE.md verification bullets that disagree with the gate.

    Projects scaffolded before #261 carry a generated ``## Verification
    Commands`` section whose three bullets disagree with what the gate
    runs. ``loop.run_loop`` prepends CLAUDE.md into the engineer prompt,
    so those bullets are instructions the agent follows and then fails
    Phase 1 on.

    Removal is per-bullet and only when the stated command differs from
    the resolved one, so a project whose CLAUDE.md happens to be correct
    is left byte-identical, and surrounding prose always survives. The
    file on disk is never modified: this scrubs the in-memory copy that
    goes into the prompt, and every removal is reported so the operator
    can delete the stale section for good.
    """
    kept: list[str] = []
    divergences: list[str] = []
    by_label = {
        "test": commands.test,
        "typecheck": commands.typecheck,
        "lint": commands.lint,
    }
    # keepends: what survives is re-joined with "", so a file with CRLF
    # endings or no trailing newline round-trips byte for byte.
    for line in claude_md.splitlines(keepends=True):
        match = _CLAUDE_MD_COMMAND_RE.match(line)
        if match is None:
            kept.append(line)
            continue
        label = match.group(1).lower()
        stated = match.group(2).strip()
        resolved = by_label[label]
        if stated == resolved:
            kept.append(line)
            continue
        divergences.append(
            f"CLAUDE.md tells the agent to {label} with `{stated}`, but the "
            f"gate runs `{resolved}`. Dropping the stale line from the "
            f"engineer prompt; delete it from CLAUDE.md and set [verify] in "
            f"kstrl.toml instead."
        )
    return ScrubbedProjectContext(text="".join(kept), divergences=divergences)


def scrub_project_claude_md(
    root: Path,
    commands: ResolvedVerifyCommands,
) -> ScrubbedProjectContext | None:
    """``scrub_stale_verify_commands`` on ``root``'s CLAUDE.md, or None.

    None when the project has no readable CLAUDE.md. One place decides
    where the file lives and what an unreadable one means, because two
    callers need the same answer for different reasons: the engineer
    loop wants ``.text`` (the copy that goes into the prompt) and the
    factory preflight wants ``.divergences`` (what to tell the
    operator), and they had drifted into two different missing-file
    policies (#261).
    """
    try:
        claude_md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return scrub_stale_verify_commands(claude_md, commands)


def _failed_gate_result(
    name: str,
    message: str,
    parsed: ParsedOutput,
    cmd: str,
    cwd: Path,
    start: float,
) -> CheckResult:
    """Enrich a parse and package it as the gate's failing CheckResult.

    One home for all three gates because the enrichment has to happen at
    every one of them and forgetting a step is SILENT: without
    ``parsed.command`` the prompt label falls back to the parser name,
    which is exactly the #258 mislabel returning unannounced.
    """
    parsed.command = cmd
    for failure in parsed.failures:
        # eslint's default formatter prints ABSOLUTE paths, so without
        # this the engineer is handed a path rooted in kstrl's throwaway
        # worktree: correct on disk, useless as an instruction, and not
        # the path its own tools use. Deliberately the FILE only. The
        # message is prose the tool wrote and may quote a path too
        # (measured: vitest's load errors do); rewriting a tool's own
        # sentences by string substitution is a different and less safe
        # mechanism than resolving a path, and the file is the field the
        # engineer acts on and add_source_context resolves.
        if failure.file:
            failure.file = relative_to_root(Path(failure.file), cwd)
        add_source_context(failure, cwd)
        if not failure.fix_hint:
            failure.fix_hint = generate_fix_hint(failure)
    return CheckResult(
        name=name,
        passed=False,
        message=message,
        details=parsed.format_for_prompt(),
        duration_seconds=time.monotonic() - start,
        parsed=parsed,
    )


def check_test_suite(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run the project's test suite independently.

    ``tool`` pins which parser reads the output; None runs every parser
    registered for the gate and unions what they find (#258).
    """
    start = time.monotonic()
    cmd = resolve_test_command(command)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_TEST,
            passed=False,
            message=f"Test suite timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_TEST,
            f"Tests failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_TEST, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_TEST,
        passed=True,
        message="Tests passed",
        duration_seconds=time.monotonic() - start,
    )


def check_typecheck(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run typecheck independently. See ``check_test_suite`` for ``tool``."""
    start = time.monotonic()
    cmd = resolve_typecheck_command(command, cwd)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_TYPECHECK,
            passed=False,
            message=f"Typecheck timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_TYPECHECK,
            f"Typecheck failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_TYPECHECK, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_TYPECHECK,
        passed=True,
        message="Typecheck passed",
        duration_seconds=time.monotonic() - start,
    )


def check_linter(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run linter independently. See ``check_test_suite`` for ``tool``."""
    start = time.monotonic()
    cmd = resolve_lint_command(command)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_LINT,
            passed=False,
            message=f"Linter timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_LINT,
            f"Linter failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_LINT, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_LINT,
        passed=True,
        message="Linter passed",
        duration_seconds=time.monotonic() - start,
    )


def _diff_scope_details(
    base_branch: str,
    allowed_paths: list[str],
    harness_paths: list[str] | None,
    violations: list[str],
) -> list[str]:
    """Failure details for a diff that left its scope.

    R0.4: name the base branch and the FULL allowed-paths list. Without
    them the retry agent has to guess both; the recorded e2e run guessed
    `main` as base and reverted base-branch content with `git checkout
    main -- ...`, failing again. Base branch and allowed paths are single
    detail entries at the head of the list so
    ``VerificationResult.as_context()``'s ``details[:10]`` slice carries
    them into the retry prompt verbatim.

    #264: the harness carve-out is its own entry, never folded into the
    authored list. The operator has to be able to read what THEY
    authorised, and the retry agent has to know its own PRD and progress
    log are already in scope - telling it to stop writing those is the
    one instruction it cannot obey and still pass ``prd_stories``.
    """
    shown = violations[:15]
    violation_lines = [f"  - {v}" for v in shown]
    if len(violations) > len(shown):
        violation_lines.append(f"  ... and {len(violations) - len(shown)} more")
    harness_note = (
        [
            "Plus harness artifacts (kstrl's own files, already in "
            f"scope, no need to widen allowedPaths): {', '.join(harness_paths)}"
        ]
        if harness_paths
        else []
    )
    return [
        f"Base branch: {base_branch} "
        f"(scope is judged on `git diff {base_branch}...HEAD`; "
        f"do NOT `git checkout {base_branch} -- <path>`, revert only "
        "your own out-of-scope commits/edits)",
        f"Allowed paths (complete list): {', '.join(allowed_paths)}",
        *harness_note,
        # One multi-line entry so as_context()'s details[:10] slice
        # cannot drop violations or the truncation marker.
        "Files outside allowed scope:\n" + "\n".join(violation_lines),
    ]


def check_diff_scope(
    cwd: Path,
    base_branch: str,
    allowed_paths: list[str] | None = None,
    allowed_paths_error: str | None = None,
    harness_paths: list[str] | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope.

    ``allowed_paths_error`` marks a failure to establish a TRUSTWORTHY
    scope at PLAN time (#269): the pre-run PRD carrying allowedPaths was
    missing or unparseable and no run-wide ``--allowed-paths`` supplied
    one. The check then fails CLOSED: the diff cannot be proven
    in-scope, and silently skipping the guard is exactly the hole R1.5
    closes. This is distinct from ``allowed_paths=None``, which means no
    scope was configured -- a legitimate pass.

    It no longer carries PRD TAMPERING. That refusal moved to
    ``check_prd_stories`` when the plan-time snapshot took the scope
    question away from the worktree PRD: the file can still be rewritten
    and the stories still have to be defended, but the scope this check
    enforces is not something the rewrite can reach any more, so saying
    "scope could not be established" about it was untrue.

    ``harness_paths`` (#264) is kstrl's OWN per-component carve-out from
    ``config.component_harness_paths``: exact files kstrl's other checks
    require the agent to write (its PRD, its progress log, the codebase
    map). They widen the effective scope but are reported SEPARATELY, so
    the failure message still shows the operator what they authorised.
    They never create a scope where none was configured: with
    ``allowed_paths`` unset the check still passes unconditionally.
    """
    start = time.monotonic()

    if allowed_paths_error:
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                "Scope configuration could not be trusted; failing closed "
                "(scope-source error, not a diff violation)"
            ),
            details=[
                f"Error: {allowed_paths_error}",
                "The allowedPaths this diff must be judged against could "
                "not be established before the run started, so the diff "
                "cannot be proven in-scope. Restore a valid PRD file "
                "carrying the allowedPaths this component should have, "
                "or set --allowed-paths for the run; do not treat this "
                "as permission to widen the diff.",
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
    # #264: the authored scope plus kstrl's own per-component files. The
    # two lists stay separate all the way into the failure details: an
    # operator reading "outside allowed scope" must be able to tell what
    # they authorised from what the harness added on their behalf.
    #
    # Deliberately NOT guards.check_violations, which is the same
    # decision on the same inputs: it takes a set and returns sorted, and
    # the violation list is truncated to 15 for the retry prompt, so
    # sorting silently changes WHICH violations the retry agent is shown.
    # Git's order is the order the operator sees elsewhere; a cosmetic
    # de-duplication is not worth moving it.
    effective = [*allowed_paths, *(harness_paths or ())]
    violations = [f for f in changed if not path_is_allowed(f, effective)]

    if violations:
        details = _diff_scope_details(
            base_branch,
            allowed_paths,
            harness_paths,
            violations,
        )
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
    """Scan changed files for obvious problems.

    The scan reads the tree and writes nothing into it. The syntax
    check earns that: ``py_compile.compile`` defaults its output to
    ``<dir>/__pycache__/<name>.pyc`` NEXT TO the source, so scanning
    used to leave bytecode behind - noise in the factory's own diff,
    and a write ``ks sense`` (R10.1) promises never to make. Directing
    ``cfile`` at a throwaway directory keeps the ``PyCompileError``
    type and message byte-identical; only the destination moves.
    """
    start = time.monotonic()
    issues: list[str] = []

    changed = git.get_diff_names(base_branch, cwd)
    py_files = [f for f in changed if f.endswith(".py")]

    with tempfile.TemporaryDirectory(prefix="kstrl-bytecode-") as bytecode_dir:
        # One reused destination: the content is never read back, only
        # the compile's success or failure is.
        cfile = os.path.join(bytecode_dir, "scan.pyc")
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
                py_compile.compile(str(full_path), cfile=cfile, doraise=True)
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


def check_policy_envelope(
    cwd: Path,
    base_branch: str,
    config: PolicyConfig,
) -> CheckResult:
    """R8.1: enforce the declarative ``[policy]`` envelope from artifacts.

    Reads the git diff and ``uv.lock`` only, never agent self-report.
    Fails CLOSED on any infrastructure error (diff unreadable, malformed
    policy) and on any envelope violation. Enforcement-machinery edits
    are a non-overridable halt. Violation details are packed as
    individual entries so ``VerificationResult.as_context()``'s
    ``details[:10]`` slice carries them into the retry prompt.
    """
    start = time.monotonic()
    # All three reads are strict: each is a SEPARATE git subprocess, so a
    # successful content read proves nothing about the two that follow.
    # A lenient read returns [] on timeout/nonzero exit, which the
    # evaluator cannot distinguish from "nothing changed" - the change
    # would then satisfy every path and size rule vacuously.
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
        changed = git.get_diff_names(base_branch, cwd, strict=True)
        numstat = git.get_diff_numstat(base_branch, cwd, strict=True)
    except git.GitDiffError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not read the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[
                f"Error: {exc}",
                "The change cannot be proven within policy; do not treat "
                "this as permission to merge.",
            ],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
        )

    try:
        evaluation = evaluate_policy(changed, numstat, diff_text, config)
    except PolicyConfigError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message="policy envelope is misconfigured; failing closed",
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope is misconfigured: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
        )

    # License gate (R8.1): resolve each newly-added uv.lock dependency's
    # license and classify it. Runs only when configured (license_allow
    # non-empty).
    violations = list(evaluation.violations) + _check_licenses(
        evaluation.new_dependencies,
        config,
    )
    blocking = [v for v in violations if v.blocking]
    advisories = [v for v in violations if not v.blocking]

    findings = [
        Finding.policy_violation(
            category=v.category,
            explanation=v.explanation,
            location=v.location,
            severity=v.severity,
            suggestion=v.suggestion,
        )
        for v in violations
    ]
    # Blocking violations first: as_context() slices details[:10] into the
    # retry prompt, and advisories must never crowd out a real failure.
    details = [v.explanation for v in blocking] + [v.explanation for v in advisories]

    if not blocking:
        message = evaluation.summary
        if advisories:
            message += f"; {len(advisories)} advisory(ies)"
        return CheckResult(
            name="policy_envelope",
            passed=True,
            message=message,
            details=details,
            findings=findings,
            duration_seconds=time.monotonic() - start,
        )
    message = f"{len(blocking)} policy violation(s)"
    if evaluation.machinery_hit:
        message += " including enforcement-machinery halt"
    return CheckResult(
        name="policy_envelope",
        passed=False,
        message=message,
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _check_licenses(
    new_dependencies: list[tuple[str, str]],
    config: PolicyConfig,
) -> list[PolicyViolation]:
    """Resolve + classify the licenses of newly-added dependencies.

    Denied (copyleft) and resolved-but-not-allowlisted licenses are
    blocking. A license that no source could resolve is governed by
    ``license_unresolved``: "block" (default, fail-closed - an unprovable
    dependency is not demonstrably inside the envelope) or "advisory".
    No-op when the gate is unconfigured or nothing new was added.
    """
    if not config.license_allow or not new_dependencies:
        return []
    uv_cache = licensing.uv_cache_dir()
    violations: list[PolicyViolation] = []
    for name, version in new_dependencies:
        resolved = licensing.resolve_license(
            name,
            version,
            uv_cache=uv_cache,
            use_pypi=config.license_use_network,
        )
        if resolved is None:
            advisory = config.license_unresolved == "advisory"
            source = (
                "uv cache + PyPI both missed"
                if config.license_use_network
                else "uv cache missed; network resolution disabled"
            )
            violations.append(
                PolicyViolation(
                    category="license_unresolved",
                    location=f"{name} {version}",
                    severity="advisory" if advisory else "high",
                    explanation=(
                        f"license could not be resolved for {name} {version} "
                        f"({source})" + ("; recorded as advisory" if advisory else "")
                    ),
                    suggestion=(
                        "Warm the uv cache (`uv sync`) or allow network "
                        "resolution; set [policy] license_unresolved = "
                        '"advisory" to accept unprovable licenses.'
                    ),
                )
            )
            continue
        verdict = classify_license(
            resolved,
            config.license_allow,
            config.license_deny_partial,
        )
        if verdict == "denied":
            violations.append(
                PolicyViolation(
                    category="license_denied",
                    location=f"{name} {version}",
                    explanation=(f"denied license '{resolved}' for dependency {name} {version}"),
                    suggestion="Drop the dependency or find a permissive alternative.",
                )
            )
        elif verdict == "unknown":
            violations.append(
                PolicyViolation(
                    category="license_not_allowed",
                    location=f"{name} {version}",
                    explanation=(
                        f"license '{resolved}' for {name} {version} is not in license_allow"
                    ),
                    suggestion=(
                        f"Add '{resolved}' to [policy] license_allow if it is "
                        "acceptable for this repo."
                    ),
                )
            )
    return violations


def check_test_adequacy(
    cwd: Path,
    base_branch: str,
    config: AdequacyConfig,
    autonomy_level: int = 0,
) -> CheckResult:
    """R8.5 Layer 0: did this change weaken the suite, and do its new
    tests assert anything falsifiable?

    Reads the diff and the changed test files only - no test execution,
    no coverage, no mutation tooling, no historical data. Fails CLOSED on
    an unreadable diff, like every other artifact-reading check here.

    Advisory unless the level (or an explicit opt-in) says otherwise:
    findings are recorded either way, so switching the gate on later
    starts from evidence rather than a guess.

    File STATUS is read alongside the names: the whole-file oracle floor
    is a rule about NEW test files, and applying it to a file someone
    merely edited would fail a one-line change for oracles that predate
    it. Diff discipline applies to every changed file regardless.
    """
    start = time.monotonic()
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
        records = git.get_diff_name_status(base_branch, cwd, strict=True)
        changed = [path for _, path in records]
    except git.GitDiffError as exc:
        return CheckResult(
            name="test_adequacy",
            passed=False,
            message=(
                "test adequacy could not read the diff; failing closed "
                "(infrastructure error, not an adequacy pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "adequacy",
                    f"adequacy could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
        )

    sources: dict[str, str] = {}
    for rel in changed:
        if not is_test_path(rel) or not rel.endswith(".py"):
            continue
        full = cwd / rel
        if not full.exists():
            continue  # deleted; the diff analysis covers it
        try:
            sources[rel] = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # Only status "A" is new content. A rename/copy destination ("R"/"C")
    # carries tests that already existed, so it is not held to the
    # new-file oracle floor.
    new_paths = {path for status, path in records if status.startswith("A") and path in sources}
    adequacy_findings = evaluate_layer0(
        diff_text,
        sources,
        config,
        new_paths=new_paths,
    )
    blocking = layer0_blocks(config, autonomy_level)
    severity = "high" if blocking else "advisory"
    findings = [
        Finding.adequacy_finding(
            category=str(f.kind),
            explanation=f.render(),
            location=f.path,
            severity=severity,
        )
        for f in adequacy_findings
    ]

    if not adequacy_findings:
        return CheckResult(
            name="test_adequacy",
            passed=True,
            message=(f"test adequacy: {len(sources)} changed test file(s), no weakening signals"),
            duration_seconds=time.monotonic() - start,
        )
    details = [f.render() for f in adequacy_findings]
    mode = "blocking" if blocking else "advisory"
    return CheckResult(
        name="test_adequacy",
        passed=not blocking,
        message=(f"{len(adequacy_findings)} test-adequacy finding(s) [{mode}]"),
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def check_mutation_score(
    cwd: Path,
    base_branch: str,
    threshold: float = 50.0,
    timeout: float = 600.0,
    read_only: bool = False,
) -> CheckResult:
    """Run mutation testing on changed files using mutmut.

    Only mutates Python files changed relative to base_branch.
    Returns FAIL if mutation score is below threshold.
    Requires mutmut to be installed (pip install mutmut).

    ``read_only=True`` (``ks sense``, R10.1) skips the check outright:
    mutmut works by rewriting the source files it mutates, so there is
    no read-only way to run it. The skip is recorded as a passing check
    naming the reason rather than dropped, so the measurement says what
    it did not measure.
    """
    import shutil

    start = time.monotonic()

    if read_only:
        return CheckResult(
            name="mutation_testing",
            passed=True,
            message=(
                "Skipped: mutation testing rewrites the files it mutates and cannot run read-only"
            ),
            duration_seconds=time.monotonic() - start,
        )

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
        result = run_scrubbed(
            f"mutmut run --paths-to-mutate={paths_arg} --no-progress",
            cwd=cwd,
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
        results_proc = run_scrubbed("mutmut results", cwd=cwd, timeout=30)
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
    read_only: bool = False,
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

    ``read_only=True`` (``ks sense``, R10.1) is the whole reason this
    function takes a flag. Phase 1 inside the factory owns its worktree,
    so editing and committing there is free; ``ks sense`` runs against
    the operator's live checkout, where a ``git add -A`` sweeps in every
    unrelated untracked file and the commit moves their HEAD. Read-only
    therefore runs the SAME rule set with ``--no-fix`` (and ``--no-cache``,
    so not even ``.ruff_cache`` appears) and reports what the factory
    WOULD have removed instead of removing it. Nothing is edited, staged
    or committed.

    One divergence worth naming: the factory deletes the ruff-fixable
    subset before vulture looks, so vulture sees a cleaner tree than
    read-only does. A tree whose only dead code is ruff-fixable can
    therefore fail here and pass inside the factory. That is the tree
    being reported honestly, not a bug - but it is a difference.

    A user-supplied ``command`` is run as given in both modes. It is the
    operator's own program, in the same category as ``test_command``;
    kstrl suppresses only its OWN writes.
    """
    import shutil

    start = time.monotonic()

    # --- Phase A: unused imports/variables (ruff F401,F811,F841) ---
    if read_only:
        # --no-fix is explicit rather than implied by omitting --fix: a
        # project can set `fix = true` under [tool.ruff], which turns a
        # bare `ruff check` into a fixing run.
        ruff_cmd = "ruff check --no-fix --no-cache --select F401,F811,F841 ."
    else:
        ruff_cmd = "ruff check --fix --select F401,F811,F841 ."
    ruff_fixed_count = 0
    ruff_pending_count = 0

    if shutil.which("ruff"):
        try:
            ruff_result = run_scrubbed(ruff_cmd, cwd=cwd, timeout=timeout)
            output = ruff_result.stdout + ruff_result.stderr
            if read_only:
                # Nothing was fixed, so count what ruff reported instead:
                # "Found N errors."
                found = re.search(r"Found (\d+) error", output)
                if found:
                    ruff_pending_count = int(found.group(1))
            else:
                # Count fixes from ruff output (lines like "Found X errors (Y fixed, ...)")
                for line in output.splitlines():
                    if "fixed" in line.lower():
                        match = re.search(r"(\d+)\s+fix", line.lower())
                        if match:
                            ruff_fixed_count = int(match.group(1))
        except subprocess.TimeoutExpired:
            pass  # Non-fatal: continue to vulture

        # If ruff made changes, stage and commit them. `not read_only` is
        # belt over braces: --no-fix already keeps the count at zero.
        if not read_only and ruff_fixed_count > 0:
            try:
                # Stage all changes ruff made, EXCEPT the state directory
                # (#274 review). Under use_worktrees=False this runs with
                # cwd at the project root, so a bare `git add -A` commits
                # kstrl's own live `.kstrl/` journals onto the component
                # branch the moment ruff fixes one finding - and
                # check_diff_scope is deliberately un-carved, so the next
                # pass fails on them and they ride into the PR. In a
                # worktree the same exclusion is wanted for the opposite
                # reason: a `.kstrl/` there is the agent's, and this
                # function must not commit it on the agent's behalf.
                #
                # A list, not a string: run_scrubbed only shells out for a
                # string, and the `:(exclude)` pathspec must reach git
                # unmangled.
                run_scrubbed(
                    ["git", "add", "-A", "--", ".", f":(exclude){STATE_DIR_NAME}"],
                    cwd=cwd,
                    timeout=30,
                )
                run_scrubbed(
                    'git commit -m "chore: auto-remove dead code (ruff F401/F811/F841)"',
                    cwd=cwd,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                pass  # Non-fatal

    # One phrase for every message below, so the read-only wording cannot
    # drift from the auto-fix wording.
    ruff_note = (
        f"ruff reports {ruff_pending_count} auto-removable, not removed"
        if read_only
        else f"ruff auto-fixed {ruff_fixed_count}"
    )
    ruff_touched = bool(ruff_fixed_count or ruff_pending_count)

    # --- Phase B: vulture or custom dead code detection ---
    if command:
        # User-provided dead code detection command
        detect_cmd = command
    elif shutil.which("vulture"):
        # Default: vulture on changed Python files only
        changed = git.get_diff_names(base_branch, cwd)
        py_files = [f for f in changed if f.endswith(".py") and not f.startswith("test")]
        if not py_files:
            msg = f"No dead code issues ({ruff_note})"
            return CheckResult(
                name="dead_code",
                passed=True,
                message=msg,
                duration_seconds=time.monotonic() - start,
            )
        detect_cmd = f"vulture {' '.join(py_files)} --min-confidence 80"
    else:
        # Neither vulture nor custom command available
        if ruff_touched:
            return CheckResult(
                name="dead_code",
                passed=True,
                message=f"{ruff_note}; vulture not installed",
                duration_seconds=time.monotonic() - start,
            )
        return CheckResult(
            name="dead_code",
            passed=True,
            message="Skipped: neither vulture nor custom command available",
            duration_seconds=time.monotonic() - start,
        )

    try:
        result = run_scrubbed(detect_cmd, cwd=cwd, timeout=timeout)
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
            line
            for line in lines
            if line.strip() and not line.strip().startswith("#") and "__all__" not in line
        ]
        if real_issues:
            prefix = f"{ruff_note}; " if ruff_touched else ""
            return CheckResult(
                name="dead_code",
                passed=False,
                message=f"{prefix}{len(real_issues)} dead code issues remaining",
                details=real_issues[:20],
                duration_seconds=time.monotonic() - start,
            )

    msg_parts: list[str] = []
    if ruff_touched:
        msg_parts.append(ruff_note)
    msg_parts.append("no remaining dead code")
    return CheckResult(
        name="dead_code",
        passed=True,
        message="; ".join(msg_parts),
        duration_seconds=time.monotonic() - start,
    )


def _diff_scope_runs(config: VerifyConfig, allowed_paths_error: str | None) -> bool:
    """Whether Phase 1 appends the ``diff_scope`` check at all.

    ``[verify] check_diff_scope`` turns off the scope COMPARISON. It
    does NOT turn off the report that no trustworthy scope could be
    established (#293 review): left gated on the toggle alone, that
    signal was dropped entirely, and with a None authored list the
    in-loop guard is inert too, so the component ran and merged with no
    scope enforcement at all and nothing said. Same argument
    ``check_prd_stories`` makes for carrying the tamper refusal rather
    than leaving it on a check an operator can switch off.

    Costs no git call in that second case: ``check_diff_scope`` returns
    on the error before it reads a diff.
    """
    return config.check_diff_scope or allowed_paths_error is not None


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path | None,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
    allowed_paths_error: str | None = None,
    harness_paths: list[str] | None = None,
    pre_run_prd_path: Path | None = None,
    fixtures_config: FixturesConfig | None = None,
    policy_config: PolicyConfig | None = None,
    adequacy_config: AdequacyConfig | None = None,
    autonomy_level: int = 0,
    component_id: str | None = None,
    read_only: bool = False,
) -> VerificationResult:
    """Run all mechanical checks. All checks run even if earlier ones fail.

    ``prd_path=None`` (R10.1, ``ks sense``) skips the PRD-dependent
    checks: ``prd_stories``, the approved-fixtures oracle (fixtures are
    declared in the PRD), and ``self_critique`` unless
    ``config.progress_file_path`` names the log explicitly (with no PRD
    there is no sibling to derive it from). Every other check runs
    exactly as it does with a real path.

    ``harness_paths`` (#264) is the per-component carve-out for kstrl's
    OWN files, forwarded to ``check_diff_scope``. It reaches the factory
    from the run's plan-time scope snapshot (``scope.RunScope``), which
    is also where ``allowed_paths`` comes from; ``ks sense`` leaves both
    None because it judges an operator's diff, not a factory
    component's.

    ``pre_run_prd_path`` (#269) is the copy of ``prd_path`` the run
    started with, forwarded to ``check_prd_stories``, which fails closed
    on a PRD the component rewrote. Also None for ``ks sense``: there is
    no pre-run copy to compare an operator's working tree against.

    ``fixtures_config`` (R7.2): when provided AND ``.enabled`` is true,
    the approved-fixtures oracle runs against the PRD's ``fixtures``
    entries - sandboxed subprocess execution lives in
    ``kstrl.fixtures``. ``component_id`` keys the fixture snapshot
    used for regression detection; None disables snapshotting only.

    ``read_only=True`` (``ks sense``, R10.1) forbids the two checks that
    change the tree they measure: ``dead_code`` drops its ruff auto-fix
    and the ``git add -A`` / ``git commit`` that followed it, and
    ``mutation_testing`` is skipped because mutmut works by rewriting
    source. What remains still shells out to the project's OWN
    configured test / typecheck / lint (and fixture) commands, which
    are the operator's programs and write their own caches; kstrl
    suppresses only kstrl's writes.
    """
    checks: list[CheckResult] = []

    if prd_path is not None:
        checks.append(check_prd_stories(prd_path, pre_run_prd_path))

    checks.append(
        check_test_suite(
            worktree_path,
            config.test_command,
            config.subprocess_timeout,
            config.test_tool,
        )
    )

    checks.append(
        check_typecheck(
            worktree_path,
            config.typecheck_command,
            config.subprocess_timeout,
            config.typecheck_tool,
        )
    )

    checks.append(
        check_linter(
            worktree_path,
            config.lint_command,
            config.subprocess_timeout,
            config.lint_tool,
        )
    )

    if _diff_scope_runs(config, allowed_paths_error):
        checks.append(
            check_diff_scope(
                worktree_path,
                base_branch,
                allowed_paths,
                allowed_paths_error=allowed_paths_error,
                harness_paths=harness_paths,
            )
        )

    if config.check_bad_patterns:
        checks.append(check_bad_patterns(worktree_path, base_branch))

    # R8.1 policy envelope: opt-in ([policy] enabled). When disabled the
    # check is not appended, so existing runs are unchanged.
    if policy_config is not None and policy_config.enabled:
        checks.append(
            check_policy_envelope(
                worktree_path,
                base_branch,
                policy_config,
            )
        )

    # R8.5 Layer 0: opt-in ([adequacy] enabled), advisory unless the
    # level or config says block. Runs before the expensive layers so a
    # suite-weakening diff is reported even when mutation is off.
    if adequacy_config is not None and adequacy_config.enabled:
        checks.append(
            check_test_adequacy(
                worktree_path,
                base_branch,
                adequacy_config,
                autonomy_level,
            )
        )

    if config.dead_code_cleanup:
        checks.append(
            check_dead_code(
                worktree_path,
                base_branch,
                config.dead_code_command,
                config.subprocess_timeout,
                read_only=read_only,
            )
        )

    if config.mutation_testing:
        checks.append(
            check_mutation_score(
                worktree_path,
                base_branch,
                config.mutation_threshold,
                config.mutation_timeout,
                read_only=read_only,
            )
        )

    if config.require_self_critique:
        # Read the log the engineer was actually pointed at: a factory
        # component writes NEXT TO its PRD (the only location inside its
        # allowedPaths), so resolving a repo-root default here would
        # check a file that was never written and fail the component for
        # the harness's own path confusion. An explicit config wins.
        # prd_path is worktree-absolute at the factory call site, so the
        # derived sibling is too; the join is a no-op for an absolute
        # path and still anchors a relative one. With neither a PRD nor
        # an explicit path there is no log to read, so the check is
        # skipped rather than run against a path that cannot exist.
        progress_path: Path | None = None
        if config.progress_file_path is not None:
            progress_path = worktree_path / Path(config.progress_file_path)
        elif prd_path is not None:
            progress_path = worktree_path / component_progress_path(
                prd_path,
                None,
            )
        if progress_path is not None:
            checks.append(
                check_self_critique(
                    progress_path,
                    config.self_critique_min_bullets,
                )
            )

    if prd_path is not None and fixtures_config is not None and fixtures_config.enabled:
        # Imported lazily: fixtures.py imports CheckResult/run_scrubbed
        # from this module, so a module-level import would be a cycle.
        from kstrl.fixtures import check_fixtures_from_prd

        checks.append(
            check_fixtures_from_prd(
                prd_path,
                worktree_path,
                fixtures_config,
                component_id=component_id,
            )
        )

    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks)
