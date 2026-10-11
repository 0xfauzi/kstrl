"""Per-component semantic knowledge layer.

A third memory surface (alongside evolution.jsonl and progress.txt) that
captures durable facts about *the artifact being built*: interfaces,
invariants, contracts, gotchas. Written after Phase 2 review passes and
read by downstream components as part of the prompt context.

Design constraints (see plans/zazzy-orbiting-sketch.md for rationale):

- Atomic-fact files - never a single growing doc.
- Retrieval unions every run dir per component with per-fact-id
  latest-wins: a newer run supersedes only the fact ids it re-emits, so
  the DISTILL rule "do not duplicate existing facts" preserves the
  corpus instead of hiding it, and a failed distill can never erase
  previously written facts.
- No LLM-driven consolidation or rewriting of existing facts. This is a
  permanent design decision motivated by reports of memory-update
  degradation in LLM-driven memory systems.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT, append_records
from kstrl.fact_scope import (
    FACT_TIERS,
    TIER_CORE,
    TIER_DEPENDENCY,
    TIER_SIBLING,
    is_stale,
    tier_for,
)
from kstrl.jsonread import read_json
from kstrl.knowledge_facts import (
    Fact,
    KnowledgeConfig,
    _first_sentence,
    _read_every_fact,
    _render_fact_md,
    read_facts,
)

if TYPE_CHECKING:
    from kstrl.manifest import Component, Manifest


# ---------------------------------------------------------------------------
# Retrieval / context building
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token count - 4 chars per token, matching codebase scan convention."""
    return max(1, len(text) // 4)


def _sort_for_packing(facts: list[Fact]) -> list[Fact]:
    """Sort facts: verified before asserted, then newest run first."""
    # sorted() and list.sort() are stable: secondary sort first, then primary.
    by_recency = sorted(facts, key=lambda f: f.created_run_id, reverse=True)
    # Sort: test_verified first, then review_passed, then asserted.
    # Higher confidence packs first so the budget keeps the strongest.
    _rank = {"test_verified": 0, "review_passed": 1, "verified": 1, "asserted": 2}
    by_recency.sort(key=lambda f: _rank.get(f.confidence, 3))
    return by_recency


def _pack_facts_full(
    facts: list[Fact],
    budget_tokens: int,
) -> tuple[list[Fact], bool]:
    """Pack facts into budget. Returns (kept_facts, overflowed).

    Drops asserted before verified, then oldest first. The first
    overflowing fact may have its body truncated to fit the remaining
    budget if doing so buys more than 30 tokens of payload. Subsequent
    smaller facts that still fit are kept (no greedy abort).
    """
    if not facts:
        return [], False
    ordered = _sort_for_packing(facts)
    kept: list[Fact] = []
    used = 0
    overflowed = False
    truncation_used = False
    for fact in ordered:
        rendered = _render_fact_md(fact)
        cost = _estimate_tokens(rendered)
        if used + cost <= budget_tokens:
            kept.append(fact)
            used += cost
            continue

        # Doesn't fit at full size. Try truncating once (only the first
        # overflowing fact gets this treatment) to soak up remaining
        # budget; subsequent overflowing facts are dropped but smaller
        # facts after them may still fit.
        if not truncation_used:
            meta_only = _render_fact_md(replace(fact, claim=""))
            meta_cost = _estimate_tokens(meta_only)
            remaining = budget_tokens - used - meta_cost
            if remaining > 30:
                chars_available = remaining * 4
                truncated_claim = fact.claim[: chars_available - 20].rstrip() + "..."
                kept.append(replace(fact, claim=truncated_claim))
                used = budget_tokens
                truncation_used = True
        overflowed = True
        # Don't break: smaller subsequent facts may still fit.

    if len(kept) < len(facts):
        overflowed = True
    return kept, overflowed


def _pack_facts_summary(
    facts: list[Fact],
    budget_tokens: int,
) -> tuple[list[Fact], bool]:
    """Pack first-sentence summaries into budget.

    Returns ``(kept_facts_with_summary_claim, overflowed)``. A summary
    that doesn't fit is dropped, but the loop continues so smaller
    subsequent summaries are still considered.
    """
    if not facts:
        return [], False
    ordered = _sort_for_packing(facts)
    kept: list[Fact] = []
    used = 0
    overflowed = False
    for fact in ordered:
        summary = _first_sentence(fact.claim)
        summary_fact = replace(fact, claim=summary)
        rendered = _render_fact_md(summary_fact)
        cost = _estimate_tokens(rendered)
        if used + cost <= budget_tokens:
            kept.append(summary_fact)
            used += cost
        else:
            overflowed = True
            # Don't break: smaller subsequent summaries may still fit.
    if len(kept) < len(facts):
        overflowed = True
    return kept, overflowed


# The three prefix tiers are named once, in `kstrl.fact_scope`. The
# renderer below and the fact-utilization parser
# (`_extract_prefix_claims`) both resolve section titles through them,
# because the two drifting apart would silently mis-bin claims: per-tier
# counts would keep summing to the right total while attributing facts
# to the wrong tier, and nothing would fail. `_tier_for_section` is the
# single decoder.

# The core title carries the component id, so it is matched by prefix.
_CORE_SECTION_PREFIX = "Current component ("
_DEPENDENCY_SECTION_TITLE = "Dependencies"
_SIBLING_SECTION_TITLE = "Other components (summary)"


def _core_section_title(component_id: str) -> str:
    return f"{_CORE_SECTION_PREFIX}{component_id})"


def _tier_for_section(title: str) -> str:
    """Map a rendered ``### `` heading back to its tier.

    Returns ``""`` for a heading this module did not write. Such claims
    still count toward the totals but are attributed to no tier, so the
    per-tier counts can sum to less than ``injected`` - deliberately, an
    unrecognized section is not silently folded into a real tier.
    """
    if title.startswith(_CORE_SECTION_PREFIX):
        return TIER_CORE
    if title == _DEPENDENCY_SECTION_TITLE:
        return TIER_DEPENDENCY
    if title == _SIBLING_SECTION_TITLE:
        return TIER_SIBLING
    return ""


def _format_section(title: str, facts: list[Fact]) -> str:
    if not facts:
        return ""
    lines: list[str] = [f"### {title}"]
    for fact in facts:
        scope_label = f"[{fact.scope}]" if fact.scope else ""
        evidence_label = f" (evidence: {', '.join(fact.evidence)})" if fact.evidence else ""
        confidence_label = f" {{{fact.confidence}}}"
        lines.append(
            f"- **{fact.component_id}**{scope_label}{confidence_label}: "
            f"{fact.claim}{evidence_label}"
        )
    return "\n".join(lines)


#: H3 (#303): fragments build_knowledge_context assembles; versioned as
#: one body (docs/adversarial-roadmap.md, H3a sweep row).
KNOWLEDGE_CONTEXT_PROMPT_VERSION = "1.0.0"

KNOWLEDGE_CONTEXT_PROMPT = (
    "## Component Knowledge\n"
    "Durable facts captured from prior successful iterations. Treat as"
    " ground truth unless contradicted by the current diff."
)
KNOWLEDGE_OVERFLOW_PROMPT = (
    "*Note: {tiers} tier{plural} exceeded the token budget; some facts were truncated or dropped.*"
)


def _dependency_ids(
    manifest: Manifest,
    component_id: str,
    knowledge_root: Path,
    config: KnowledgeConfig,
) -> set[str]:
    """The dependency ids whose facts go in the full-text tier.

    Scope controlled by the E8 config flag. Always computes the
    transitive set: the direct subset is used in direct mode, and the
    delta is recorded as E8 telemetry, so a downstream consumer can
    detect "I switched to direct scope and silently lost N facts per
    build". The withheld facts still appear in the sibling summaries.
    """
    transitive_dep_ids = _transitive_dependencies(manifest, component_id)
    if config.dependency_scope == "transitive":
        return transitive_dep_ids
    direct_dep_ids = _direct_dependencies(manifest, component_id)
    excluded_dep_ids = transitive_dep_ids - direct_dep_ids
    if excluded_dep_ids:
        withheld_facts = sum(len(read_facts(knowledge_root, dep_id)) for dep_id in excluded_dep_ids)
        record_dependency_scope_gap(
            component_id=component_id,
            excluded_dep_count=len(excluded_dep_ids),
            withheld_fact_count=withheld_facts,
            knowledge_root=knowledge_root,
        )
    return direct_dep_ids


def build_knowledge_context(
    manifest: Manifest,
    component: Component,
    knowledge_root: Path,
    config: KnowledgeConfig,
    *,
    allowed_paths: Sequence[str] | None = None,
    dependency_paths: Mapping[str, Sequence[str]] | None = None,
    worktree: Path | None = None,
) -> str:
    """Build the three-tier knowledge prefix for a component's prompt.

    Reads every fact stored under ``knowledge_root``, whichever manifest
    wrote it (#517), and places each by :func:`kstrl.fact_scope.tier_for`.
    Tiers (each token-capped from config):
    - Core: full text of facts written under ``component``, or citing a
      path inside ``allowed_paths``.
    - Dependency: full text of facts written under a direct (or
      transitive under opt-in) dependency, or citing a path inside that
      dependency's entry in ``dependency_paths``.
    - Sibling: first-sentence summary of every other fact.

    ``allowed_paths`` of None matches by component id only. With a
    ``worktree``, a fact from a component outside this manifest is
    dropped when none of the paths it cites exists there. A fact of this
    manifest's own components is never dropped that way: its code may
    not have reached the base branch yet (``--no-prs``), and the fact is
    then all a dependent learns about it.

    Returns the empty string when there is nothing to surface or when
    knowledge is disabled. Side-effect: when ``dependency_scope=direct``
    and the gap between direct and transitive is non-zero, records an
    E8 telemetry event via :func:`record_dependency_scope_gap` so that
    silent regressions are detectable from the evolution journal.
    """
    if not config.enabled:
        return ""
    if not knowledge_root.is_dir():
        return ""

    dep_ids = _dependency_ids(manifest, component.id, knowledge_root, config)
    manifest_ids = {comp.id for comp in manifest.components}
    tiers: dict[str, list[Fact]] = {tier: [] for tier in FACT_TIERS}
    for owner, fact in _read_every_fact(knowledge_root):
        if worktree is not None and owner not in manifest_ids and is_stale(fact, worktree):
            continue
        tier = tier_for(owner, fact, component.id, allowed_paths, dep_ids, dependency_paths or {})
        tiers[tier].append(fact)

    core_kept, core_overflow = _pack_facts_full(tiers[TIER_CORE], config.max_core_tokens)
    dep_kept, dep_overflow = _pack_facts_full(
        tiers[TIER_DEPENDENCY],
        config.max_dependency_tokens,
    )
    sibling_kept, sibling_overflow = _pack_facts_summary(
        tiers[TIER_SIBLING],
        config.max_sibling_tokens,
    )

    if not (core_kept or dep_kept or sibling_kept):
        return ""

    parts: list[str] = [KNOWLEDGE_CONTEXT_PROMPT]
    parts.append("")

    core_section = _format_section(_core_section_title(component.id), core_kept)
    if core_section:
        parts.append(core_section)
        parts.append("")
    dep_section = _format_section(_DEPENDENCY_SECTION_TITLE, dep_kept)
    if dep_section:
        parts.append(dep_section)
        parts.append("")
    sibling_section = _format_section(_SIBLING_SECTION_TITLE, sibling_kept)
    if sibling_section:
        parts.append(sibling_section)
        parts.append("")

    overflow_flags: list[str] = []
    if core_overflow:
        overflow_flags.append("core")
    if dep_overflow:
        overflow_flags.append("dependency")
    if sibling_overflow:
        overflow_flags.append("sibling")
    if overflow_flags:
        parts.append(
            KNOWLEDGE_OVERFLOW_PROMPT.format(
                tiers=", ".join(overflow_flags),
                plural="s" if len(overflow_flags) > 1 else "",
            )
        )
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def retrieve_knowledge_context(
    manifest: Manifest,
    component: Component,
    config: KnowledgeConfig,
    *,
    allowed_paths: Sequence[str] | None,
    worktree: Path,
    on_failure: Callable[[Exception], None],
    dependency_paths: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    """The non-fatal, never-silent retrieval policy every engineer-loop
    caller shares (#599 A3).

    ``factory._submit_args`` and ``feature_cmd._feature_knowledge_prefix``
    duplicated this exact policy - disabled or a raised exception both
    mean "the engineer runs with no facts", warned through ``on_failure``
    on the exception, never on the disabled path (disabled is the
    operator's own choice, not a degradation to report) - and the
    feature copy dropped the distinction entirely, calling
    ``build_knowledge_context`` (which already returns ``""`` when
    disabled, at the check this duplicated a second time) with no
    caller left able to tell "off" from "on and empty" apart. Callers
    that need that distinction, such as the factory's fact-utilization
    measurement, read it off THIS function's return: ``None`` for
    disabled-or-failed, the built string otherwise (which may itself be
    ``""`` - a real empty result, a cold knowledge store, not an
    absence).

    Never raises: knowledge is a hint the engineer's prompt carries, not
    a gate any run depends on.
    """
    if not config.enabled:
        return None
    try:
        return build_knowledge_context(
            manifest,
            component,
            config.knowledge_root,
            config,
            allowed_paths=allowed_paths,
            dependency_paths=dependency_paths,
            worktree=worktree,
        )
    except Exception as exc:  # noqa: BLE001 - non-fatal, never silent
        on_failure(exc)
        return None


# ---------------------------------------------------------------------------
# E8 telemetry: surface "direct scope hid these facts" so silent quality
# regressions become visible.
# ---------------------------------------------------------------------------


_E8_TELEMETRY_RELATIVE_PATH = Path("_e8_dependency_scope.jsonl")


def record_dependency_scope_gap(
    component_id: str,
    excluded_dep_count: int,
    withheld_fact_count: int,
    knowledge_root: Path,
) -> None:
    """Append an E8 telemetry event to
    ``<knowledge_root>/_e8_dependency_scope.jsonl``.

    Each line records that ``component_id`` was built with
    ``dependency_scope=direct`` while ``excluded_dep_count`` transitive
    deps carried ``withheld_fact_count`` full-text facts that were
    demoted to first-sentence sibling summaries. Persistent non-zero
    values across runs are the signal that direct scope is dropping
    information real workflows need. The default empty signal is the
    healthy state.

    Atomic-append best-effort -- write failures are swallowed (telemetry
    must never block a factory run).

    #331: through ``appendio``, which repairs an unterminated tail
    before appending onto it. Without that, a crash mid-write cost the
    NEXT row as well as the torn one, measured through
    :func:`read_dependency_scope_telemetry`: ``['alpha']`` where alpha
    and beta were both recorded, and ``['a1']`` where a2 had lost only
    its newline and a3 was appended onto it.

    A repair ROW rather than a bare pad, because nothing counts these
    rows against anything. The inbox takes a pad because its unreadable
    lines are charged to an admission cap; this reader has no production
    consumer at all, so a row costs nothing and is the durable trace
    that a write here was interrupted.

    No lock. ``build_knowledge_context`` is the only caller and runs in
    the ORCHESTRATOR process: ``factory._submit_args`` calls it on the
    scheduler thread before ``executor.submit``, so it is never reached
    from a worker, and the run-level ``factory.lock`` excludes a second
    orchestrator on the same root. Its other caller,
    ``feature_cmd._feature_knowledge_prefix`` (#599), passes a
    one-component manifest with no dependencies, so no dependency is
    excluded and that path never reaches this writer.

    The ``"a+b"`` open widens what can fail - a telemetry log this
    process can write but not read is refused rather than appended to
    blind - and the ``OSError`` handler below is what that widening
    lands in, unchanged.
    """
    if excluded_dep_count == 0 and withheld_fact_count == 0:
        return
    record = {
        "timestamp": _telemetry_timestamp(),
        "component_id": component_id,
        "excluded_dep_count": excluded_dep_count,
        "withheld_fact_count": withheld_fact_count,
    }
    repair = {"timestamp": _telemetry_timestamp(), "event": JOURNAL_REPAIR_EVENT}
    target = knowledge_root / _E8_TELEMETRY_RELATIVE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        append_records(
            target,
            json.dumps(record, separators=(",", ":")) + "\n",
            repair=json.dumps(repair, separators=(",", ":")) + "\n",
        )
    except OSError:
        pass


def read_dependency_scope_telemetry(knowledge_root: Path) -> list[dict[str, Any]]:
    """Read the JSONL log produced by
    :func:`record_dependency_scope_gap`. Returns ``[]`` when the file
    does not exist or cannot be parsed.

    ONE clause for the open failure and the decode failure, because this
    telemetry reader reports to nobody: both answers are ``[]`` and no
    caller can tell them apart, so two handlers would be a distinction
    with no observer. Sites that DO say something get their own message;
    see :func:`read_facts` above.
    """
    target = knowledge_root / _E8_TELEMETRY_RELATIVE_PATH
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(read_json(line))
            except json.JSONDecodeError:
                continue
    except (OSError, UnicodeDecodeError):
        return []
    return out


def _telemetry_timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _direct_dependencies(manifest: Manifest, component_id: str) -> set[str]:
    """Return the set of directly-declared dependency IDs for ``component_id``.

    This is what ``Component.dependencies`` says in the manifest, no
    transitive walk. Used by E8's default "direct" dependency scope to
    avoid flooding downstream components' prompts with full-text facts
    from transitive deps the consumer does not actually import.
    """
    for comp in manifest.components:
        if comp.id == component_id:
            return {dep for dep in comp.dependencies if dep != component_id}
    return set()


def _transitive_dependencies(manifest: Manifest, component_id: str) -> set[str]:
    """Return the set of transitive dependency component IDs for ``component_id``."""
    dep_ids: set[str] = set()
    visited: set[str] = set()
    queue: list[str] = [component_id]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        for comp in manifest.components:
            if comp.id != current:
                continue
            for dep in comp.dependencies:
                if dep not in dep_ids:
                    dep_ids.add(dep)
                    queue.append(dep)
            break
    dep_ids.discard(component_id)
    return dep_ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PREFIX_CLAIM_RE = re.compile(
    r"^- \*\*[^\*]+\*\*\[[^\]]+\]\s+\{[^\}]+\}:\s*(.+?)(?:\s*\(evidence:|\s*$)",
)


_PREFIX_SECTION_RE = re.compile(r"^###\s+(.+?)\s*$")


def _extract_prefix_claims(knowledge_prefix: str) -> list[tuple[str, str]]:
    """Return ``(claim, tier)`` pairs from a rendered knowledge prefix.

    Used by the fact-utilization metric to look for downstream
    references. ``tier`` is one of :data:`FACT_TIERS`, or ``""`` for a
    claim under a heading this module did not write (including a claim
    that appears before any heading).
    """
    claims: list[tuple[str, str]] = []
    tier = ""
    for line in knowledge_prefix.splitlines():
        section = _PREFIX_SECTION_RE.match(line)
        if section:
            tier = _tier_for_section(section.group(1))
            continue
        m = _PREFIX_CLAIM_RE.match(line)
        if m:
            claim = m.group(1).strip()
            if claim:
                claims.append((claim, tier))
    return claims


def diff_added_content(diff_text: str) -> str:
    """Return only the ADDED lines of a unified diff, `+` stripped.

    Utilization must be matched against what the engineer WROTE. A raw
    diff also carries deletion lines and unchanged context, and a claim
    found in either is not evidence the fact was used: deleting the code
    that expressed a fact, or editing near it, would otherwise score as
    referencing it and satisfy the nonzero-utilization gate. That is a
    false POSITIVE, which is the one error direction a lower-bound
    metric must not have.

    ``+++ `` file headers are excluded so a path cannot match. A line
    whose added content itself begins with ``++ `` is dropped with them;
    that is a false negative, the safe direction.
    """
    return "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++ ")
    )


def measure_fact_utilization(
    knowledge_prefix: str,
    *artifacts: str,
    diff: str = "",
    snippet_length: int = 30,
) -> dict[str, int]:
    """Approximate fact-utilization metric.

    Returns ``{"injected": N, "referenced": M}`` plus a
    ``"<tier>_injected"`` / ``"<tier>_referenced"`` pair for each of
    :data:`FACT_TIERS`. N is the number of fact claims injected via
    ``knowledge_prefix``; M is the count whose 30-char first-sentence
    snippet appears as a substring in the searched text.

    Pass a unified diff as ``diff``, NOT as an artifact: it is reduced
    to its added lines by :func:`diff_added_content` first. Keeping it a
    separate parameter is deliberate - a raw diff handed in through
    ``artifacts`` would silently match deletions and context again, and
    the signature is the only place that can prevent it. ``artifacts``
    is for plain text such as the component's progress log, where the
    engineer writing about a fact IS the signal.

    The substring match is crude: LLMs paraphrase, so a false negative
    just means we under-count. The metric surfaces the lower bound of
    utilization, it does not measure semantic understanding.

    The per-tier split exists because the totals are biased. The
    sibling tier carries first-sentence summaries of EVERY other
    component's facts, which are the claims this component is least
    likely to echo, so they inflate the denominator of the overall
    ratio. Reading ``core_referenced / core_injected`` asks the sharper
    question: did the engineer use what was known about the component
    it was actually building? Per-tier counts can sum to less than the
    totals when a prefix carries an unrecognized section.
    """
    claims = _extract_prefix_claims(knowledge_prefix)
    counts = {"injected": 0, "referenced": 0}
    for tier in FACT_TIERS:
        counts[f"{tier}_injected"] = 0
        counts[f"{tier}_referenced"] = 0
    if not claims:
        return counts
    # Case-insensitive substring match: LLMs frequently echo facts with
    # casing changes (e.g. starting a sentence with "The" vs the
    # mid-sentence "the").
    searched = list(artifacts)
    if diff:
        searched.append(diff_added_content(diff))
    haystack = "\n".join(searched).lower()
    for claim, tier in claims:
        snippet = _first_sentence(claim)[:snippet_length].strip().lower()
        hit = bool(snippet) and snippet in haystack
        counts["injected"] += 1
        if hit:
            counts["referenced"] += 1
        if tier:
            counts[f"{tier}_injected"] += 1
            if hit:
                counts[f"{tier}_referenced"] += 1
    return counts


def current_run_id() -> str:
    """Construct a run id for the knowledge layer.

    Format: ``factory-YYYYMMDD-HHMMSS.ffffff-<nonce>`` (see
    ``kstrl.runid`` - the shared minting home since run kinds landed).
    The microsecond field makes same-second ids order deterministically
    by creation time instead of by the random nonce; the nonce still
    guards against collisions inside the same microsecond. Old-format
    second-precision ids (factory.py builds one inline for the
    evolution journal) sort before same-second new-format ids because
    ``-`` < ``.``.
    """
    from kstrl.runid import mint_run_id

    return mint_run_id("factory")
