"""Continuous learning - evolution journal, experiment tracking, and harness proposals.

Records factory run outcomes and extracts recurring failure patterns across runs.
Inspired by AutoResearchClaw's evolution directory and autoresearch-agents' results.tsv.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ralph_py.factory import FactoryResult
    from ralph_py.manifest import Component, Manifest


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


@dataclass
class FailurePattern:
    description: str
    frequency: int
    total_components: int
    affected_components: list[str]
    check_name: str  # e.g. "test_suite", "typecheck", "linter", "review"
    error_signature: str  # normalized error pattern (e.g. "S608" for ruff, "missing-argument" for pytest)
    category: str  # "verification", "review", "contract", "iteration"


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


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    ) -> None:
        """Record a completed factory run to the journal.

        Writes individual component outcomes as JSONL entries.
        Also appends a summary line to experiments.tsv.
        """
        from ralph_py.manifest import ComponentStatus

        timestamp = _timestamp_now()

        # --- JSONL entries per component ---
        entries: list[dict] = []
        for comp in manifest.components:
            has_error = bool(comp.error) and comp.status in (
                ComponentStatus.FAILED.value,
                ComponentStatus.PENDING.value,  # retried components reset to pending
            )
            check_name = ""
            error_sig = ""
            if has_error:
                check_name, _ = _classify_check(comp.error)
                error_sig = _normalize_error(comp.error)
            entry = {
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
                "duration_seconds": comp.duration_seconds,
                "iteration_count": comp.iteration_count,
            }
            entries.append(entry)

        try:
            self.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.journal_path, "a") as f:
                for entry in entries:
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass

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

        # Most common failure signature
        failure_sigs: dict[str, int] = {}
        for comp in manifest.components:
            if comp.status == ComponentStatus.FAILED.value and comp.error:
                sig = _normalize_error(comp.error)
                failure_sigs[sig] = failure_sigs.get(sig, 0) + 1
        common_failure = max(failure_sigs, key=failure_sigs.get, default="") if failure_sigs else ""  # type: ignore[arg-type]

        header = (
            "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
            "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure"
        )
        row = (
            f"{run_id}\t{timestamp}\t{manifest.project_name}\t{total}\t"
            f"{completed}\t{failed}\t{skipped}\t{avg_iterations:.2f}\t"
            f"{avg_duration:.1f}\t{retry_rate:.2f}\t{common_failure}"
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
        except OSError:
            pass

    # ------------------------------------------------------------------
    # extract_failure_patterns (single run)
    # ------------------------------------------------------------------

    def extract_failure_patterns(
        self,
        manifest: Manifest,
        min_frequency: int = 2,
    ) -> list[FailurePattern]:
        """Extract recurring failure patterns from a single run.

        Looks at failed/retried components to find common error signatures.
        Groups by check_name and error_signature.
        """
        from ralph_py.manifest import ComponentStatus

        # Collect components that failed or were retried.
        troubled: list[Component] = [
            c
            for c in manifest.components
            if c.status == ComponentStatus.FAILED.value or c.retries > 0
        ]

        if not troubled:
            return []

        # Group by (check_name, error_signature).
        groups: dict[tuple[str, str], list[str]] = {}
        sig_errors: dict[tuple[str, str], str] = {}
        for comp in troubled:
            if not comp.error:
                continue
            check_name, category = _classify_check(comp.error)
            sig = _normalize_error(comp.error)
            key = (check_name, sig)
            groups.setdefault(key, []).append(comp.id)
            sig_errors.setdefault(key, comp.error)

        total = len(manifest.components)
        patterns: list[FailurePattern] = []
        for (check_name, sig), comp_ids in groups.items():
            if len(comp_ids) < min_frequency:
                continue
            _, category = _classify_check(sig_errors[(check_name, sig)])
            patterns.append(
                FailurePattern(
                    description=(
                        f"{check_name} failure '{sig}' in {len(comp_ids)}/{total} components"
                    ),
                    frequency=len(comp_ids),
                    total_components=total,
                    affected_components=comp_ids,
                    check_name=check_name,
                    error_signature=sig,
                    category=category,
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

        Reads the journal, groups entries by error_signature,
        returns patterns that appear in >= min_pattern_frequency runs.
        """
        entries = self._read_journal_entries(lookback_runs)
        if not entries:
            return []

        # Group by error_signature across distinct run_ids.
        sig_runs: dict[str, set[str]] = {}
        sig_components: dict[str, list[str]] = {}
        sig_check: dict[str, str] = {}
        sig_error: dict[str, str] = {}

        for entry in entries:
            sig = entry.get("error_signature", "")
            if not sig:
                continue
            run_id = entry.get("run_id", "")
            comp_id = entry.get("component_id", "")
            check = entry.get("check_name", "unknown")
            error = entry.get("error", "")

            sig_runs.setdefault(sig, set()).add(run_id)
            sig_components.setdefault(sig, []).append(comp_id)
            sig_check.setdefault(sig, check)
            sig_error.setdefault(sig, error)

        total_runs = len({e.get("run_id") for e in entries})
        patterns: list[FailurePattern] = []

        for sig, run_ids in sig_runs.items():
            if len(run_ids) < self.config.min_pattern_frequency:
                continue
            _, category = _classify_check(sig_error.get(sig, ""))
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
                    check_name=sig_check.get(sig, "unknown"),
                    error_signature=sig,
                    category=category,
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
    ) -> list[HarnessProposal]:
        """Generate concrete harness improvement proposals from patterns.

        Computational proposals only (no LLM calls):
        - Recurring linter errors - suggest CLAUDE.md convention entry
        - Recurring typecheck patterns - suggest config change
        - Recurring test failures on same module - suggest feedforward focus
        """
        proposals: list[HarnessProposal] = []
        counter = 0

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
                            f"Review finding '{pattern.error_signature}' appeared in "
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

    def save_proposals(
        self,
        proposals: list[HarnessProposal],
        output_dir: Path,
    ) -> list[Path]:
        """Write proposals as markdown files to output_dir.

        Returns list of written file paths.
        """
        written: list[Path] = []
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return written

        for proposal in proposals:
            filename = f"{proposal.id.lower()}.md"
            filepath = output_dir / filename

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
            except OSError:
                pass

        return written

    # ------------------------------------------------------------------
    # get_concern_hit_rate (D8)
    # ------------------------------------------------------------------

    def get_concern_hit_rate(self, lookback_runs: int = 10) -> dict:
        """Aggregate reviewer-concern signal across recent factory runs.

        Returns ``{"runs": N, "components": M, "with_concern": K,
        "by_category": {...}}`` so dashboards can ask "did the
        adversarial reviewer surface anything across the last N runs?"

        Today the evolution journal does not persist concerns as a
        structured field (concerns are rendered into ``component.error``
        or PR-body text). This method returns the LOWER BOUND of
        concern surface based on the existing journal shape; a richer
        implementation would write a dedicated concerns entry per
        component. Tracked as a follow-up in
        docs/adversarial-roadmap.md (Phase E3 - structured findings).
        """
        entries = self._read_journal_entries(lookback_runs)
        runs = len({e.get("run_id", "") for e in entries})
        components = len(entries)
        with_concern = 0
        by_category: dict[str, int] = {}
        for entry in entries:
            error = (entry.get("error") or "").lower()
            # Recognize concern-shaped errors by the reviewer prefix
            # that as_retry_context emits ("FAIL <category>:" or
            # "ADVISORY <category>:"). Best-effort signal only.
            for category in (
                "scope_creep", "security_concern", "test_quality",
                "unrelated_change", "dead_code", "error_handling",
                "copy_paste",
            ):
                if category in error:
                    with_concern += 1
                    by_category[category] = by_category.get(category, 0) + 1
                    break
        return {
            "runs": runs,
            "components": components,
            "with_concern": with_concern,
            "by_category": by_category,
        }

    # ------------------------------------------------------------------
    # get_experiment_trends
    # ------------------------------------------------------------------

    def get_experiment_trends(self, last_n: int = 10) -> list[dict]:
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

    def _read_journal_entries(self, lookback_runs: int = 10) -> list[dict]:
        """Read JSONL journal and return entries from the last N distinct runs."""
        try:
            lines = self.config.journal_path.read_text().strip().splitlines()
        except OSError:
            return []

        entries: list[dict] = []
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
