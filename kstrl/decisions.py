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

Every decision carries ``issue``, the id of the ``spec_issues`` entry it
closes. That join is what makes the halt gate a gate: round 1 of this
change compared a COUNT of blockers against a COUNT of parsed
escalations, and a disposition of ``"Escalated"`` parsed to nothing, so
both counts fell to zero and agreed. A gate a capital letter disables is
not a gate. Nothing in this module is allowed to turn a malformed entry
into a zero: ``decision_entry_errors`` names the fault and the caller
rejects the payload.

The register is written next to ``manifest.json`` and rendered into the
engineer prompt the way ``knowledge.build_knowledge_context`` renders
distilled facts: a per-component context block under one enrolled
template.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection, Sequence
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

# Register status, as ``read_decisions`` reports it. Three states rather
# than a bool because the factory must treat them differently: a missing
# register is a manifest built before this landed, an unreadable one is
# a mechanism that silently disappeared, and only "ok" may bind an
# engineer.
REGISTER_OK = "ok"
REGISTER_MISSING = "missing"
REGISTER_UNREADABLE = "unreadable"

#: Token budget for the OTHER-component tier of the engineer block, in
#: the 4-chars-per-token convention the feedforward and knowledge layers
#: already use.
#:
#: It bounds that tier ALONE, and the name says so. Round 1 called it
#: ``MAX_DECISION_TOKENS`` and applied it to all three tiers with one
#: greedy loop, which meant the tier ordering protected nothing:
#: measured, 100 own decisions with 300-character questions rendered 22
#: and dropped 78, one oversized own decision rendered an empty block,
#: and the note told the engineer that everything dropped had belonged
#: to another component. Decisions that bind this component are now
#: rendered in full, always, whatever the number, because a truncated
#: binding instruction is worse than a long prompt.
#:
#: The number: across the five recorded real runs the architect's 117
#: findings averaged 471 characters of summary plus suggestion plus
#: location each, at 20 to 32 findings per run, and a whole run rendered
#: uncapped is 8,315 to 17,323 bytes against a 4,444-byte engineer
#: prompt template. Other components' decisions are the part of that an
#: engineer can lose without losing an instruction, so they are the part
#: with a cap.
MAX_OTHER_DECISION_TOKENS = 2000

#: The harness-wide estimate feedforward and knowledge also use.
_CHARS_PER_TOKEN = 4

DECISIONS_CONTEXT_PROMPT_VERSION = "1.0.0"

#: An empty tier renders as nothing at all under its heading, and
#: deliberately NOT as a "(none)" marker: that marker would be a second
#: piece of harness-authored English reaching the engineer, needing its
#: own constant, version and snapshot, and the H3 render guard cannot
#: hold a fragment nested inside another enrolled template. One enrolled
#: body per module is the point.
#:
#: H3/H3a: harness-authored instruction text that reaches the engineer.
#: Round 1 built this block from inline f-strings, so the reviewer
#: changed "binding" to "advisory" and all 61 prompt-version and
#: enrollment tests stayed green. It is enrolled now, and
#: ``tests/test_decisions.py`` renders a known register and compares it
#: to this template formatted in the test, so delivered English cannot
#: live anywhere else.
DECISIONS_CONTEXT_PROMPT = """\
## Architect Decisions

Questions the architect closed while decomposing the spec, and how it
closed them. These are binding: implement what is written here. An
`assumed` decision is pinned by an acceptance criterion in a PRD - if
your code cannot honour one, say so in your Self-Critique rather than
deciding differently.

### Decisions binding this component ({component_id})

{own}

### Decisions binding the whole run

{run_wide}

### Decisions binding other components ({other_shown} of {other_total} shown; \
any not shown did not fit the context budget, and none of them binds this component)

{other}
"""


@dataclass(frozen=True)
class SpecDecision:
    """One question the architect closed, and how.

    ``issue`` is the ``spec_issues`` id this decision closes. It is the
    join key, so it is not optional and not defaulted: a decision that
    closes nothing cannot be checked against anything.
    """

    question: str
    disposition: str
    resolution: str
    issue: str
    reason: str = ""
    alternative: str = ""
    component: str = ""


@dataclass(frozen=True)
class DecisionRegister:
    """The persisted register plus the identity it was written under.

    ``project`` and ``spec_file`` are carried back out rather than
    discarded, because the factory reads one fixed path and has to prove
    the file belongs to the manifest it is about to schedule. Round 1
    dropped both, and a register belonging to project A bound project
    B's engineer.
    """

    decisions: tuple[SpecDecision, ...] = ()
    project: str = ""
    spec_file: str = ""
    halted: bool = False
    status: str = REGISTER_MISSING
    detail: str = ""


class DecisionRegisterError(RuntimeError):
    """The register on disk cannot be trusted to bind this run (#260 r2).

    Raised rather than warned. The register carries instructions the
    engineer prompt calls binding, so "carry on without them" is a
    silent degradation of the exact mechanism this change exists to add,
    and "carry on with somebody else's" is worse: a factory run on
    project B, with project A's register beside it, handed the engineer
    project A's binding instruction.
    """


def bind_register(
    register: DecisionRegister,
    project_name: str,
    spec_file: str,
) -> tuple[SpecDecision, ...]:
    """The decisions that may bind this manifest, or raise.

    Two things are legal and return nothing.

    A MISSING register: a manifest written before this feature has none,
    and that is a fact rather than a fault.

    An empty ``spec_file``: that manifest did not come from a decompose.
    ``Manifest.from_prd`` sets it to ``""``, so it is what ``ks run``
    hands the factory, and the decompose path always sets
    ``spec_path.name``. Round 3 caught this by measurement: without the
    check, the register left by one successful ``ks factory`` refused
    every later ``ks run`` in the same project, and the message told the
    operator to "re-run the decompose for this spec" when ``ks run`` has
    no spec to decompose. An architect's answers about spec.md do not
    bind a run that is not building spec.md, so no spec means no
    binding, quietly.

    Everything else is a refusal - unreadable, halted, or belonging to
    another project or another spec.
    """
    if register.status == REGISTER_MISSING or not spec_file:
        return ()
    if register.status != REGISTER_OK:
        raise DecisionRegisterError(
            f"architect decision register is unreadable: {register.detail}. "
            f"Re-run the decompose, or delete "
            f"{SPEC_DECISIONS_REL_PATH} to run without one."
        )
    if register.halted:
        raise DecisionRegisterError(
            "architect decision register was written by a HALTED decompose, "
            "so no manifest was saved for it. Answer the escalated "
            "question and re-run the decompose."
        )
    if register.project != project_name or register.spec_file != spec_file:
        raise DecisionRegisterError(
            f"architect decision register belongs to project "
            f"{register.project!r} / spec {register.spec_file!r}, but this "
            f"run is {project_name!r} / {spec_file!r}. Re-run the decompose "
            f"for this spec."
        )
    return register.decisions


def _clean(value: Any) -> str:
    """A raw JSON field as a stripped string, or "" for anything else."""
    return value.strip() if isinstance(value, str) else ""


def required_field_error(prefix: str, name: str, value: Any) -> str | None:
    """One required string field, or the reason it is not one.

    Shared with ``decompose._spec_issue_errors``: the two raw validators
    that guard the #260 join must word an identical fault identically,
    or a retry sees two vocabularies for one mistake.
    """
    if not isinstance(value, str):
        return f"{prefix}.{name}: must be a string, got {type(value).__name__}"
    if not value.strip():
        return f"{prefix}.{name}: must not be empty"
    return None


def enum_field_error(
    prefix: str,
    name: str,
    value: Any,
    valid: Collection[str],
) -> str | None:
    """One required field constrained to a fixed vocabulary.

    The message names the whole vocabulary and says the match is exact,
    because "is not one of" without the list sends a retry guessing. F1
    was a capitalised ``disposition``; the round-2 /simplify pass found
    the same hole one field over, in a spec issue's ``severity``, so
    this is the one place both are now checked.
    """
    if not isinstance(value, str):
        return f"{prefix}.{name}: must be a string, got {type(value).__name__}"
    if value not in valid:
        return f"{prefix}.{name}: {value!r} is not one of {sorted(valid)} (match is case-exact)"
    return None


def decision_entry_errors(index: int, entry: Any) -> list[str]:
    """Everything wrong with ONE raw decisions entry, indexed.

    Raw, before any parsing. This is the whole of F1's fix: round 1
    parsed first and counted afterwards, so an entry the parser could
    not read became an absence rather than a fault, and an absence
    agrees with any count. The message is indexed like every other
    validator message so the retry can fix the exact record instead of
    re-deriving which one.
    """
    prefix = f"decisions[{index}]"
    if not isinstance(entry, dict):
        return [f"{prefix}: must be an object, got {type(entry).__name__}"]
    errors: list[str] = []
    for name in ("question", "resolution", "issue"):
        error = required_field_error(prefix, name, entry.get(name))
        if error is not None:
            errors.append(error)
    error = enum_field_error(prefix, "disposition", entry.get("disposition"), VALID_DISPOSITIONS)
    if error is not None:
        errors.append(error)
    for name in ("reason", "alternative", "component"):
        value = entry.get(name)
        if value is not None and not isinstance(value, str):
            errors.append(f"{prefix}.{name}: must be a string, got {type(value).__name__}")
    return errors


def decisions_payload_errors(data: Any) -> list[str]:
    """Everything wrong with the whole raw ``decisions`` array.

    The array is REQUIRED. Round 1 read it with ``data.get("decisions",
    [])``, so a payload that omitted it entirely passed with zero
    decisions and zero escalations.
    """
    if not isinstance(data, dict):
        return ["output must be a JSON object"]
    if "decisions" not in data:
        return ["'decisions' is required (use [] when the spec raised no question)"]
    raw = data["decisions"]
    if not isinstance(raw, list):
        return [f"'decisions' must be an array, got {type(raw).__name__}"]
    errors: list[str] = []
    for index, entry in enumerate(raw):
        errors.extend(decision_entry_errors(index, entry))
    return errors


def parse_decisions(data: Any) -> list[SpecDecision]:
    """Extract typed decisions from raw architect output.

    Call ``decisions_payload_errors`` FIRST and reject on any error.
    This function assumes that has happened: it skips an entry it cannot
    read, and a skip is only safe once a skip can no longer be the
    difference between halting and not.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("decisions", [])
    if not isinstance(raw, list):
        return []
    decisions: list[SpecDecision] = []
    for entry in raw:
        # Non-dict entries included: ``decision_entry_errors`` rejects
        # them by itself, so a separate isinstance guard here would be
        # a second, silently divergent definition of a usable entry.
        if decision_entry_errors(0, entry):
            continue
        decisions.append(
            SpecDecision(
                question=_clean(entry.get("question")),
                disposition=_clean(entry.get("disposition")),
                resolution=_clean(entry.get("resolution")),
                issue=_clean(entry.get("issue")),
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
    """One decision as the seven JSON keys every artifact writes it under."""
    return {
        "issue": decision.issue,
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
    *,
    halted: bool,
) -> Path:
    """Persist the register to ``scripts/kstrl/decisions.json``.

    Written on every decompose that produced parseable output, including
    one that closed nothing: an empty ``decisions`` array is the record
    that the architect had no open question, which is a different fact
    from "no record". ``halted`` is stamped in because a halted run
    saves no manifest, so its register would otherwise sit beside an
    OLDER manifest and read as that run's decisions. Raises ``OSError``
    on write failure so the caller can surface it loudly.
    """
    path = root_dir / SPEC_DECISIONS_REL_PATH
    payload: dict[str, Any] = {
        "project": project_name,
        "specFile": spec_file,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "halted": halted,
        "counts": _decision_counts(decisions),
        "decisions": [_decision_dict(d) for d in decisions],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return path


def read_decisions(root_dir: Path) -> DecisionRegister:
    """Read the register back, reporting WHY when it cannot be used.

    Three outcomes, never one silent empty list. A missing file is a
    manifest older than this feature. An unreadable or malformed one is
    the mechanism this module exists to add, gone; the caller is
    expected to stop rather than schedule engineers with no decisions
    and no warning.

    ``ValueError`` is caught alongside ``OSError`` deliberately: the
    file is JSON read as utf-8, and both ``json.JSONDecodeError`` and
    ``UnicodeDecodeError`` are ``ValueError`` subclasses that would
    otherwise escape a fail-closed ``except OSError``.
    """
    path = root_dir / SPEC_DECISIONS_REL_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DecisionRegister(status=REGISTER_MISSING, detail=f"no register at {path}")
    except (OSError, ValueError) as exc:
        return DecisionRegister(status=REGISTER_UNREADABLE, detail=f"{path}: {exc}")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return DecisionRegister(status=REGISTER_UNREADABLE, detail=f"{path}: {exc}")
    # No separate non-dict branch: ``decisions_payload_errors`` already
    # answers "output must be a JSON object" for anything that is not a
    # dict, and a second wording for one fault is a second thing to keep
    # in step.
    entry_errors = decisions_payload_errors(raw)
    if entry_errors:
        return DecisionRegister(
            status=REGISTER_UNREADABLE, detail=f"{path}: {'; '.join(entry_errors[:3])}"
        )
    return DecisionRegister(
        decisions=tuple(parse_decisions(raw)),
        project=_clean(raw.get("project")),
        spec_file=_clean(raw.get("specFile")),
        halted=bool(raw.get("halted", False)),
        status=REGISTER_OK,
    )


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


def _pack_other(items: Sequence[SpecDecision], max_tokens: int) -> list[SpecDecision]:
    """As many other-component decisions as the budget allows.

    An item too large for the remaining budget is skipped rather than
    ending the tier, because a later, smaller one may still fit - the
    same choice ``knowledge._pack_facts_full`` makes. This is the ONLY
    place a decision is ever dropped.

    Budgeted in CHARACTERS, at the harness-wide 4-per-token convention,
    and including the newline that joins each item. Round 1 summed a
    per-item ``max(1, len // 4)``, and flooring each item independently
    let the tier finish over its own cap: measured, 2,001 tokens against
    a 2,000 cap. Accumulating the real rendered length cannot round in
    the permissive direction.
    """
    budget_chars = max_tokens * _CHARS_PER_TOKEN
    used = 0
    kept: list[SpecDecision] = []
    for item in items:
        cost = len(_render_summary(item)) + 1
        if used + cost > budget_chars:
            continue
        used += cost
        kept.append(item)
    return kept


def build_decisions_context(
    decisions: Sequence[SpecDecision],
    component_id: str,
    max_other_tokens: int = MAX_OTHER_DECISION_TOKENS,
) -> str:
    """The engineer-facing block for one component, or "" when empty.

    Three tiers, and only the third can lose anything:

    1. Decisions naming ``component_id``, in full, ALL of them.
    2. Decisions naming no component, in full, ALL of them: the prompt
       defines an empty ``component`` as "binds the whole run", so these
       bind this engineer too.
    3. Decisions naming another component, one line each, up to
       ``max_other_tokens``.

    That makes the sentence in ``DECISIONS_CONTEXT_PROMPT`` true by
    construction rather than by hope: nothing binding this component is
    ever cut, so the block has no cap and does not pretend to have one.
    """
    if not decisions:
        return ""
    # Escalations first within each tier: if a register somehow reaches
    # an engineer with one in it, it is the line that must lead. Sorted
    # once - Python's sort is stable, so the partitions below inherit
    # the order.
    rank = {name: i for i, name in enumerate(DISPOSITION_ORDER)}
    ordered = sorted(decisions, key=lambda d: rank.get(d.disposition, len(rank)))
    own = [d for d in ordered if d.component == component_id]
    run_wide = [d for d in ordered if not d.component]
    other = [d for d in ordered if d.component and d.component != component_id]
    shown_other = _pack_other(other, max_other_tokens)
    return DECISIONS_CONTEXT_PROMPT.format(
        component_id=component_id,
        own="\n".join(_render_full(d) for d in own),
        run_wide="\n".join(_render_full(d) for d in run_wide),
        other_shown=len(shown_other),
        other_total=len(other),
        other="\n".join(_render_summary(d) for d in shown_other),
    )
