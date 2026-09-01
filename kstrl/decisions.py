"""The architect's disposition register (#260).

Every question the architect raised while decomposing a spec has to be
closed one of four ways, and this module is where the closing is
recorded, persisted and delivered:

- ``decided``   - the architect chose, and says what it rejected.
- ``assumed``   - the architect took a default and pinned it with an
  acceptance criterion in the affected component's PRD.
- ``spiked``    - the answer is a fact about the world. Either the
  architect ran one command against a tool already on PATH and recorded
  the answer, or the spike became a real component scheduled ahead of
  the component that needs it.
- ``escalated`` - the architect refuses to choose, because the question
  is a product, scope or risk judgement, or a choice between two
  incompatible architectures that is expensive to unwind.

Only ``escalated`` halts. That is the whole point of the register: five
real runs against a real spec produced 117 findings, 26 of them scored
blocker, and not one of the 26 was a judgement only the owner could
make. The architect already knew most of the answers and had nowhere to
write them down.

The register is written next to ``manifest.json`` and rendered into the
engineer prompt the way ``knowledge.build_knowledge_context`` renders
distilled facts: a per-component context block, own decisions in full,
the rest of the run summarised, the whole thing under one token budget.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_json

# Relative location of the persisted register. Next to manifest.json and
# spec-issues.json so one directory holds every decompose output.
SPEC_DECISIONS_REL_PATH = Path("scripts") / "kstrl" / "decisions.json"

DISPOSITION_ESCALATED = "escalated"

# Escalations first: they are the only disposition that halts, so they
# lead every rendering and every persisted file. Written inline like
# ``decompose._SEVERITY_ORDER``, whose members are also only ever a
# vocabulary rather than values other modules pass around.
DISPOSITION_ORDER = (DISPOSITION_ESCALATED, "decided", "assumed", "spiked")
VALID_DISPOSITIONS = frozenset(DISPOSITION_ORDER)

# Token budget for the whole engineer-facing block, in the 4-chars-per-token
# convention the feedforward and knowledge layers already use.
#
# Measured, not guessed: across the five recorded real runs the architect's
# 117 findings averaged 471 characters of summary plus suggestion plus
# location each, at 20 to 32 findings per run. Rendering those runs
# uncapped, at one, two and three components, gives 8,315 to 17,323 bytes,
# which is two to four times the entire 4,444-byte engineer prompt
# template. So the budget has to bite, and the note below says out loud
# when it did. 2000 tokens matches KnowledgeConfig's max_core_tokens, the
# existing precedent for "one tier of context the engineer must read".
MAX_DECISION_TOKENS = 2000

_SECTION_TITLE = "## Architect Decisions"
# The three tiers, named here rather than built at the call site. Same
# discipline as ``knowledge._CORE_SECTION_PREFIX`` and for the same
# reason: these headings share one prompt string with the knowledge
# block, and a renderer whose titles live inline drifts silently.
_OWN_SECTION_PREFIX = "Decisions binding this component ("
_RUN_WIDE_SECTION_TITLE = "Decisions binding the whole run"
_OTHER_SECTION_TITLE = "Decisions binding other components"


@dataclass(frozen=True)
class SpecDecision:
    """One question the architect closed, and how."""

    question: str
    disposition: str
    resolution: str
    reason: str = ""
    alternative: str = ""
    component: str = ""


def _clean(value: Any) -> str:
    """A raw JSON field as a stripped string, or "" for anything else."""
    return value.strip() if isinstance(value, str) else ""


def parse_decisions(data: Any) -> list[SpecDecision]:
    """Extract typed decisions from raw architect output.

    Malformed entries (unknown disposition, missing question or
    resolution) are skipped rather than crashing decomposition, matching
    ``decompose._parse_spec_issues``. Skipping is safe HERE because the
    validator runs first and rejects a payload whose escalation count
    disagrees with its blocker count, so a dropped escalation is a
    retryable error rather than a silently-cleared halt.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("decisions", [])
    if not isinstance(raw, list):
        return []
    decisions: list[SpecDecision] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        disposition = _clean(entry.get("disposition"))
        question = _clean(entry.get("question"))
        resolution = _clean(entry.get("resolution"))
        if disposition not in VALID_DISPOSITIONS or not question or not resolution:
            continue
        decisions.append(
            SpecDecision(
                question=question,
                disposition=disposition,
                resolution=resolution,
                reason=_clean(entry.get("reason")),
                alternative=_clean(entry.get("alternative")),
                component=_clean(entry.get("component")),
            )
        )
    return decisions


def escalations(decisions: Sequence[SpecDecision]) -> list[SpecDecision]:
    """The decisions that halt the run."""
    return [d for d in decisions if d.disposition == DISPOSITION_ESCALATED]


def _decision_dict(decision: SpecDecision) -> dict[str, str]:
    """One decision as the six JSON keys every artifact writes it under."""
    return {
        "question": decision.question,
        "disposition": decision.disposition,
        "resolution": decision.resolution,
        "reason": decision.reason,
        "alternative": decision.alternative,
        "component": decision.component,
    }


def _decision_counts(decisions: Sequence[SpecDecision]) -> dict[str, int]:
    """Per-disposition counts, in ``DISPOSITION_ORDER``."""
    return {d: sum(1 for x in decisions if x.disposition == d) for d in DISPOSITION_ORDER}


def write_decisions(
    decisions: Sequence[SpecDecision],
    root_dir: Path,
    project_name: str,
    spec_file: str,
) -> Path:
    """Persist the register to ``scripts/kstrl/decisions.json``.

    Written on every decompose that produced parseable output, including
    one that closed nothing: an empty ``decisions`` array is the record
    that the architect had no open question, which is a different fact
    from "no record". Raises ``OSError`` on write failure so the caller
    can surface it loudly.
    """
    path = root_dir / SPEC_DECISIONS_REL_PATH
    payload: dict[str, Any] = {
        "project": project_name,
        "specFile": spec_file,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": _decision_counts(decisions),
        "decisions": [_decision_dict(d) for d in decisions],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return path


def read_decisions(root_dir: Path) -> list[SpecDecision]:
    """Read the register back, or return [] when there is none.

    ``ValueError`` is caught alongside ``OSError`` deliberately: the file
    is JSON read as utf-8, and both ``json.JSONDecodeError`` and
    ``UnicodeDecodeError`` are ``ValueError`` subclasses that would
    otherwise escape a fail-closed ``except OSError``.
    """
    path = root_dir / SPEC_DECISIONS_REL_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return parse_decisions(raw)


def _estimate_tokens(text: str) -> int:
    """Rough token count - 4 chars per token, the harness-wide convention."""
    return max(1, len(text) // 4)


def _render_full(decision: SpecDecision) -> str:
    lines = [f"- **[{decision.disposition}]** {decision.question}"]
    lines.append(f"  - Resolution: {decision.resolution}")
    if decision.reason:
        lines.append(f"  - Because: {decision.reason}")
    if decision.alternative:
        lines.append(f"  - Rejected: {decision.alternative}")
    return "\n".join(lines)


def _render_summary(decision: SpecDecision) -> str:
    return f"- **[{decision.disposition}]** {decision.question} -> {decision.resolution}"


def _pack_section(
    title: str,
    items: Sequence[SpecDecision],
    renderer: Callable[[SpecDecision], str],
    budget: int,
) -> tuple[list[str], int, int]:
    """Render as much of one tier as ``budget`` allows.

    Returns the section's lines, the budget left, and how many items did
    not fit. An item too large for the remaining budget is skipped
    rather than ending the tier, because a later, smaller one may still
    fit - the same choice ``knowledge._pack_facts_full`` makes.
    """
    section: list[str] = []
    dropped = 0
    for item in items:
        block = renderer(item)
        cost = _estimate_tokens(block)
        if cost > budget:
            dropped += 1
            continue
        budget -= cost
        section.append(block)
    if not section:
        return [], budget, dropped
    return [f"### {title}", *section, ""], budget, dropped


def build_decisions_context(
    decisions: Sequence[SpecDecision],
    component_id: str,
    max_tokens: int = MAX_DECISION_TOKENS,
) -> str:
    """The engineer-facing block for one component, or "" when empty.

    Three tiers, packed in this order so that what survives the budget
    is what binds this engineer:

    1. Decisions naming ``component_id``, rendered in full.
    2. Decisions naming no component, rendered in full: the prompt
       defines an empty ``component`` as "binds the whole run", so these
       bind this engineer too and deserve their reason and their
       rejected alternative.
    3. Decisions naming another component, one line each.

    Only tier 3 can be dropped, which is what makes the truncation safe
    to state plainly: nothing binding this component is ever cut. The
    earlier two-tier split put run-wide decisions under "the rest of the
    run", argued least and dropped first, which was backwards.
    """
    if not decisions:
        return ""
    # Escalations first within each tier: if a register somehow reaches
    # an engineer with one in it, it is the line that must survive.
    # Sorted once - Python's sort is stable, so the partitions below
    # inherit the order.
    rank = {name: i for i, name in enumerate(DISPOSITION_ORDER)}
    ordered = sorted(decisions, key=lambda d: rank.get(d.disposition, len(rank)))
    own = [d for d in ordered if d.component == component_id]
    run_wide = [d for d in ordered if not d.component]
    other = [d for d in ordered if d.component and d.component != component_id]

    header = [
        _SECTION_TITLE,
        "",
        "Questions the architect closed while decomposing the spec, and how"
        " it closed them. These are binding: implement what is written here."
        " An `assumed` decision is pinned by an acceptance criterion in a"
        " PRD - if your code cannot honour one, say so in your"
        " Self-Critique rather than deciding differently.",
        "",
    ]
    budget = max_tokens - _estimate_tokens("\n".join(header))
    dropped = 0
    body: list[str] = []
    tiers: tuple[tuple[str, list[SpecDecision], Callable[[SpecDecision], str]], ...] = (
        (f"{_OWN_SECTION_PREFIX}{component_id})", own, _render_full),
        (_RUN_WIDE_SECTION_TITLE, run_wide, _render_full),
        (_OTHER_SECTION_TITLE, other, _render_summary),
    )
    for title, items, renderer in tiers:
        lines, budget, tier_dropped = _pack_section(title, items, renderer, budget)
        body.extend(lines)
        dropped += tier_dropped

    if not body:
        return ""
    parts = header + body
    if dropped:
        # Deliberately does NOT point at decisions.json: the register is
        # written to the project root and the engineer works in a
        # worktree that never receives a copy, so naming the path would
        # send it after a file it cannot open. What it can be told is
        # that nothing it must implement was cut.
        parts.append(
            f"*Note: {dropped} decision(s) about other components did not"
            f" fit the context budget. None of them bind this component.*"
        )
    return "\n".join(parts).rstrip() + "\n"
