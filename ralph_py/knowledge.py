"""Per-component semantic knowledge layer.

A third memory surface (alongside evolution.jsonl and progress.txt) that
captures durable facts about *the artifact being built*: interfaces,
invariants, contracts, gotchas. Written after Phase 2 review passes and
read by downstream components as part of the prompt context.

Design constraints (see plans/zazzy-orbiting-sketch.md for rationale):

- Atomic-fact files - never a single growing doc.
- Latest run dir per component wins - simple, no supersede logic needed.
- No LLM-driven consolidation or rewriting of existing facts. This is a
  permanent design decision motivated by reports of memory-update
  degradation in LLM-driven memory systems.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py.decompose import (
    AgentOutputTooLarge,
    _extract_json,
    _select_agent_output,
    collect_agent_output,
)

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.manifest import Component, Manifest


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeConfig:
    """Configuration for the per-component knowledge layer."""

    enabled: bool = True
    knowledge_root: Path = field(default_factory=lambda: Path(".ralph/knowledge"))
    max_core_tokens: int = 2000
    max_dependency_tokens: int = 1000
    max_sibling_tokens: int = 500
    distill_timeout_seconds: float = 300.0
    distill_model: str = ""  # empty = falls back to base config's model
    max_facts_per_distill: int = 7

    @classmethod
    def load(cls, root_dir: Path | None = None) -> KnowledgeConfig:
        """Load configuration with precedence: env > toml > defaults.

        Reads the [knowledge] section from ``<root_dir>/ralph.toml`` if
        present, then applies any matching environment variable overrides.
        Raises ValueError on malformed TOML, matching the error policy of
        :meth:`RalphConfig.load` so the two loaders treat the same file
        consistently.
        """
        import tomllib

        if root_dir is None:
            root_dir = Path.cwd()

        config = cls()
        config.knowledge_root = root_dir / ".ralph" / "knowledge"

        toml_path = root_dir / "ralph.toml"
        data: dict = {}
        if toml_path.exists():
            try:
                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"Invalid TOML in {toml_path}: {exc}") from exc

        section = data.get("knowledge")
        if isinstance(section, dict):
            if "enabled" in section:
                config.enabled = bool(section["enabled"])
            if "max_core_tokens" in section:
                config.max_core_tokens = int(section["max_core_tokens"])
            if "max_dependency_tokens" in section:
                config.max_dependency_tokens = int(section["max_dependency_tokens"])
            if "max_sibling_tokens" in section:
                config.max_sibling_tokens = int(section["max_sibling_tokens"])
            if "distill_timeout_seconds" in section:
                config.distill_timeout_seconds = float(
                    section["distill_timeout_seconds"]
                )
            if "distill_model" in section:
                config.distill_model = str(section["distill_model"])
            if "max_facts_per_distill" in section:
                config.max_facts_per_distill = int(section["max_facts_per_distill"])

        # Env overrides
        if "RALPH_KNOWLEDGE_ENABLED" in os.environ:
            config.enabled = _parse_bool(os.environ["RALPH_KNOWLEDGE_ENABLED"])
        if "RALPH_KNOWLEDGE_MAX_CORE_TOKENS" in os.environ:
            config.max_core_tokens = int(os.environ["RALPH_KNOWLEDGE_MAX_CORE_TOKENS"])
        if "RALPH_KNOWLEDGE_MAX_DEPENDENCY_TOKENS" in os.environ:
            config.max_dependency_tokens = int(
                os.environ["RALPH_KNOWLEDGE_MAX_DEPENDENCY_TOKENS"]
            )
        if "RALPH_KNOWLEDGE_MAX_SIBLING_TOKENS" in os.environ:
            config.max_sibling_tokens = int(
                os.environ["RALPH_KNOWLEDGE_MAX_SIBLING_TOKENS"]
            )
        if "RALPH_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS" in os.environ:
            config.distill_timeout_seconds = float(
                os.environ["RALPH_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS"]
            )
        if "RALPH_KNOWLEDGE_DISTILL_MODEL" in os.environ:
            config.distill_model = os.environ["RALPH_KNOWLEDGE_DISTILL_MODEL"]
        if "RALPH_KNOWLEDGE_MAX_FACTS_PER_DISTILL" in os.environ:
            config.max_facts_per_distill = int(
                os.environ["RALPH_KNOWLEDGE_MAX_FACTS_PER_DISTILL"]
            )

        return config


# ---------------------------------------------------------------------------
# Fact dataclass
# ---------------------------------------------------------------------------


VALID_SCOPES = frozenset(
    {"handler", "adapter", "schema", "contract", "invariant", "gotcha"}
)
# E5: confidence taxonomy.
# - "review_passed" replaces "verified" - more honest about what the
#   signal actually means (Phase 2 review LLM said pass; not "this is
#   true").
# - "test_verified" is a stronger claim - the fact cites at least one
#   passing test as evidence. Reserved for distillation paths that
#   confirm the claim against test output, not just review verdicts.
# - "asserted" is the bottom of the scale; the LLM stated the claim
#   but no review or test confirmed it.
# "verified" is kept as a backward-compat alias so old fact files on
# disk continue to load. New facts should use review_passed.
VALID_CONFIDENCES = frozenset({
    "review_passed", "test_verified", "asserted", "verified",
})
# Map legacy values to the canonical name on read.
_CONFIDENCE_ALIASES: dict[str, str] = {"verified": "review_passed"}


@dataclass
class Fact:
    """An atomic, durable semantic fact about a component."""

    id: str
    component_id: str
    created_iter: int
    created_run_id: str
    scope: str
    evidence: list[str]
    confidence: str
    claim: str
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialization (markdown with JSON frontmatter)
# ---------------------------------------------------------------------------


_FRONTMATTER_DELIMITER = "---"


def _render_fact_md(fact: Fact) -> str:
    """Render a fact as a markdown file with a one-line JSON frontmatter block."""
    meta = {
        "id": fact.id,
        "component_id": fact.component_id,
        "created_iter": fact.created_iter,
        "created_run_id": fact.created_run_id,
        "scope": fact.scope,
        "evidence": fact.evidence,
        "confidence": fact.confidence,
        "tags": fact.tags,
    }
    return (
        f"{_FRONTMATTER_DELIMITER}\n"
        f"{json.dumps(meta, separators=(',', ':'))}\n"
        f"{_FRONTMATTER_DELIMITER}\n\n"
        f"{fact.claim.rstrip()}\n"
    )


def _parse_fact_md(content: str) -> Fact:
    """Parse a fact markdown file. Raises ValueError on malformed input."""
    lines = content.split("\n")
    if len(lines) < 3 or lines[0] != _FRONTMATTER_DELIMITER:
        raise ValueError("missing opening frontmatter delimiter")

    # Find the closing delimiter
    closing_idx: int | None = None
    for i in range(2, len(lines)):
        if lines[i] == _FRONTMATTER_DELIMITER:
            closing_idx = i
            break
    if closing_idx is None:
        raise ValueError("missing closing frontmatter delimiter")

    meta_json = "\n".join(lines[1:closing_idx])
    try:
        meta = json.loads(meta_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frontmatter is not valid JSON: {exc}") from exc

    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a JSON object")

    claim_lines = lines[closing_idx + 1:]
    # Drop leading blank lines
    while claim_lines and not claim_lines[0].strip():
        claim_lines = claim_lines[1:]
    claim = "\n".join(claim_lines).rstrip("\n")

    return Fact(
        id=str(meta.get("id", "")),
        component_id=str(meta.get("component_id", "")),
        created_iter=int(meta.get("created_iter", 0)),
        created_run_id=str(meta.get("created_run_id", "")),
        scope=str(meta.get("scope", "")),
        evidence=[str(e) for e in meta.get("evidence", []) if isinstance(e, str)],
        confidence=_CONFIDENCE_ALIASES.get(
            str(meta.get("confidence", "asserted")),
            str(meta.get("confidence", "asserted")),
        ),
        tags=[str(t) for t in meta.get("tags", []) if isinstance(t, str)],
        claim=claim,
    )


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _latest_run_dir(component_root: Path) -> Path | None:
    """Return the most-recent run directory for a component, or None.

    Run dirs are named like ``factory-YYYYMMDD-HHMMSS`` - lexicographic
    sort matches chronological order.
    """
    if not component_root.is_dir():
        return None
    candidates = [d for d in component_root.iterdir() if d.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name, reverse=True)
    return candidates[0]


def read_facts(knowledge_root: Path, component_id: str) -> list[Fact]:
    """Read all facts for a component from its latest run dir.

    Returns an empty list if the component has no recorded facts, the
    knowledge root does not exist, or files cannot be parsed. Individual
    parse failures are skipped silently (they indicate a corrupted file,
    not a crash-worthy condition).
    """
    component_root = knowledge_root / component_id
    run_dir = _latest_run_dir(component_root)
    if run_dir is None:
        return []

    facts: list[Fact] = []
    for path in sorted(run_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            facts.append(_parse_fact_md(content))
        except ValueError:
            continue
    return facts


def write_facts(
    facts: list[Fact],
    knowledge_root: Path,
    component_id: str,
    run_id: str,
) -> int:
    """Atomically write facts under ``<knowledge_root>/<component_id>/<run_id>/``.

    Returns the number of facts successfully written. Disk failures are
    non-fatal: a fact that fails to write is skipped (no exception
    raised). The directory is created if missing.
    """
    if not facts:
        return 0

    run_dir = knowledge_root / component_id / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0

    written = 0
    for fact in facts:
        content = _render_fact_md(fact)
        target = run_dir / f"{fact.id}.md"
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(run_dir), suffix=".tmp", prefix=f".{fact.id}-",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, str(target))
                written += 1
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            continue

    return written


# ---------------------------------------------------------------------------
# Retrieval / context building
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token count - 4 chars per token, matching feedforward convention."""
    return max(1, len(text) // 4)


# Sentence-end regex: terminal punctuation followed by either end-of-string
# or whitespace + an uppercase ASCII letter. This avoids splitting on
# common abbreviations like "e.g.", "i.e.", "Mr." where the period is
# followed by a lowercase continuation.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s+[A-Z]|\s*$)")


def _first_sentence(claim: str) -> str:
    """Return the first sentence of a claim.

    Splits on '.', '!', or '?' only when followed by whitespace + a
    capital letter (or end of string). This is a heuristic - it
    deliberately keeps abbreviations like "e.g." intact at the cost of
    occasionally missing a real boundary when the next sentence starts
    with a lowercase word.
    """
    stripped = claim.strip()
    if not stripped:
        return ""
    match = _SENTENCE_END_RE.search(stripped)
    if match is None:
        return stripped
    return stripped[: match.end()].rstrip()


def _sort_for_packing(facts: list[Fact]) -> list[Fact]:
    """Sort facts: verified before asserted, then newest run first."""
    # Python sort is stable: secondary sort first, then primary.
    by_recency = sorted(facts, key=lambda f: f.created_run_id, reverse=True)
    # Sort: test_verified first, then review_passed, then asserted.
    # Higher confidence packs first so the budget keeps the strongest.
    _rank = {"test_verified": 0, "review_passed": 1, "verified": 1, "asserted": 2}
    by_recency.sort(key=lambda f: _rank.get(f.confidence, 3))
    return by_recency


def _pack_facts_full(
    facts: list[Fact], budget_tokens: int,
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
                truncated_claim = (
                    fact.claim[: chars_available - 20].rstrip() + "..."
                )
                kept.append(replace(fact, claim=truncated_claim))
                used = budget_tokens
                truncation_used = True
        overflowed = True
        # Don't break: smaller subsequent facts may still fit.

    if len(kept) < len(facts):
        overflowed = True
    return kept, overflowed


def _pack_facts_summary(
    facts: list[Fact], budget_tokens: int,
) -> tuple[list[Fact], bool]:
    """Pack first-sentence summaries into budget. Returns (kept_facts_with_summary_claim, overflowed).

    A summary that doesn't fit is dropped, but the loop continues so
    smaller subsequent summaries are still considered.
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


def _format_section(title: str, facts: list[Fact]) -> str:
    if not facts:
        return ""
    lines: list[str] = [f"### {title}"]
    for fact in facts:
        scope_label = f"[{fact.scope}]" if fact.scope else ""
        evidence_label = (
            f" (evidence: {', '.join(fact.evidence)})" if fact.evidence else ""
        )
        confidence_label = f" {{{fact.confidence}}}"
        lines.append(
            f"- **{fact.component_id}**{scope_label}{confidence_label}: "
            f"{fact.claim}{evidence_label}"
        )
    return "\n".join(lines)


def build_knowledge_context(
    manifest: Manifest,
    component: Component,
    knowledge_root: Path,
    config: KnowledgeConfig,
) -> str:
    """Build the three-tier knowledge prefix for a component's prompt.

    Tiers (each token-capped from config):
    - Core: full text of all facts for ``component``.
    - Dependency: full text of facts for every transitive dependency.
    - Sibling: first-sentence summary of facts for every other component.

    Returns the empty string when there is nothing to surface or when
    knowledge is disabled.
    """
    if not config.enabled:
        return ""
    if not knowledge_root.is_dir():
        return ""

    # Core
    core_facts = read_facts(knowledge_root, component.id)
    core_kept, core_overflow = _pack_facts_full(core_facts, config.max_core_tokens)

    # Dependency (transitive closure)
    dep_ids = _transitive_dependencies(manifest, component.id)
    dep_facts: list[Fact] = []
    for dep_id in dep_ids:
        dep_facts.extend(read_facts(knowledge_root, dep_id))
    dep_kept, dep_overflow = _pack_facts_full(
        dep_facts, config.max_dependency_tokens,
    )

    # Sibling: everything else
    excluded = {component.id} | dep_ids
    sibling_facts: list[Fact] = []
    for comp in manifest.components:
        if comp.id in excluded:
            continue
        sibling_facts.extend(read_facts(knowledge_root, comp.id))
    sibling_kept, sibling_overflow = _pack_facts_summary(
        sibling_facts, config.max_sibling_tokens,
    )

    if not (core_kept or dep_kept or sibling_kept):
        return ""

    parts: list[str] = ["## Component Knowledge"]
    parts.append(
        "Durable facts captured from prior successful iterations. Treat as"
        " ground truth unless contradicted by the current diff."
    )
    parts.append("")

    core_section = _format_section(f"Current component ({component.id})", core_kept)
    if core_section:
        parts.append(core_section)
        parts.append("")
    dep_section = _format_section("Dependencies", dep_kept)
    if dep_section:
        parts.append(dep_section)
        parts.append("")
    sibling_section = _format_section("Other components (summary)", sibling_kept)
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
            "*Note: "
            f"{', '.join(overflow_flags)} tier"
            f"{'s' if len(overflow_flags) > 1 else ''}"
            " exceeded the token budget; some facts were truncated or dropped.*"
        )
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


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
# Distillation (Voyager-style post-gate write trigger)
# ---------------------------------------------------------------------------


DISTILL_PROMPT_VERSION = "1.0.0"

DISTILL_PROMPT = """\
You are a knowledge-distillation agent. The implementing agent has just
completed an iteration on a single component, and that work has passed
mechanical verification AND second-opinion review. Your job is to extract
durable semantic facts about WHAT WAS BUILT that downstream components
(and future iterations of this same component) need to know.

You must output ONLY valid JSON (no Markdown, no code fences, no
explanation). Schema:

{{
  "facts": [
    {{
      "id": "fact-001",
      "scope": "handler|adapter|schema|contract|invariant|gotcha",
      "confidence": "review_passed|test_verified|asserted",
      "evidence": ["path/to/file.py:42-58", "tests/test_x.py:101"],
      "tags": ["auth", "tokens"],
      "claim": "A single durable assertion in 1-5 sentences. State it as a fact about the artifact, not the iteration. Cite the evidence inline if helpful."
    }}
  ]
}}

Rules:
1. Facts must be DURABLE - things that will remain true unless the
   component is rewritten. Skip implementation details that change every
   iteration.
2. Facts must be SEMANTIC - what the code DOES or PROMISES, not what
   files exist or what tests were run.
3. Every fact must cite at least one path:line-range in the diff. If you
   cannot ground a claim in concrete evidence, do not write it.
4. confidence values:
   - "test_verified": the claim is backed by at least one passing test cited
     by file:line in evidence. Strongest signal.
   - "review_passed": the implementation passed Phase 2 reviewer judgment
     but no specific test grounds the claim.
   - "asserted": the claim is plausible from the diff but neither tests
     nor the reviewer specifically confirmed it.
5. Maximum {max_facts} facts. If nothing durable was established (e.g.
   pure refactor with no behavioral change), return {{"facts": []}}.
6. fact ids must be unique within this response and match /^fact-\\d{{3}}$/.
7. Do not duplicate existing facts (shown below). If an existing fact is
   wrong or stale, do not "fix" it - that is handled separately. Just
   omit it from your output.

Component being distilled: {component_id}
Title: {component_title}
Description: {component_description}
Dependencies: {dependencies}

================================================================================
ACCEPTANCE CRITERIA (from PRD)
================================================================================

{prd_content}

================================================================================
EXISTING FACTS FROM PRIOR RUNS (do not duplicate)
================================================================================

{existing_facts}

================================================================================
GIT DIFF (the work that just passed verification + review)
================================================================================

{diff_content}
"""


def _summarize_existing_facts(facts: list[Fact]) -> str:
    if not facts:
        return "(none)"
    lines: list[str] = []
    for fact in facts:
        first = _first_sentence(fact.claim) or fact.claim[:120]
        lines.append(f"- [{fact.scope}] {first}")
    return "\n".join(lines)


def _read_prd_text(prd_path: Path) -> str:
    try:
        text = prd_path.read_text(encoding="utf-8")
    except OSError:
        return "(PRD not readable)"
    return text


def _parse_distill_output(raw_output: str) -> list[dict]:
    """Extract the facts array from raw agent output. Returns [] on any failure."""
    try:
        data = _extract_json(raw_output)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    facts = data.get("facts")
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict)]


_FACT_ID_RE = re.compile(r"^fact-\d{3}$")

# Hard cap on claim length. A durable semantic fact rarely needs more
# than a few sentences; longer values are usually the LLM drifting into
# essay mode and are more likely to carry injection payloads.
MAX_CLAIM_LENGTH = 500
MAX_EVIDENCE_ITEMS = 10
MAX_TAG_ITEMS = 8

# Patterns that look like prompt-injection attempts inside a fact body.
# Knowledge facts get rendered verbatim into downstream component prompts,
# so any content that looks like a role marker or "ignore previous
# instructions" gets the fact rejected at write time.
_INJECTION_PATTERNS = [
    re.compile(r"<\s*system\s*>", re.IGNORECASE),
    re.compile(r"</\s*system\s*>", re.IGNORECASE),
    re.compile(r"<\s*assistant\s*>", re.IGNORECASE),
    re.compile(r"<\s*user\s*>", re.IGNORECASE),
    re.compile(r"<\|.*?\|>"),  # OpenAI-style special tokens
    re.compile(r"^\s*#{1,6}\s*instructions?\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bignore\b.{0,30}\b(previous|prior|all)\b.{0,30}\binstructions?\b",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"\bdisregard\b.{0,30}\binstructions?\b",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"\boverride\b.{0,30}\bsystem\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bnew\s+(instructions?|task|goal)\b:", re.IGNORECASE),
]


def _is_injection_attempt(text: str) -> bool:
    """Return True when the text matches any known prompt-injection pattern."""
    return any(pat.search(text) for pat in _INJECTION_PATTERNS)


def _coerce_facts(
    raw_facts: list[dict],
    component_id: str,
    iteration_count: int,
    run_id: str,
    max_facts: int,
) -> list[Fact]:
    """Validate and coerce raw fact dicts into Fact instances.

    Invalid facts (missing required fields, bad scope, etc.) are skipped
    rather than crashing distillation.
    """
    facts: list[Fact] = []
    seen_ids: set[str] = set()
    for raw in raw_facts:
        if len(facts) >= max_facts:
            break

        fact_id = str(raw.get("id", "")).strip()
        if not _FACT_ID_RE.match(fact_id) or fact_id in seen_ids:
            continue
        scope = str(raw.get("scope", "")).strip()
        if scope not in VALID_SCOPES:
            continue
        confidence = str(raw.get("confidence", "")).strip()
        if confidence not in VALID_CONFIDENCES:
            continue
        evidence_raw = raw.get("evidence", [])
        if not isinstance(evidence_raw, list) or not evidence_raw:
            continue
        evidence = [str(e).strip() for e in evidence_raw if isinstance(e, str)]
        evidence = [e for e in evidence if e][:MAX_EVIDENCE_ITEMS]
        if not evidence:
            continue
        claim = str(raw.get("claim", "")).strip()
        if not claim:
            continue
        # Knowledge facts get rendered verbatim into downstream prompts.
        # Reject anything that looks like a prompt-injection attempt or
        # an out-of-band role marker. Cap claim length to discourage the
        # LLM from smuggling instructions inside a long body.
        if _is_injection_attempt(claim):
            continue
        if len(claim) > MAX_CLAIM_LENGTH:
            claim = claim[:MAX_CLAIM_LENGTH].rstrip() + "..."
        tags_raw = raw.get("tags", [])
        tags = (
            [str(t).strip() for t in tags_raw if isinstance(t, str)]
            if isinstance(tags_raw, list)
            else []
        )
        # Reject injection patterns in tags too; tags get rendered into
        # the prompt as part of the fact frontmatter.
        tags = [t for t in tags if t and not _is_injection_attempt(t)]
        tags = tags[:MAX_TAG_ITEMS]

        facts.append(
            Fact(
                id=fact_id,
                component_id=component_id,
                created_iter=iteration_count,
                created_run_id=run_id,
                scope=scope,
                evidence=evidence,
                confidence=confidence,
                claim=claim,
                tags=tags,
            )
        )
        seen_ids.add(fact_id)
    return facts


def distill_facts(
    agent: Agent,
    component: Component,
    diff_content: str,
    prd_path: Path,
    iteration_count: int,
    run_id: str,
    knowledge_root: Path,
    config: KnowledgeConfig,
    worktree_path: Path,
    review_passed: bool | None,
) -> tuple[int, str]:
    """Run the LLM distillation call and persist any facts returned.

    Returns ``(written_count, status_message)``. Failures are reported in
    the status message rather than raised - distillation is non-fatal.

    ``review_passed`` controls the confidence ceiling: ``True`` allows
    "verified", ``None`` (skip mode) and ``False`` (shouldn't happen but
    handled defensively) cap at "asserted".
    """
    if not config.enabled:
        return 0, "knowledge.disabled"

    # Truncate diff to match review.py's 50KB convention
    diff_for_prompt = diff_content
    if len(diff_for_prompt) > 50000:
        diff_for_prompt = diff_for_prompt[:50000] + "\n... (diff truncated at 50KB)"

    existing = read_facts(knowledge_root, component.id)

    prompt = DISTILL_PROMPT.format(
        max_facts=config.max_facts_per_distill,
        component_id=component.id,
        component_title=component.title,
        component_description=component.description,
        dependencies=", ".join(component.dependencies) or "(none)",
        prd_content=_read_prd_text(prd_path),
        existing_facts=_summarize_existing_facts(existing),
        diff_content=diff_for_prompt,
    )

    try:
        output_lines = collect_agent_output(
            agent, prompt, cwd=worktree_path,
            timeout=config.distill_timeout_seconds,
        )
    except AgentOutputTooLarge as exc:
        return 0, f"knowledge.agent_output_too_large: {exc}"
    except Exception as exc:  # noqa: BLE001 - non-fatal
        return 0, f"knowledge.agent_error: {exc}"

    streamed_output = "\n".join(output_lines)
    # Select the best candidate: prefer agent.final_message when it
    # parses (codex/claude path), else use streamed (CustomAgent or any
    # agent that emits multi-line JSON without populating final_message
    # with the full response).
    raw_output = _select_agent_output(agent, output_lines)
    final_message = getattr(agent, "final_message", None)

    def _dump_debug(label: str) -> None:
        """Persist raw distiller output so failure modes are diagnosable
        without re-running. Best-effort; ignore disk errors."""
        try:
            debug_dir = knowledge_root / component.id / run_id
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "_distill_raw.txt").write_text(
                streamed_output, encoding="utf-8",
            )
            if final_message and final_message != streamed_output:
                (debug_dir / "_distill_final.txt").write_text(
                    final_message, encoding="utf-8",
                )
            (debug_dir / "_distill_status.txt").write_text(label, encoding="utf-8")
        except OSError:
            pass

    raw_facts = _parse_distill_output(raw_output)
    if not raw_facts:
        # Could be a clean empty response or a parse failure - dump so we
        # can tell which without re-running.
        _dump_debug("no_facts")
        return 0, "knowledge.no_facts"

    facts = _coerce_facts(
        raw_facts, component.id, iteration_count, run_id,
        config.max_facts_per_distill,
    )

    # Confidence ceiling: only Phase-2-passed work earns the
    # review_passed / test_verified tiers. When review is skipped or
    # failed, downgrade any non-asserted claim back to asserted.
    if review_passed is not True:
        facts = [
            replace(f, confidence="asserted")
            if f.confidence in {"review_passed", "test_verified", "verified"}
            else f
            for f in facts
        ]

    if not facts:
        # Surface a brief sample of the rejected raw output so the user
        # can see why coercion failed without grepping the dump.
        sample = raw_output[:200].replace("\n", " ")
        _dump_debug("no_valid_facts")
        return 0, f"knowledge.no_valid_facts (raw: {sample}...)"

    written = write_facts(facts, knowledge_root, component.id, run_id)
    return written, f"knowledge.wrote {written}/{len(facts)} facts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


_PREFIX_CLAIM_RE = re.compile(
    r"^- \*\*[^\*]+\*\*\[[^\]]+\]\s+\{[^\}]+\}:\s*(.+?)(?:\s*\(evidence:|\s*$)",
)


def _extract_prefix_claims(knowledge_prefix: str) -> list[str]:
    """Return the human-readable claim sentences from a rendered
    knowledge prefix. Used by the fact-utilization metric to look for
    downstream references."""
    claims: list[str] = []
    for line in knowledge_prefix.splitlines():
        m = _PREFIX_CLAIM_RE.match(line)
        if m:
            claim = m.group(1).strip()
            if claim:
                claims.append(claim)
    return claims


def measure_fact_utilization(
    knowledge_prefix: str, *artifacts: str, snippet_length: int = 30,
) -> dict[str, int]:
    """Approximate fact-utilization metric.

    Returns ``{"injected": N, "referenced": M}`` where N is the number
    of fact claims injected via ``knowledge_prefix`` and M is the count
    that have a 30-char snippet from their first sentence appearing as
    a substring in any of the provided ``artifacts`` (typically the
    component's git diff and progress.txt).

    The substring match is crude: LLMs paraphrase, so a False
    negative just means we under-count. The metric is meant to surface
    the lower bound of utilization, not measure semantic understanding.
    """
    claims = _extract_prefix_claims(knowledge_prefix)
    if not claims:
        return {"injected": 0, "referenced": 0}
    # Case-insensitive substring match: LLMs frequently echo facts with
    # casing changes (e.g. starting a sentence with "The" vs the
    # mid-sentence "the").
    haystack = "\n".join(artifacts).lower()
    referenced = 0
    for claim in claims:
        snippet = _first_sentence(claim)[:snippet_length].strip().lower()
        if snippet and snippet in haystack:
            referenced += 1
    return {"injected": len(claims), "referenced": referenced}


def current_run_id() -> str:
    """Construct a run id of the same shape factory.py uses for evolution.jsonl.

    Includes a 6-char random nonce so two factory invocations started
    within the same UTC second produce distinct ids.
    """
    import secrets
    return (
        f"factory-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        f"-{secrets.token_hex(3)}"
    )
