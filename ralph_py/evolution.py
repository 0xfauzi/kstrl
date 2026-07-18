"""Continuous learning - evolution journal, experiment tracking, and harness proposals.

Records factory run outcomes and extracts recurring failure patterns across runs.
Inspired by AutoResearchClaw's evolution directory and autoresearch-agents' results.tsv.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ralph_py.factory import FactoryResult
    from ralph_py.findings import Finding
    from ralph_py.manifest import Component, Manifest
    from ralph_py.verify import CheckResult

logger = logging.getLogger("ralph.evolution")

# R6.4: journal entries carry an explicit schema version so future
# format migrations are detectable. Version 2 = structured failure
# signatures (R6.1). Entries without the field are version 1 (the
# pre-R6 shape); wave 1 (R4.1) archived the polluted v1 journals to
# .ralph/archive/, so fresh journals contain v2 entries only.
JOURNAL_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EvolutionConfig:
    enabled: bool = True
    journal_path: Path = field(default_factory=lambda: Path(".ralph/evolution.jsonl"))
    experiments_path: Path = field(default_factory=lambda: Path(".ralph/experiments.tsv"))
    min_pattern_frequency: int = 2
    lookback_runs: int = 10
    auto_propose: bool = True
    auto_apply_computational: bool = False

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config from environment variables only.

        Relative journal/experiments paths resolve against ``root_dir``
        (the project root), not the process CWD, matching :meth:`load`.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config with precedence: env > toml > defaults."""
        from ralph_py.config import load_toml_section
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(root_dir / "ralph.toml", "evolution")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "journal_path" in section:
            jp = str(section["journal_path"])
            config.journal_path = (
                Path(jp) if Path(jp).is_absolute() else root_dir / jp
            )
        if "experiments_path" in section:
            ep = str(section["experiments_path"])
            config.experiments_path = (
                Path(ep) if Path(ep).is_absolute() else root_dir / ep
            )
        if "min_pattern_frequency" in section:
            config.min_pattern_frequency = int(section["min_pattern_frequency"])
        if "lookback_runs" in section:
            config.lookback_runs = int(section["lookback_runs"])
        if "auto_propose" in section:
            config.auto_propose = bool(section["auto_propose"])
        if "auto_apply_computational" in section:
            config.auto_apply_computational = bool(
                section["auto_apply_computational"]
            )
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config


def _apply_env_overrides(config: EvolutionConfig, root_dir: Path) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""
    if "RALPH_EVOLUTION_ENABLED" in os.environ:
        config.enabled = os.environ["RALPH_EVOLUTION_ENABLED"].lower() in {
            "1", "true", "yes",
        }
    if "RALPH_EVOLUTION_JOURNAL_PATH" in os.environ:
        raw = os.environ["RALPH_EVOLUTION_JOURNAL_PATH"]
        config.journal_path = (
            Path(raw) if Path(raw).is_absolute() else root_dir / raw
        )
    if "RALPH_EVOLUTION_LOOKBACK_RUNS" in os.environ:
        config.lookback_runs = int(os.environ["RALPH_EVOLUTION_LOOKBACK_RUNS"])


def _resolve_relative_paths(config: EvolutionConfig, root_dir: Path) -> None:
    """Anchor relative journal/experiments paths to the project root.

    The bare ``EvolutionConfig()`` constructor keeps its historical
    CWD-relative defaults; the load/from_env paths always hand back
    absolute paths so ``ralph factory --root X`` run from elsewhere
    cannot scatter ``.ralph/`` state into the operator's CWD.
    """
    if not config.journal_path.is_absolute():
        config.journal_path = root_dir / config.journal_path
    if not config.experiments_path.is_absolute():
        config.experiments_path = root_dir / config.experiments_path


@dataclass
class FailurePattern:
    description: str
    frequency: int
    total_components: int
    affected_components: list[str]
    check_name: str  # e.g. "test_suite", "typecheck", "linter", "review"
    # structured failure code (e.g. "S608" for ruff, "arg-type" for mypy,
    # "scope_creep" for a review concern) - the part after the colon in
    # the full "<check>:<code>" signature
    error_signature: str
    category: str  # "verification", "review", "security", "contract", "iteration"


@dataclass
class HarnessProposal:
    id: str  # e.g. "PROP-001"
    title: str
    description: str
    proposal_type: str  # "computational" or "inferential"
    target: str  # what to change: "claude_md", "pyproject", "feedforward_config"
    suggested_change: str  # the actual proposed content/config change
    source_patterns: list[str]  # pattern descriptions that led to this proposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Regex for linter rule codes like S608, E501, W291, etc.
_LINTER_CODE_RE = re.compile(r"\b([A-Z]\d{3,4})\b")

# Regex to strip file paths (unix and windows style)
_PATH_RE = re.compile(r"(?:/[\w./-]+|[A-Z]:\\[\w.\\-]+)")

# Regex to strip line/column numbers like ":42:" or "line 42"
_LINENO_RE = re.compile(r"(?::\d+:?\d*|line \d+|col(?:umn)? \d+)", re.IGNORECASE)

# Regex to strip quoted variable/argument names
_QUOTED_NAME_RE = re.compile(r"['\"][\w.]+['\"]")


def _normalize_error(error: str) -> str:
    """Normalize an error string into a stable signature.

    - Keeps linter rule codes as-is (e.g. S608, E501).
    - Strips file paths, line numbers, and variable names.
    - Converts the remaining message to a slug.
    """
    if not error:
        return ""

    # Check for a linter rule code first - it is the most stable identifier.
    code_match = _LINTER_CODE_RE.search(error)
    if code_match:
        return code_match.group(1)

    normalized = error
    normalized = _PATH_RE.sub("", normalized)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)

    # Take the first meaningful line only.
    first_line = normalized.strip().split("\n")[0].strip()

    # Extract "ErrorType: message" pattern if present.
    colon_idx = first_line.find(":")
    if colon_idx > 0:
        error_type = first_line[:colon_idx].strip()
        message = first_line[colon_idx + 1:].strip()
        # Slugify message portion.
        slug = re.sub(r"[^a-z0-9]+", "-", message.lower()).strip("-")
        if slug:
            return slug[:80]
        return re.sub(r"[^a-z0-9]+", "-", error_type.lower()).strip("-")[:80]

    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:80] if slug else "unknown"


def _classify_check(error: str) -> tuple[str, str]:
    """Return (check_name, category) inferred from the error text."""
    lower = error.lower()

    if any(kw in lower for kw in ("ruff", "flake8", "pylint", "lint")):
        return "linter", "verification"
    if any(kw in lower for kw in ("mypy", "pyright", "typecheck", "type error")):
        return "typecheck", "verification"
    if any(kw in lower for kw in ("pytest", "test", "assert", "unittest")):
        return "test_suite", "verification"
    if any(kw in lower for kw in ("review", "finding", "reviewer")):
        return "review", "review"
    if any(kw in lower for kw in ("contract", "integration")):
        return "contract", "contract"
    if any(kw in lower for kw in ("mechanical verification failed",)):
        return "verification", "verification"

    return "unknown", "iteration"


# ---------------------------------------------------------------------------
# Structured failure signatures (R6.1)
#
# A failure signature is "<check_name>:<code>", e.g. "linter:E501",
# "typecheck:arg-type", "review:scope_creep", "diff_scope:files-outside-
# allowed-scope". The check prefix comes from the gate that fired; the
# code comes from the tool's parser (ruff rule, mypy error code, finding
# category) rather than from re-parsing a flattened error string, so
# cross-run grouping is on real, stable identifiers.
# ---------------------------------------------------------------------------

# Digit runs are counts/limits ("3 files outside scope", "600s wall
# clock") - stripping them keeps slugs stable across runs whose only
# difference is the number.
_DIGIT_RUN_RE = re.compile(r"\d+")

# Leading Python exception name in a pytest failure message.
_EXC_NAME_RE = re.compile(
    r"^([A-Z][A-Za-z0-9]*(?:Error|Exception|Failure|Warning|Exit|Interrupt))\b"
)

_CATEGORY_BY_CHECK = {
    "linter": "verification",
    "typecheck": "verification",
    "test_suite": "verification",
    "diff_scope": "verification",
    "bad_patterns": "verification",
    "self_critique": "verification",
    "dead_code": "verification",
    "mutation_testing": "verification",
    "prd_stories": "verification",
    "verification": "verification",
    "review": "review",
    "security": "security",
    "contract": "contract",
}

# Cap on distinct per-check signatures so one catastrophic run (e.g. 40
# distinct ruff rules) cannot flood the journal entry.
_MAX_SIGNATURES_PER_CHECK = 5


def signature_slug(text: str) -> str:
    """Stable low-cardinality slug for a failure message.

    Strips file paths, line/column numbers, quoted names, and standalone
    counts, then slugifies the first line. Unlike ``_normalize_error``
    this never extracts linter codes (callers get those from the parser
    directly) and never keeps varying counts."""
    if not text:
        return ""
    normalized = _PATH_RE.sub("", text)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)
    normalized = _DIGIT_RUN_RE.sub("", normalized)
    first_line = normalized.strip().split("\n")[0].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:60]


def signature_for_error(check_name: str, error: str) -> str:
    """Fallback signature when no parser-level codes are available."""
    slug = signature_slug(error) or "failed"
    return f"{check_name or 'unknown'}:{slug}"


def split_signature(signature: str) -> tuple[str, str]:
    """Split "check:code" into (check_name, code)."""
    check, sep, code = signature.partition(":")
    if not sep:
        return "unknown", signature
    return check or "unknown", code or "failed"


def category_for_check(check_name: str) -> str:
    """Map a check/gate name to a FailurePattern category."""
    return _CATEGORY_BY_CHECK.get(check_name, "iteration")


def signatures_from_verification(checks: Iterable[CheckResult]) -> list[str]:
    """Derive structured signatures from failed mechanical checks.

    Prefers the parser's structured codes (ruff rule, mypy error code,
    pytest exception type); falls back to a slug of the check message
    when no parse is available."""
    signatures: list[str] = []
    for check in checks:
        if check.passed:
            continue
        codes: list[str] = []
        parsed = check.parsed
        if parsed is not None and parsed.failures:
            if parsed.tool in ("ruff", "mypy"):
                codes = [f.rule_or_test for f in parsed.failures if f.rule_or_test]
            elif parsed.tool == "pytest":
                for failure in parsed.failures:
                    m = _EXC_NAME_RE.match(failure.message or "")
                    if m:
                        codes.append(
                            re.sub(r"(?<!^)(?=[A-Z])", "-", m.group(1)).lower()
                        )
        if codes:
            distinct = list(dict.fromkeys(codes))[:_MAX_SIGNATURES_PER_CHECK]
            signatures.extend(f"{check.name}:{code}" for code in distinct)
        else:
            signatures.append(signature_for_error(check.name, check.message))
    return list(dict.fromkeys(signatures))


def signatures_from_findings(phase: str, findings: Iterable[Finding]) -> list[str]:
    """Derive signatures from the typed findings that failed a review or
    security gate: "<phase>:<category>" for every gating finding
    (severity fail/critical/high) and "<phase>:infrastructure" when the
    role itself failed to run."""
    signatures: list[str] = []
    for finding in findings:
        if finding.is_infrastructure_error:
            signatures.append(f"{phase}:infrastructure")
        elif finding.severity in ("fail", "critical", "high"):
            signatures.append(f"{phase}:{finding.category}")
    return list(dict.fromkeys(signatures))[:_MAX_SIGNATURES_PER_CHECK]


def _timestamp_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarize_findings(findings: list[Finding]) -> dict[str, Any]:
    """Aggregate counts grouped by phase, severity, category, and OWASP
    bucket for the evolution journal. Lets dashboards query trends
    without re-walking every Finding."""
    summary: dict[str, Any] = {
        "total": len(findings),
        "by_phase": {},
        "by_severity": {},
        "by_category": {},
        "by_owasp": {},
        "infrastructure_errors": 0,
    }
    for f in findings:
        if f.is_infrastructure_error:
            summary["infrastructure_errors"] += 1
        summary["by_phase"][f.phase] = summary["by_phase"].get(f.phase, 0) + 1
        summary["by_severity"][f.severity] = (
            summary["by_severity"].get(f.severity, 0) + 1
        )
        summary["by_category"][f.category] = (
            summary["by_category"].get(f.category, 0) + 1
        )
        if f.owasp:
            summary["by_owasp"][f.owasp] = summary["by_owasp"].get(f.owasp, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class EvolutionJournal:
    def __init__(self, config: EvolutionConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # record_run
    # ------------------------------------------------------------------

    def record_run(
        self,
        run_id: str,
        manifest: Manifest,
        factory_result: FactoryResult,
        usage_by_component: dict[str, dict[str, dict[str, Any]]] | None = None,
        run_usage: dict[str, Any] | None = None,
        failure_signatures: dict[str, list[str]] | None = None,
    ) -> None:
        """Record a completed factory run to the journal.

        Writes individual component outcomes as JSONL entries.
        Also appends a summary line to experiments.tsv.

        R3.1: ``usage_by_component`` maps component id -> phase ->
        UsageTotals.to_dict() and lands on each component's journal
        entry; ``run_usage`` is the run-level UsageTotals.to_dict() and
        feeds the TSV totals columns. Both optional so pre-R3.1 callers
        keep working; token/cost figures are CLI self-reports and are
        lower bounds whenever ``unreported_calls`` > 0.

        R6.1: ``failure_signatures`` maps component id -> the structured
        "<check>:<code>" signatures the factory recorded when the
        component's last attempt failed (e.g. "linter:E501",
        "review:scope_creep"). When absent for a failed component, the
        legacy flattened-string classification is the fallback so
        journal entries never lose the signature fields entirely.
        """
        from ralph_py.manifest import ComponentStatus

        timestamp = _timestamp_now()
        usage_by_component = usage_by_component or {}
        failure_signatures = failure_signatures or {}

        # --- JSONL entries per component ---
        entries: list[dict[str, Any]] = []
        for comp in manifest.components:
            has_error = bool(comp.error) and comp.status in (
                ComponentStatus.FAILED.value,
                ComponentStatus.PENDING.value,  # retried components reset to pending
            )
            comp_signatures: list[str] = []
            check_name = ""
            error_sig = ""
            if has_error:
                comp_signatures = list(failure_signatures.get(comp.id) or [])
                if not comp_signatures:
                    # Legacy fallback: classify the flattened string.
                    legacy_check, _ = _classify_check(comp.error)
                    legacy_sig = _normalize_error(comp.error)
                    if legacy_sig:
                        comp_signatures = [f"{legacy_check}:{legacy_sig}"]
                if comp_signatures:
                    check_name, error_sig = split_signature(comp_signatures[0])
            # E3-consume: include typed findings in the journal so
            # downstream aggregations (concern hit-rate, OWASP-bucket
            # frequency, infrastructure_error rate) can query the
            # structured stream directly rather than re-parsing the
            # rendered string.
            findings_serialized = [f.to_dict() for f in comp.findings]
            findings_summary = _summarize_findings(comp.findings)
            entry = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "timestamp": timestamp,
                "run_id": run_id,
                "project": manifest.project_name,
                "component_id": comp.id,
                "event_type": "component_result",
                "status": comp.status,
                "retries": comp.retries,
                "error": comp.error,
                "check_name": check_name,
                "error_signature": error_sig,
                "failure_signatures": comp_signatures,
                "failed_phase": comp.failed_phase,
                "failed_check": comp.failed_check,
                "duration_seconds": comp.duration_seconds,
                "iteration_count": comp.iteration_count,
                "findings": findings_serialized,
                "findings_summary": findings_summary,
                "usage": usage_by_component.get(comp.id, {}),
            }
            entries.append(entry)

        try:
            self.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.journal_path, "a") as f:
                for entry in entries:
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning(
                "evolution journal write failed (non-fatal): %s: %s",
                self.config.journal_path, exc,
            )

        # --- Experiments TSV summary line ---
        total = len(manifest.components)
        completed = len(factory_result.completed)
        failed = len(factory_result.failed)
        skipped = len(factory_result.skipped)

        iteration_counts = [c.iteration_count for c in manifest.components if c.iteration_count > 0]
        avg_iterations = (
            sum(iteration_counts) / len(iteration_counts) if iteration_counts else 0.0
        )

        durations = [c.duration_seconds for c in manifest.components if c.duration_seconds > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        retry_total = sum(c.retries for c in manifest.components)
        retry_rate = retry_total / total if total > 0 else 0.0

        # Most common failure signature (full "<check>:<code>" form).
        failure_sigs: dict[str, int] = {}
        for comp in manifest.components:
            if comp.status != ComponentStatus.FAILED.value or not comp.error:
                continue
            sigs = list(failure_signatures.get(comp.id) or [])
            if not sigs:
                sigs = [signature_for_error(
                    _classify_check(comp.error)[0], comp.error,
                )]
            for sig in sigs:
                failure_sigs[sig] = failure_sigs.get(sig, 0) + 1
        common_failure = max(failure_sigs, key=failure_sigs.get, default="") if failure_sigs else ""  # type: ignore[arg-type]

        # R3.1 totals columns. Empty string (not 0) when no usage was
        # tracked for the run - zero would misread as "measured, free".
        # unreported_calls > 0 marks the token/cost figures as lower
        # bounds. Files written before R3.1 keep their shorter header;
        # csv.DictReader in get_experiment_trends drops the extra values
        # rather than crashing.
        if run_usage:
            total_tokens_col = str(run_usage.get("total_tokens", ""))
            total_cost_col = str(run_usage.get("cost_usd", ""))
            unreported_col = str(run_usage.get("unreported_calls", ""))
        else:
            total_tokens_col = total_cost_col = unreported_col = ""

        header = (
            "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
            "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\t"
            "total_tokens\ttotal_cost_usd\tunreported_calls"
        )
        row = (
            f"{run_id}\t{timestamp}\t{manifest.project_name}\t{total}\t"
            f"{completed}\t{failed}\t{skipped}\t{avg_iterations:.2f}\t"
            f"{avg_duration:.1f}\t{retry_rate:.2f}\t{common_failure}\t"
            f"{total_tokens_col}\t{total_cost_col}\t{unreported_col}"
        )

        try:
            self.config.experiments_path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = (
                not self.config.experiments_path.exists()
                or self.config.experiments_path.stat().st_size == 0
            )
            with open(self.config.experiments_path, "a") as f:
                if needs_header:
                    f.write(header + "\n")
                f.write(row + "\n")
        except OSError as exc:
            logger.warning(
                "experiments.tsv write failed (non-fatal): %s: %s",
                self.config.experiments_path, exc,
            )

    # ------------------------------------------------------------------
    # extract_failure_patterns (single run)
    # ------------------------------------------------------------------

    def extract_failure_patterns(
        self,
        manifest: Manifest,
        min_frequency: int = 2,
        signatures_by_component: dict[str, list[str]] | None = None,
    ) -> list[FailurePattern]:
        """Extract recurring failure patterns from a single run.

        Looks at failed/retried components to find common failure
        signatures, grouped by the full "<check>:<code>" signature.
        ``signatures_by_component`` carries the factory's structured
        signatures (R6.1); components absent from it fall back to
        classifying their flattened error string.
        """
        from ralph_py.manifest import ComponentStatus

        signatures_by_component = signatures_by_component or {}

        # Collect components that failed or were retried.
        troubled: list[Component] = [
            c
            for c in manifest.components
            if c.status == ComponentStatus.FAILED.value or c.retries > 0
        ]

        if not troubled:
            return []

        # Group by full signature string.
        groups: dict[str, list[str]] = {}
        for comp in troubled:
            if not comp.error:
                continue
            sigs = list(signatures_by_component.get(comp.id) or [])
            if not sigs:
                legacy_check, _ = _classify_check(comp.error)
                legacy_sig = _normalize_error(comp.error)
                if not legacy_sig:
                    continue
                sigs = [f"{legacy_check}:{legacy_sig}"]
            for sig in sigs:
                groups.setdefault(sig, []).append(comp.id)

        total = len(manifest.components)
        patterns: list[FailurePattern] = []
        for full_sig, comp_ids in groups.items():
            if len(comp_ids) < min_frequency:
                continue
            check_name, code = split_signature(full_sig)
            patterns.append(
                FailurePattern(
                    description=(
                        f"{check_name} failure '{code}' in {len(comp_ids)}/{total} components"
                    ),
                    frequency=len(comp_ids),
                    total_components=total,
                    affected_components=comp_ids,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # get_cross_run_patterns
    # ------------------------------------------------------------------

    def get_cross_run_patterns(
        self,
        lookback_runs: int = 10,
    ) -> list[FailurePattern]:
        """Get patterns that recur across multiple factory runs.

        Reads the journal, groups entries by their structured failure
        signatures ("<check>:<code>", R6.1), and returns patterns that
        appear in >= min_pattern_frequency distinct runs. Legacy v1
        entries without ``failure_signatures`` fall back to composing
        the signature from their check_name/error_signature fields.
        """
        entries = self._read_journal_entries(lookback_runs)
        if not entries:
            return []

        # Group by full signature across distinct run_ids.
        sig_runs: dict[str, set[str]] = {}
        sig_components: dict[str, list[str]] = {}

        for entry in entries:
            if entry.get("event_type", "component_result") != "component_result":
                continue
            sigs = entry.get("failure_signatures") or []
            if not sigs:
                # v1 fallback: compose from the legacy scalar fields.
                legacy_sig = entry.get("error_signature", "")
                if not legacy_sig:
                    continue
                legacy_check = entry.get("check_name") or "unknown"
                sigs = [f"{legacy_check}:{legacy_sig}"]
            run_id = entry.get("run_id", "")
            comp_id = entry.get("component_id", "")
            for sig in sigs:
                if not isinstance(sig, str) or not sig:
                    continue
                sig_runs.setdefault(sig, set()).add(run_id)
                sig_components.setdefault(sig, []).append(comp_id)

        total_runs = len({
            e.get("run_id") for e in entries
            if e.get("event_type", "component_result") == "component_result"
        })
        patterns: list[FailurePattern] = []

        for sig, run_ids in sig_runs.items():
            if len(run_ids) < self.config.min_pattern_frequency:
                continue
            check_name, code = split_signature(sig)
            unique_comps = list(dict.fromkeys(sig_components.get(sig, [])))
            patterns.append(
                FailurePattern(
                    description=(
                        f"'{sig}' appeared in {len(run_ids)}/{total_runs} runs "
                        f"across {len(unique_comps)} components"
                    ),
                    frequency=len(run_ids),
                    total_components=total_runs,
                    affected_components=unique_comps,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # propose_improvements
    # ------------------------------------------------------------------

    def propose_improvements(
        self,
        patterns: list[FailurePattern],
        starting_number: int = 1,
    ) -> list[HarnessProposal]:
        """Generate concrete harness improvement proposals from patterns.

        Computational proposals only (no LLM calls):
        - Recurring linter errors - suggest CLAUDE.md convention entry
        - Recurring typecheck patterns - suggest config change
        - Recurring test failures on same module - suggest feedforward focus
        - Recurring review/security finding categories - suggest CLAUDE.md
          guidance derived from the finding taxonomy

        R6.2: IDs are monotonic across runs - pass
        ``next_proposal_number(output_dir)`` as ``starting_number`` so a
        second `ralph evolve` continues numbering instead of restarting
        at PROP-001 and clobbering earlier files.
        """
        proposals: list[HarnessProposal] = []
        counter = starting_number - 1

        for pattern in patterns:
            counter += 1
            proposal_id = f"PROP-{counter:03d}"

            if pattern.check_name == "linter":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Add linter convention for {pattern.error_signature} to CLAUDE.md",
                        description=(
                            f"Linter rule {pattern.error_signature} triggered in "
                            f"{pattern.frequency} components. Adding an explicit convention "
                            f"to CLAUDE.md will help the agent avoid this pattern."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Avoid triggering linter rule {pattern.error_signature}. "
                            f"Check ruff/flake8 docs for the correct pattern."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "typecheck":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Adjust type-checking config for '{pattern.error_signature}'",
                        description=(
                            f"Type error pattern '{pattern.error_signature}' recurred in "
                            f"{pattern.frequency} components. Consider adjusting pyproject.toml "
                            f"or adding a CLAUDE.md note about the expected typing style."
                        ),
                        proposal_type="computational",
                        target="pyproject",
                        suggested_change=(
                            f"Review [tool.mypy] or [tool.pyright] settings in pyproject.toml. "
                            f"If this is a known false positive, add to ignore list. "
                            f"Otherwise add to CLAUDE.md:\n"
                            f"> Ensure all functions have return type annotations to avoid "
                            f"'{pattern.error_signature}'."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "test_suite":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Add feedforward focus for test pattern '{pattern.error_signature}'",
                        description=(
                            f"Test failure '{pattern.error_signature}' hit "
                            f"{pattern.frequency} components: "
                            f"{', '.join(pattern.affected_components[:5])}. "
                            f"Focusing feedforward context on this pattern may help the agent "
                            f"fix the root cause earlier in the iteration loop."
                        ),
                        proposal_type="computational",
                        target="feedforward_config",
                        suggested_change=(
                            f"Add to feedforward config or CLAUDE.md:\n"
                            f"> Known recurring test issue: '{pattern.error_signature}'. "
                            f"When tests fail with this pattern, check the affected modules "
                            f"before re-running."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "review":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Add review guidance for '{pattern.error_signature}'",
                        description=(
                            f"Review finding category '{pattern.error_signature}' "
                            f"(reviewer concern taxonomy) appeared in "
                            f"{pattern.frequency} components. Adding explicit guidance to "
                            f"CLAUDE.md can help the agent avoid this in the first pass."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Reviewer repeatedly flags '{pattern.error_signature}'. "
                            f"Address this pattern proactively."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            elif pattern.check_name == "security":
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=(
                            f"Add security guidance for "
                            f"'{pattern.error_signature}'"
                        ),
                        description=(
                            f"Security finding category '{pattern.error_signature}' "
                            f"(OWASP-mapped taxonomy) appeared in "
                            f"{pattern.frequency} components. Adding an explicit "
                            f"convention to CLAUDE.md can prevent the vulnerability "
                            f"class from being introduced at all."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Security reviewer repeatedly flags "
                            f"'{pattern.error_signature}'. Follow the secure "
                            f"pattern for this category from the start."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

            else:
                # Generic proposal for unknown/iteration category patterns.
                proposals.append(
                    HarnessProposal(
                        id=proposal_id,
                        title=f"Investigate recurring failure: {pattern.error_signature}",
                        description=(
                            f"Pattern '{pattern.error_signature}' ({pattern.check_name}) "
                            f"occurred {pattern.frequency} times. Manual investigation "
                            f"recommended."
                        ),
                        proposal_type="computational",
                        target="claude_md",
                        suggested_change=(
                            f"Add to CLAUDE.md:\n"
                            f"> Known issue: '{pattern.error_signature}'. "
                            f"Take extra care with this pattern."
                        ),
                        source_patterns=[pattern.description],
                    )
                )

        return proposals

    # ------------------------------------------------------------------
    # save_proposals
    # ------------------------------------------------------------------

    def next_proposal_number(self, output_dir: Path) -> int:
        """Next monotonic proposal number: max existing PROP number in
        ``output_dir`` plus one (R6.2). 1 when the directory is empty or
        missing."""
        highest = 0
        try:
            candidates = list(output_dir.glob("prop-*.md"))
        except OSError:
            return 1
        for path in candidates:
            m = re.fullmatch(r"prop-(\d+)\.md", path.name)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest + 1

    def save_proposals(
        self,
        proposals: list[HarnessProposal],
        output_dir: Path,
    ) -> list[Path]:
        """Write proposals as markdown files to output_dir.

        Returns list of written file paths. Never overwrites an existing
        proposal file (R6.2): a filename collision means the caller
        numbered the batch wrong (see ``next_proposal_number``), and
        clobbering would silently rewrite audit history - skip and warn
        instead.
        """
        written: list[Path] = []
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "proposal dir creation failed (non-fatal): %s: %s",
                output_dir, exc,
            )
            return written

        for proposal in proposals:
            filename = f"{proposal.id.lower()}.md"
            filepath = output_dir / filename

            if filepath.exists():
                logger.warning(
                    "refusing to overwrite existing proposal %s; "
                    "renumber with next_proposal_number()", filepath,
                )
                continue

            sources_block = "\n".join(
                f"- {s}" for s in proposal.source_patterns
            )

            content = (
                f"# {proposal.id}: {proposal.title}\n"
                f"\n"
                f"**Type**: {proposal.proposal_type}\n"
                f"**Target**: {proposal.target}\n"
                f"**Source patterns**:\n"
                f"{sources_block}\n"
                f"\n"
                f"## Description\n"
                f"\n"
                f"{proposal.description}\n"
                f"\n"
                f"## Suggested change\n"
                f"\n"
                f"{proposal.suggested_change}\n"
            )

            try:
                filepath.write_text(content)
                written.append(filepath)
            except OSError as exc:
                logger.warning(
                    "proposal write failed (non-fatal): %s: %s",
                    filepath, exc,
                )

        return written

    # ------------------------------------------------------------------
    # get_concern_hit_rate (D8)
    # ------------------------------------------------------------------

    def get_concern_hit_rate(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate reviewer/security finding signal across recent runs.

        Returns ``{"runs": N, "components": M, "with_concern": K,
        "by_category": {...}}`` so dashboards can ask "did the
        adversarial reviewers surface anything across the last N runs?"

        R6.2: consumes the typed ``findings_summary`` that record_run
        writes on every component_result entry (E3 stream), replacing
        the old error-string scan that was structurally zero (concern
        categories never appeared in ``component.error``). A component
        counts as "with concern" when its summary has at least one
        finding in a real category - the synthetic
        ``infrastructure_error`` and ``phase_skipped`` categories mark
        non-execution, not adversarial signal, and are excluded.
        """
        entries = [
            e for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        runs = len({e.get("run_id", "") for e in entries})
        components = len(entries)
        with_concern = 0
        by_category: dict[str, int] = {}
        for entry in entries:
            summary = entry.get("findings_summary") or {}
            cat_counts = summary.get("by_category") or {}
            if not isinstance(cat_counts, dict):
                continue
            hit = False
            for category, count in cat_counts.items():
                if category in ("infrastructure_error", "phase_skipped"):
                    continue
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                hit = True
                by_category[category] = by_category.get(category, 0) + n
            if hit:
                with_concern += 1
        return {
            "runs": runs,
            "components": components,
            "with_concern": with_concern,
            "by_category": by_category,
        }

    # ------------------------------------------------------------------
    # get_experiment_trends
    # ------------------------------------------------------------------

    def get_experiment_trends(self, last_n: int = 10) -> list[dict[str, Any]]:
        """Read experiments.tsv and return the last N entries as dicts."""
        try:
            text = self.config.experiments_path.read_text()
        except OSError:
            return []

        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows = list(reader)
        return rows[-last_n:]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_journal_entries(self, lookback_runs: int = 10) -> list[dict[str, Any]]:
        """Read JSONL journal and return entries from the last N distinct runs."""
        try:
            lines = self.config.journal_path.read_text().strip().splitlines()
        except OSError:
            return []

        entries: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not entries:
            return []

        # Determine the last N distinct run_ids (preserving order of appearance).
        seen_runs: list[str] = []
        seen_set: set[str] = set()
        for entry in reversed(entries):
            rid = entry.get("run_id", "")
            if rid and rid not in seen_set:
                seen_set.add(rid)
                seen_runs.append(rid)
            if len(seen_runs) >= lookback_runs:
                break

        allowed = set(seen_runs)
        return [e for e in entries if e.get("run_id", "") in allowed]
