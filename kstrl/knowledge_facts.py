"""The fact of the per-component knowledge layer.

Its config, its record, the limits on its fields, the check of a fact
that enters the layer, its markdown form, and the read and write of the
fact files under the knowledge root.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_text
from kstrl.config_numbers import check_numbers
from kstrl.jsonread import read_json

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


_VALID_DEPENDENCY_SCOPES = frozenset({"direct", "transitive"})


@dataclass
class KnowledgeConfig:
    """Configuration for the per-component knowledge layer."""

    enabled: bool = True
    knowledge_root: Path = field(default_factory=lambda: Path(".kstrl/knowledge"))
    max_core_tokens: int = 2000
    max_dependency_tokens: int = 1000
    max_sibling_tokens: int = 500
    distill_timeout_seconds: float = 0.0
    distill_model: str = ""  # empty = falls back to base config's model
    max_facts_per_distill: int = 7
    # E8: scope of the "Dependencies" full-text tier in build_knowledge_context.
    # "direct" surfaces only Component.dependencies (the import surface declared
    # in the manifest). "transitive" walks the full closure. Transitive deps
    # excluded from the full-text tier still appear in the sibling summary,
    # so they are not invisible -- just downgraded. Default is "direct"
    # because the typical reason a component needs a transitive dep's full
    # facts is that the manifest is missing a direct edge.
    dependency_scope: str = "direct"

    def __post_init__(self) -> None:
        if self.dependency_scope not in _VALID_DEPENDENCY_SCOPES:
            raise ValueError(
                f"KnowledgeConfig.dependency_scope must be one of "
                f"{sorted(_VALID_DEPENDENCY_SCOPES)}, got {self.dependency_scope!r}"
            )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> KnowledgeConfig:
        """Load configuration with precedence: env > toml > defaults.

        Reads the [knowledge] section from ``<root_dir>/kstrl.toml`` if
        present, then applies any matching environment variable overrides.
        Raises ``ConfigError`` on malformed TOML through the shared
        ``load_toml_section``, which is where every other loader reads
        its section: a second copy of the open-and-decode was a second
        place for the file's error policy to drift (#272).
        """
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()

        config = cls()
        config.knowledge_root = root_dir / ".kstrl" / "knowledge"

        section = load_toml_section(resolve_config_file(root_dir), "knowledge")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "max_core_tokens" in section:
            config.max_core_tokens = int(section["max_core_tokens"])
        if "max_dependency_tokens" in section:
            config.max_dependency_tokens = int(section["max_dependency_tokens"])
        if "max_sibling_tokens" in section:
            config.max_sibling_tokens = int(section["max_sibling_tokens"])
        if "distill_timeout_seconds" in section:
            config.distill_timeout_seconds = float(section["distill_timeout_seconds"])
        if "distill_model" in section:
            config.distill_model = str(section["distill_model"])
        if "max_facts_per_distill" in section:
            config.max_facts_per_distill = int(section["max_facts_per_distill"])
        if "dependency_scope" in section:
            config.dependency_scope = str(section["dependency_scope"])

        _apply_knowledge_env_overrides(config)
        return check_numbers(config)

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> KnowledgeConfig:
        """Load knowledge config from environment variables only.

        ``knowledge_root`` resolves against ``root_dir`` (the project
        root), matching :meth:`load`; the toml file is not consulted.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        config.knowledge_root = root_dir / ".kstrl" / "knowledge"
        _apply_knowledge_env_overrides(config)
        return config


def _apply_knowledge_env_overrides(config: KnowledgeConfig) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay).

    Re-validates dependency_scope afterwards: env can supply a bad value
    that bypassed ``__post_init__`` at construction time.
    """
    if "KSTRL_KNOWLEDGE_ENABLED" in os.environ:
        config.enabled = _parse_bool(os.environ["KSTRL_KNOWLEDGE_ENABLED"])
    if "KSTRL_KNOWLEDGE_MAX_CORE_TOKENS" in os.environ:
        config.max_core_tokens = int(os.environ["KSTRL_KNOWLEDGE_MAX_CORE_TOKENS"])
    if "KSTRL_KNOWLEDGE_MAX_DEPENDENCY_TOKENS" in os.environ:
        config.max_dependency_tokens = int(os.environ["KSTRL_KNOWLEDGE_MAX_DEPENDENCY_TOKENS"])
    if "KSTRL_KNOWLEDGE_MAX_SIBLING_TOKENS" in os.environ:
        config.max_sibling_tokens = int(os.environ["KSTRL_KNOWLEDGE_MAX_SIBLING_TOKENS"])
    if "KSTRL_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS" in os.environ:
        config.distill_timeout_seconds = float(
            os.environ["KSTRL_KNOWLEDGE_DISTILL_TIMEOUT_SECONDS"]
        )
    if "KSTRL_KNOWLEDGE_DISTILL_MODEL" in os.environ:
        config.distill_model = os.environ["KSTRL_KNOWLEDGE_DISTILL_MODEL"]
    if "KSTRL_KNOWLEDGE_MAX_FACTS_PER_DISTILL" in os.environ:
        config.max_facts_per_distill = int(os.environ["KSTRL_KNOWLEDGE_MAX_FACTS_PER_DISTILL"])
    if "KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE" in os.environ:
        config.dependency_scope = os.environ["KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE"]

    if config.dependency_scope not in _VALID_DEPENDENCY_SCOPES:
        raise ValueError(
            f"KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE must be one of "
            f"{sorted(_VALID_DEPENDENCY_SCOPES)}, "
            f"got {config.dependency_scope!r}"
        )


# ---------------------------------------------------------------------------
# Fact dataclass
# ---------------------------------------------------------------------------


VALID_SCOPES = frozenset({"handler", "adapter", "schema", "contract", "invariant", "gotcha"})
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
VALID_CONFIDENCES = frozenset(
    {
        "review_passed",
        "test_verified",
        "asserted",
        "verified",
    }
)
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
    """Parse and validate a fact markdown file.

    Raises ValueError on malformed input AND on content that fails the
    write-time filters (injection patterns, length/count caps, unknown
    scope/confidence). Fact files are plain markdown on disk between
    runs; without the re-validation here, editing a file after it lands
    would bypass every filter in :func:`_coerce_facts`.
    """
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
        meta = read_json(meta_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frontmatter is not valid JSON: {exc}") from exc

    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a JSON object")

    claim_lines = lines[closing_idx + 1 :]
    # Drop leading blank lines
    while claim_lines and not claim_lines[0].strip():
        claim_lines = claim_lines[1:]
    claim = "\n".join(claim_lines).rstrip("\n")

    try:
        created_iter = int(meta.get("created_iter", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"created_iter is not an integer: {exc}") from exc
    evidence_raw = meta.get("evidence", [])
    if not isinstance(evidence_raw, list):
        raise ValueError("evidence must be a list")
    tags_raw = meta.get("tags", [])
    if not isinstance(tags_raw, list):
        raise ValueError("tags must be a list")

    fact = Fact(
        id=str(meta.get("id", "")),
        component_id=str(meta.get("component_id", "")),
        created_iter=created_iter,
        created_run_id=str(meta.get("created_run_id", "")),
        scope=str(meta.get("scope", "")),
        evidence=[str(e) for e in evidence_raw if isinstance(e, str)],
        confidence=_CONFIDENCE_ALIASES.get(
            str(meta.get("confidence", "asserted")),
            str(meta.get("confidence", "asserted")),
        ),
        tags=[str(t) for t in tags_raw if isinstance(t, str)],
        claim=claim,
    )
    error = _validate_fact_content(fact)
    if error is not None:
        raise ValueError(error)
    return fact


def _validate_fact_content(fact: Fact) -> str | None:
    """Return an error message when a fact violates the write-time
    content filters, else None.

    Runs on READ (via :func:`_parse_fact_md`): the limits mirror the
    write side exactly, so a file that breaches one here did not come
    through :func:`_coerce_facts` intact and is treated as tampered.
    """
    if not _FACT_ID_RE.match(fact.id):
        return f"fact id {fact.id!r} does not match fact-NNN"
    if fact.scope not in VALID_SCOPES:
        return f"unknown scope {fact.scope!r}"
    if fact.confidence not in VALID_CONFIDENCES:
        return f"unknown confidence {fact.confidence!r}"
    if not fact.claim:
        return "empty claim"
    # Write-side truncation appends "..." past the cap, hence the +3.
    if len(fact.claim) > MAX_CLAIM_LENGTH + 3:
        return f"claim longer than {MAX_CLAIM_LENGTH} chars"
    if _is_injection_attempt(fact.claim):
        return "claim matches a prompt-injection pattern"
    if not fact.evidence:
        return "no evidence items"
    if len(fact.evidence) > MAX_EVIDENCE_ITEMS:
        return f"more than {MAX_EVIDENCE_ITEMS} evidence items"
    for item in fact.evidence:
        if len(item) > MAX_EVIDENCE_ITEM_LENGTH:
            return f"evidence item longer than {MAX_EVIDENCE_ITEM_LENGTH} chars"
        if _is_injection_attempt(item):
            return "evidence item matches a prompt-injection pattern"
    if len(fact.tags) > MAX_TAG_ITEMS:
        return f"more than {MAX_TAG_ITEMS} tags"
    for tag in fact.tags:
        if _is_injection_attempt(tag):
            return "tag matches a prompt-injection pattern"
    return None


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


# Debug dumps live under <component_root>/_debug/<run_id>/. The leading
# underscore keeps them out of _run_dirs, so a failed distill (which
# writes only a dump) can never create a run dir that shadows real facts.
_DEBUG_DIR_NAME = "_debug"


def _run_dirs(component_root: Path) -> list[Path]:
    """Return every run directory for a component, oldest first.

    Run dirs are named like ``factory-YYYYMMDD-HHMMSS.ffffff-<nonce>``
    (older runs may lack the microsecond field) - lexicographic sort
    matches chronological order, and an old-format id sorts before a
    new-format id from the same second because ``-`` < ``.``.
    Underscore-prefixed entries (e.g. ``_debug``) are metadata, never
    fact dirs.
    """
    if not component_root.is_dir():
        return []
    candidates = [d for d in component_root.iterdir() if d.is_dir() and not d.name.startswith("_")]
    candidates.sort(key=lambda p: p.name)
    return candidates


def read_facts(knowledge_root: Path, component_id: str) -> list[Fact]:
    """Read a component's facts: the union of every run dir, with
    per-fact-id latest-wins.

    A run supersedes only the fact ids it re-emits. The distill prompt
    forbids re-emitting existing facts, so most runs add new ids; facts
    from prior runs stay visible instead of being hidden by whichever
    directory happens to sort last. Returns an empty list if the
    component has no recorded facts or the knowledge root does not
    exist. A file that fails parsing or content validation is rejected
    with a RuntimeWarning, never a crash - one corrupted or tampered
    file must not take down retrieval.
    """
    component_root = knowledge_root / component_id
    facts_by_id: dict[str, Fact] = {}
    for run_dir in _run_dirs(component_root):
        for path in sorted(run_dir.glob("*.md")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            except UnicodeDecodeError as exc:
                # Rejected with a warning, like a fact file that will not
                # PARSE, and unlike one that will not OPEN. This is a
                # content fault in a file kstrl wrote as ASCII, so it is
                # the same class of event as a malformed heading: worth
                # telling somebody about. #320: without this clause the
                # UnicodeDecodeError - a ValueError - walked past the
                # OSError above and crashed retrieval, which is exactly
                # what this function's docstring promises never happens.
                warnings.warn(
                    f"knowledge: rejected fact file {path}: not valid UTF-8 ({exc})",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            try:
                fact = _parse_fact_md(content)
            except ValueError as exc:
                warnings.warn(
                    f"knowledge: rejected fact file {path}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            facts_by_id[fact.id] = fact
    return list(facts_by_id.values())


def _read_every_fact(knowledge_root: Path) -> list[tuple[str, Fact]]:
    """Every fact stored under the knowledge root, as ``(owner, fact)``.

    ``owner`` is the component directory the fact was read from. Every
    directory is read, not only the current manifest's ids, because a
    component id does not survive a new manifest (#517). Facts are kept
    apart per directory: ``fact-001`` exists in every one of them, so a
    fact's identity is ``(owner, fact.id)`` and never ``fact.id`` alone.
    Underscore-prefixed entries are metadata, as in :func:`_run_dirs`.
    """
    owners = sorted(
        entry.name
        for entry in knowledge_root.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    )
    return [(owner, fact) for owner in owners for fact in read_facts(knowledge_root, owner)]


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
            atomic_write_text(target, content)
        except OSError:
            continue
        written += 1

    return written


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


_FACT_ID_RE = re.compile(r"^fact-\d{3}$")

# Hard cap on claim length. A durable semantic fact rarely needs more
# than a few sentences; longer values are usually the LLM drifting into
# essay mode and are more likely to carry injection payloads.
MAX_CLAIM_LENGTH = 500
MAX_EVIDENCE_ITEMS = 10
# Evidence items are path:line citations ("src/x:42-58"); anything
# longer is essay-mode drift or a smuggled payload, not a citation.
MAX_EVIDENCE_ITEM_LENGTH = 200
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
    re.compile(
        r"\bignore\b.{0,30}\b(previous|prior|all)\b.{0,30}\binstructions?\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\bdisregard\b.{0,30}\binstructions?\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\boverride\b.{0,30}\bsystem\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bnew\s+(instructions?|task|goal)\b:", re.IGNORECASE),
]


def _is_injection_attempt(text: str) -> bool:
    """Return True when the text matches any known prompt-injection pattern."""
    return any(pat.search(text) for pat in _INJECTION_PATTERNS)


def _coerce_facts(
    raw_facts: list[dict[str, Any]],
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
        evidence = [e for e in evidence if e]
        # Evidence is rendered verbatim into downstream "treat as ground
        # truth" prompts (both the fact file and _format_section), so it
        # gets the same injection gate as the claim. Checked before
        # truncation so a payload cannot dodge the patterns by being cut
        # mid-match. One poisoned citation rejects the whole fact: a
        # distiller that smuggles instructions into a citation cannot be
        # trusted about the rest of the fact either.
        if any(_is_injection_attempt(e) for e in evidence):
            continue
        evidence = [e[:MAX_EVIDENCE_ITEM_LENGTH].rstrip() for e in evidence]
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


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}
