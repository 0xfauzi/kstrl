"""Phase 2.5: Security review - a dedicated reviewer focused on vulnerabilities.

A separate LLM pass over the same diff that Phase 2 review evaluated for
correctness. Security review applies a different threat-modeling framing:
auth/authz, injection, secrets, deserialization, crypto, races, exfil paths.
The two reviewers catch different things; running them as separate calls is
a deliberate adversarial cross-check.

Note: SECURITY_PROMPT below names risky APIs (pickle.loads, yaml.load,
random for security, MD5/SHA1 for security, etc.) as examples the reviewer
should DETECT in user diffs. They are not invoked by this module. A
security-pattern linter scanning this file's string literals may flag
them; that is a false positive.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git
from ralph_py.decompose import (
    AgentOutputTooLarge,
    _extract_json,
    _select_agent_output,
    collect_agent_output,
)

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.ui.base import UI


class SecurityMode(str, Enum):
    HARD = "hard"      # block on critical findings
    ADVISORY = "advisory"  # surface findings but never block
    SKIP = "skip"      # skip the phase entirely


# Security category taxonomy. Each category maps to an OWASP Top 10
# (2021) bucket and a representative CWE so findings can be aggregated
# against industry-standard classifications. Used by the calibration
# runner and downstream reporting; kept here so the security reviewer
# prompt and tooling share one source of truth.
SECURITY_CATEGORY_MAP: dict[str, dict[str, str]] = {
    "injection": {"owasp": "A03:2021", "cwe": "CWE-89"},
    "auth_bypass": {"owasp": "A07:2021", "cwe": "CWE-287"},
    "authz_bypass": {"owasp": "A01:2021", "cwe": "CWE-285"},
    "hardcoded_secret": {"owasp": "A02:2021", "cwe": "CWE-798"},
    "unsafe_deserialization": {"owasp": "A08:2021", "cwe": "CWE-502"},
    "broken_crypto": {"owasp": "A02:2021", "cwe": "CWE-327"},
    "predictable_randomness": {"owasp": "A02:2021", "cwe": "CWE-338"},
    "missing_input_validation": {"owasp": "A03:2021", "cwe": "CWE-20"},
    "race_condition": {"owasp": "A04:2021", "cwe": "CWE-362"},
    "ssrf": {"owasp": "A10:2021", "cwe": "CWE-918"},
    "xss": {"owasp": "A03:2021", "cwe": "CWE-79"},
    "open_redirect": {"owasp": "A01:2021", "cwe": "CWE-601"},
    "information_disclosure": {"owasp": "A04:2021", "cwe": "CWE-200"},
    "denial_of_service": {"owasp": "A04:2021", "cwe": "CWE-400"},
    "other": {"owasp": "n/a", "cwe": "n/a"},
}

VALID_CATEGORIES = frozenset(SECURITY_CATEGORY_MAP.keys())

VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


def category_owasp(category: str) -> str:
    """Return the OWASP Top 10 bucket for a category, or 'n/a'."""
    return SECURITY_CATEGORY_MAP.get(category, {}).get("owasp", "n/a")


def category_cwe(category: str) -> str:
    """Return a representative CWE for a category, or 'n/a'."""
    return SECURITY_CATEGORY_MAP.get(category, {}).get("cwe", "n/a")


@dataclass
class SecurityFinding:
    """A single security concern surfaced by the security reviewer."""

    category: str
    severity: str  # critical | high | medium | low
    location: str
    explanation: str
    suggestion: str = ""


@dataclass
class SecurityResult:
    """Aggregated result of a security review."""

    passed: bool
    mode: str
    findings: list[SecurityFinding] = field(default_factory=list)
    overall_notes: str = ""
    # Self-reported thoroughness claim. Useful as a hint when triaging
    # security findings but DO NOT gate on it - it cannot be verified
    # at runtime. The trustworthy verification path is the planted-bug
    # calibration suite at tests/test_calibration.py (runs with
    # RALPH_RUN_CALIBRATION=1) which catches reviewers that claim
    # exhaustive coverage but miss known bugs.
    exhaustively_searched: bool = False
    raw_output: str = ""
    duration_seconds: float = 0.0
    # True when the reviewer agent failed to run or returned unparseable
    # output. Distinguishes "clean review found nothing" from "review
    # never actually happened" so hard-mode can fail loudly instead of
    # accidentally passing on infrastructure errors.
    infrastructure_error: bool = False

    def as_retry_context(self) -> str:
        """Format failing findings for injection into the implementer's
        retry prompt."""
        if not self.findings:
            return ""
        lines = ["Security findings to address:"]
        for f in self.findings:
            lines.append(
                f"- [{f.severity}] {f.category} at {f.location}: {f.explanation}"
            )
            if f.suggestion:
                lines.append(f"  Suggestion: {f.suggestion}")
        if self.overall_notes:
            lines.append(f"Overall: {self.overall_notes}")
        return "\n".join(lines)

    def as_pr_body_section(self) -> str:
        if not self.findings:
            return (
                "## Security Review\n\n"
                f"**No findings ({self.mode} mode, "
                f"{'exhaustively' if self.exhaustively_searched else 'briefly'} searched)**"
            )
        lines = ["## Security Review", ""]
        crit = sum(1 for f in self.findings if f.severity == "critical")
        high = sum(1 for f in self.findings if f.severity == "high")
        med = sum(1 for f in self.findings if f.severity == "medium")
        low = sum(1 for f in self.findings if f.severity == "low")
        lines.append(
            f"**{crit} critical, {high} high, {med} medium, {low} low "
            f"({self.mode} mode)**"
        )
        lines.append("")
        for f in self.findings:
            lines.append(
                f"- [{f.severity}] **{f.category}** at `{f.location}`"
            )
            lines.append(f"  - {f.explanation}")
            if f.suggestion:
                lines.append(f"  - Suggestion: {f.suggestion}")
        if self.overall_notes:
            lines.append("")
            lines.append(f"**Notes**: {self.overall_notes}")
        return "\n".join(lines)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")


@dataclass
class SecurityConfig:
    """Configuration for the security review phase."""

    mode: str = SecurityMode.ADVISORY.value
    agent_cmd: str | None = None
    agent_type: str | None = None
    model: str | None = None
    timeout_seconds: float = 600.0
    # Severity threshold above which findings cause the phase to fail
    # in HARD mode. Default "high" means critical+high fail the phase.
    fail_threshold: str = "high"

    def __post_init__(self) -> None:
        # Reject unknown modes / thresholds rather than silently
        # defaulting downstream (the env-var path bypasses click choice
        # validation so a typo would otherwise change the gate without
        # any signal).
        if self.mode not in {m.value for m in SecurityMode}:
            raise ValueError(
                f"Invalid SecurityConfig.mode {self.mode!r}; "
                f"must be one of skip|advisory|hard"
            )
        if self.fail_threshold not in VALID_SEVERITIES:
            raise ValueError(
                f"Invalid SecurityConfig.fail_threshold {self.fail_threshold!r}; "
                f"must be one of {sorted(VALID_SEVERITIES)}"
            )

    @classmethod
    def from_env(cls) -> SecurityConfig:
        return cls(
            mode=os.environ.get("RALPH_SECURITY_MODE", SecurityMode.ADVISORY.value),
            agent_cmd=os.environ.get("RALPH_SECURITY_AGENT_CMD") or None,
            agent_type=os.environ.get("RALPH_SECURITY_AGENT_TYPE") or None,
            model=os.environ.get("RALPH_SECURITY_MODEL") or None,
            timeout_seconds=float(os.environ.get("RALPH_SECURITY_TIMEOUT", "600")),
            fail_threshold=os.environ.get("RALPH_SECURITY_FAIL_THRESHOLD", "high"),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> SecurityConfig:
        """Load security config with precedence: env > toml > defaults."""
        from ralph_py.config import load_toml_section
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(root_dir / "ralph.toml", "security")
        if "mode" in section:
            config.mode = str(section["mode"])
        if "agent_cmd" in section:
            config.agent_cmd = str(section["agent_cmd"]) or None
        if "agent_type" in section:
            config.agent_type = str(section["agent_type"]) or None
        if "model" in section:
            config.model = str(section["model"]) or None
        if "timeout_seconds" in section:
            config.timeout_seconds = float(section["timeout_seconds"])
        if "fail_threshold" in section:
            config.fail_threshold = str(section["fail_threshold"])
        # Env overrides
        if "RALPH_SECURITY_MODE" in os.environ:
            config.mode = os.environ["RALPH_SECURITY_MODE"]
        if "RALPH_SECURITY_AGENT_CMD" in os.environ:
            config.agent_cmd = os.environ["RALPH_SECURITY_AGENT_CMD"] or None
        if "RALPH_SECURITY_AGENT_TYPE" in os.environ:
            config.agent_type = os.environ["RALPH_SECURITY_AGENT_TYPE"] or None
        if "RALPH_SECURITY_MODEL" in os.environ:
            config.model = os.environ["RALPH_SECURITY_MODEL"] or None
        if "RALPH_SECURITY_TIMEOUT" in os.environ:
            config.timeout_seconds = float(os.environ["RALPH_SECURITY_TIMEOUT"])
        if "RALPH_SECURITY_FAIL_THRESHOLD" in os.environ:
            config.fail_threshold = os.environ["RALPH_SECURITY_FAIL_THRESHOLD"]
        # Re-validate after assignment - typos in env or TOML must surface
        config.__post_init__()
        return config


SECURITY_PROMPT = """\
You are an adversarial application security reviewer. Your default stance
is that this diff introduces a vulnerability somewhere; your job is to
find it before it ships. You do not verify correctness or style - other
reviewers handle that. You focus exclusively on security.

Threat model: assume hostile input crosses every trust boundary visible
in the diff. Assume attackers can craft headers, query strings, request
bodies, file uploads, environment variables, and timing signals.

You must output ONLY valid JSON (no Markdown, no code fences, no
explanation).

Output schema:
{{
  "findings": [
    {{
      "category": "injection|auth_bypass|authz_bypass|hardcoded_secret|unsafe_deserialization|broken_crypto|predictable_randomness|missing_input_validation|race_condition|ssrf|xss|open_redirect|information_disclosure|denial_of_service|other",
      "severity": "critical|high|medium|low",
      "location": "path/to/file.py:42-58",
      "explanation": "what the vulnerability is and how an attacker could exploit it - evidence-based, citing the actual diff",
      "suggestion": "concrete fix"
    }}
  ],
  "exhaustively_searched": true,
  "overallNotes": "cross-cutting observations or empty string"
}}

Categories - look for ALL of these explicitly:
- "injection": shell, SQL, NoSQL, LDAP, OS command, template, log injection.
  Concatenated strings to subprocess.run / SQL execute / exec / eval are
  red flags.
- "auth_bypass": missing authentication check, broken JWT verification,
  comparing tokens with `==` (timing oracle), accepting client-supplied
  identity claims without re-verification.
- "authz_bypass": missing authorization check, IDOR, role/permission
  checks that miss a code path, mass assignment of restricted fields.
- "hardcoded_secret": API keys, passwords, tokens, private keys, salts,
  pinned credentials, or default test credentials shipped to prod.
- "unsafe_deserialization": pickle.loads / yaml.load (not safe_load) /
  marshal / shelve on attacker-controlled bytes.
- "broken_crypto": MD5/SHA1 for security, ECB mode, missing IV, fixed
  IV, custom crypto, predictable salts, hardcoded keys.
- "predictable_randomness": random.* used for security purposes (tokens,
  IDs, salts); should be secrets.* or os.urandom.
- "missing_input_validation": trust boundary crossed with no schema,
  size, range, type, or character-class check.
- "race_condition": TOCTOU, double-spend windows, missing locks on
  shared mutable state, unsafe concurrent file I/O.
- "ssrf": requests/urllib/curl invoked with user-controlled URL with
  no allowlist.
- "xss": HTML-rendered content built from user input without escaping;
  innerHTML usage; template engines with autoescape disabled.
- "open_redirect": redirect target taken from user input without
  allowlist.
- "information_disclosure": stack traces / internal IDs / DB errors
  leaked to clients; PII in logs; secrets in error messages.
- "denial_of_service": unbounded loops on user input, unbounded memory
  allocation, recursive regex, no rate limiting on expensive endpoints.

Severity:
- "critical": exploitable now, no auth required, full compromise possible
- "high": exploitable with realistic preconditions; significant damage
- "medium": requires unusual conditions but the door is open
- "low": defense-in-depth, hardening, future-risk

Evidence rules:
- Every finding must cite file:line ranges from the diff
- Do not speculate beyond what the diff shows
- Be honest: if you cannot find anything after looking, return
  "findings": [] AND "exhaustively_searched": true. Padding with
  fabricated findings is worse than silence.

Process: read every hunk. For each new function that touches a trust
boundary (HTTP handler, file read, subprocess, deserialization, SQL,
auth, crypto), ask: what input makes this misbehave? what could an
attacker craft? what is missing that a paranoid reviewer would demand?

================================================================================
PRD (what the implementer was asked to build)
================================================================================

{prd_content}

================================================================================
GIT DIFF (changes to review for security)
================================================================================

{diff_content}
"""


def _build_security_prompt(prd_text: str, diff_content: str) -> str:
    return SECURITY_PROMPT.format(
        prd_content=prd_text or "(PRD not available)",
        diff_content=git.truncate_diff_for_prompt(diff_content),
    )


def parse_security_output(raw_output: str, mode: str) -> SecurityResult:
    """Parse structured JSON from the security reviewer agent."""
    try:
        data = _extract_json(raw_output)
    except ValueError:
        return SecurityResult(
            passed=False,
            mode=mode,
            overall_notes="Failed to parse security reviewer output as JSON",
            raw_output=raw_output[:2000],
            infrastructure_error=True,
        )

    if not isinstance(data, dict):
        return SecurityResult(
            passed=False,
            mode=mode,
            overall_notes="Security output was not a JSON object",
            raw_output=raw_output[:2000],
            infrastructure_error=True,
        )

    findings: list[SecurityFinding] = []
    raw_findings = data.get("findings", [])
    if isinstance(raw_findings, list):
        for f in raw_findings:
            if not isinstance(f, dict):
                continue
            category = str(f.get("category", "")).strip()
            severity = str(f.get("severity", "")).strip()
            location = str(f.get("location", "")).strip()
            explanation = str(f.get("explanation", "")).strip()
            if category not in VALID_CATEGORIES:
                continue
            if severity not in VALID_SEVERITIES:
                continue
            if not explanation:
                continue
            findings.append(SecurityFinding(
                category=category,
                severity=severity,
                location=location,
                explanation=explanation,
                suggestion=str(f.get("suggestion", "")).strip(),
            ))

    exhaustively_searched = bool(data.get("exhaustively_searched", False))
    overall_notes = str(data.get("overallNotes", ""))

    return SecurityResult(
        passed=True,  # caller decides pass/fail based on mode + threshold
        mode=mode,
        findings=findings,
        exhaustively_searched=exhaustively_searched,
        overall_notes=overall_notes,
        raw_output=raw_output[:2000],
    )


_SEVERITY_ORDER = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _passes_threshold(
    findings: list[SecurityFinding], mode: str, fail_threshold: str,
) -> bool:
    """Decide whether the result passes given the mode and threshold."""
    if mode == SecurityMode.SKIP.value:
        return True
    if mode == SecurityMode.ADVISORY.value:
        return True
    # HARD mode: fail if any finding meets or exceeds the threshold.
    threshold_rank = _SEVERITY_ORDER.get(fail_threshold, 2)  # default "high"
    blocking = [
        f for f in findings
        if _SEVERITY_ORDER.get(f.severity, 0) >= threshold_rank
    ]
    return not blocking


def run_security_review(
    agent: Agent,
    prd_path: Path,
    worktree_path: Path,
    base_branch: str,
    config: SecurityConfig,
    ui: UI,
    diff_content: str | None = None,
) -> SecurityResult:
    """Run the security review phase. Always non-fatal: on any
    infrastructure error returns a SecurityResult with empty findings
    and passed=True. The caller decides whether to gate on passed."""
    mode = config.mode
    if mode == SecurityMode.SKIP.value:
        return SecurityResult(passed=True, mode=mode)

    ui.info("  Running security review...")
    start = time.monotonic()

    prd_text = ""
    try:
        prd_text = prd_path.read_text(encoding="utf-8")
    except OSError:
        pass

    if diff_content is None:
        diff_content = git.get_diff_content(base_branch, worktree_path)
    prompt = _build_security_prompt(prd_text, diff_content)

    try:
        output_lines = collect_agent_output(
            agent, prompt, cwd=worktree_path, timeout=config.timeout_seconds,
        )
    except (AgentOutputTooLarge, Exception) as exc:  # noqa: BLE001
        # Agent crashed mid-run OR streamed more than MAX_AGENT_OUTPUT_BYTES.
        # In hard mode this MUST surface as a failure - otherwise a
        # flaky / hostile agent silently approves the diff. Advisory
        # mode warns but doesn't block. Skip mode never gets here.
        passed = mode != SecurityMode.HARD.value
        return SecurityResult(
            passed=passed,
            mode=mode,
            overall_notes=f"Security review agent failed: {exc}",
            duration_seconds=time.monotonic() - start,
            infrastructure_error=True,
        )

    raw_output = _select_agent_output(agent, output_lines)
    result = parse_security_output(raw_output, mode)
    if result.infrastructure_error:
        # Parsing failed - we have no usable findings list, so don't
        # let _passes_threshold overwrite passed=False with True. In
        # hard mode this is a block; in advisory it surfaces as a
        # warning but lets the pipeline continue.
        if mode != SecurityMode.HARD.value:
            result.passed = True
    else:
        result.passed = _passes_threshold(
            result.findings, mode, config.fail_threshold,
        )
    result.duration_seconds = time.monotonic() - start

    status = "passed" if result.passed else "FAILED"
    ui.info(
        f"  Security review {status}: "
        f"{result.critical_count} critical, {result.high_count} high, "
        f"{len(result.findings)} total"
    )
    return result
