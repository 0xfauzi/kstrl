"""LLM-driven spec decomposition into components and PRDs."""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.agent_output import (
    MAX_AGENT_OUTPUT_BYTES,
    _extract_agent_json,
    timed_out_message,
)
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    Agent,
    collect_usage,
    print_usage_rollup,
    timed_out_since,
    usage_cursor,
)
from kstrl.agents.prompt_record import AgentCall, recording_prompts
from kstrl.atomicio import atomic_write_json
from kstrl.config import KstrlConfig, relative_to_root
from kstrl.decisions import (
    DISPOSITION_ESCALATED,
    SpecDecision,
    decisions_payload_errors,
    enum_field_error,
    escalations,
    open_escalation_item,
    parse_decisions,
    required_field_error,
    resolve_escalation_items,
    write_decisions,
)
from kstrl.decompose_prompt import (
    SPECKIT_ARTIFACTS,
    build_decompose_prompt,
    load_spec_input,
    spec_digest,
    spec_ref,
)
from kstrl.event_catalog import (
    ArtifactWritten,
    ComponentCompleted,
    ComponentFailed,
    ComponentStarted,
    ComponentUsage,
    Event,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunPlan,
    RunStarted,
    SpecIssueRecorded,
)
from kstrl.events import EventBus
from kstrl.guards import ScopeHazard, scope_entry_hazard
from kstrl.linear import (
    LinearClient,
    LinearConfig,
    linear_branch_name,
    sync_decompose,
)
from kstrl.manifest import (
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.names import validate_branch_name, validate_component_id
from kstrl.owner_answers import OwnerAnswers, read_owner_answers
from kstrl.prd import PRD
from kstrl.requirements import parse_requirements, requirements_payload_errors
from kstrl.runid import mint_run_id
from kstrl.spec_convergence import (
    _build_convergence,
    _excluded_history,
    _journal_snapshot,
    _surface_convergence,
)
from kstrl.spec_issues import (
    _VALID_KINDS,
    _VALID_SEVERITIES,
    SPEC_ISSUE_APPLIES_COMPONENT,
    _parse_spec_issues,
    _record_spec_issues_event,
    _spec_audit_journal,
    _surface_spec_decisions,
    _surface_spec_issues,
    persist_spec_issues,
    route_spec_issues,
)
from kstrl.stack import (
    file_stack_item,
    stack_errors,
    stack_from_table,
    stack_text_digest,
    stack_toml,
)
from kstrl.statedir import plan_prd_path

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kstrl.factory_lock import _RunLock
    from kstrl.ui.base import UI


class SpecBlockerError(Exception):
    """Raised when the architect ESCALATED at least one question (#260).

    The decompose pipeline halts until the owner answers, rather than
    letting the architect guess at a product, scope or risk judgement.
    It no longer halts on blocker-severity findings as such: five real
    runs raised 26 blockers and not one of them was a judgement only the
    owner could make, so the architect halted on questions it had
    already answered in its own suggestions.

    The escalated decisions are attached via ``escalations``.
    ``artifact_path`` points at the persisted spec-issues.json (R1.7)
    and ``decisions_path`` at decisions.json, each when its write
    succeeded, so callers can direct the user at a durable record
    instead of scrollback. Both, because after #260 they hold different
    halves of the halt: the finding is in the audit, the question the
    owner has to answer and the reason it was not answered are in the
    register.
    """

    def __init__(
        self,
        escalated: list[SpecDecision],
        artifact_path: Path | None = None,
        decisions_path: Path | None = None,
    ):
        self.escalations = escalated
        self.artifact_path = artifact_path
        self.decisions_path = decisions_path
        summary_lines = [f"- {d.question}\n  owner must decide: {d.resolution}" for d in escalated]
        super().__init__(
            "Architect escalated; the owner must answer before re-running:\n"
            + "\n".join(summary_lines),
        )

    def artifact_lines(self) -> list[str]:
        """Where the operator should go to answer, one line per record.

        R1.7 says point at a file rather than at scrollback, and every
        handler of this exception owes the same pointers, so the wording
        lives here instead of being repeated at each of them.
        """
        lines: list[str] = []
        if self.artifact_path is not None:
            lines.append(f"Spec issues written to: {self.artifact_path}")
        if self.decisions_path is not None:
            lines.append(f"Architect decisions written to: {self.decisions_path}")
        return lines


# R1.5 / H-4: DECOMPOSE_PROMPT rule #12 promises the harness rejects
# allowedPaths entries that would reopen its own guardrails. This is
# that enforcement -- exactly the prompt's EXCLUDE list. Entries are
# compared after normalization (leading `./` and trailing `/` removed)
# so `.kstrl`, `.kstrl/` and `./.kstrl/` all match. Keep this set in
# sync with the prompt body (which only Session 8C may edit).
#
# #696 slice 7: kstrl.toml is on the list, because it holds the project's
# [stack], which only a person confirms; the architect proposes a stack in
# its output instead. A build manifest is not on it any more: kstrl names
# no language, and a component may create the manifest its project needs.
# The comparison is an equality test, so only the exact entry is refused.
_ALLOWED_PATHS_EXCLUDE: frozenset[str] = frozenset(
    {
        ".kstrl",  # harness runtime state
        ".github",  # CI configuration
        "kstrl",  # harness package
        "kstrl.toml",  # kstrl's configuration and the confirmed [stack]
        "scripts/kstrl",  # bare prefix exposes the manifest + sibling features
    }
)


# Rule #12's structural hazards, addressed to the ARCHITECT inside the
# decompose retry loop. Keyed by guards.ScopeHazard so adding a hazard
# there is a type error here rather than a silently unhandled case;
# tests/test_harness_path_scope.py asserts the keys stay in step.
_SCOPE_HAZARD_ADVICE: dict[ScopeHazard, str] = {
    "root": ("grants whole-repo scope; list specific source/test/feature path prefixes instead"),
    "absolute": "is an absolute path; entries must be repo-relative prefixes",
    "traversal": "contains '..'; path traversal outside the worktree is not allowed",
    "whitespace": (
        "has leading or trailing whitespace; scope matching is exact, so it would authorise nothing"
    ),
}


def _validate_allowed_path_entry(entry: str) -> str | None:
    """Return an error message if an allowedPaths entry is unacceptable.

    Enforces the DECOMPOSE_PROMPT rule #12 EXCLUDE list plus every
    hazard ``guards.scope_entry_hazard`` classifies, each with its
    sentence in ``_SCOPE_HAZARD_ADVICE``. The predicate
    is shared with ``factory._preflight_component_scope`` so a hazard
    added for one input path is caught for the other; only the wording
    forks, because these errors feed the decompose retry-with-error loop
    and address the architect directly.
    """
    stripped = entry.strip()
    # The RAW entry, not the stripped one: path_is_allowed matches raw,
    # so " src/" authorises nothing and must be rejected here too.
    hazard = scope_entry_hazard(entry)
    if hazard is not None:
        return f"entry '{entry}' {_SCOPE_HAZARD_ADVICE[hazard]}"
    normalized = stripped
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if normalized in _ALLOWED_PATHS_EXCLUDE:
        return (
            f"entry '{entry}' is on the DECOMPOSE_PROMPT EXCLUDE list "
            "(harness state, CI config, kstrl.toml, and "
            "the harness's own packages are never in scope; for "
            "scripts/kstrl list only this component's own "
            "scripts/kstrl/feature/<id>/ subtree)"
        )
    return None


def _spec_issue_errors(data: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Every raw spec issue, validated for the #260 join.

    Returns the errors and, when there are none, ``{id: severity}`` for
    the join below. Validated RAW against the SAME vocabularies
    ``_parse_spec_issues`` uses, because the two must agree about which
    entries exist: the round-2 /simplify pass measured a severity of
    ``"Blocker"`` validating, being closed by a ``decided`` decision,
    and then being dropped by the parser, so the issue reached neither
    the halt gate nor ``spec-issues.json`` nor the UI. That is F1's
    capital letter one field over.
    """
    raw = data.get("spec_issues")
    if raw is None:
        return [], {}
    if not isinstance(raw, list):
        return [f"'spec_issues' must be an array, got {type(raw).__name__}"], {}
    errors: list[str] = []
    severities: dict[str, str] = {}
    for index, entry in enumerate(raw):
        prefix = f"spec_issues[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object, got {type(entry).__name__}")
            continue
        issue_id = entry.get("id")
        if not isinstance(issue_id, str) or not issue_id.strip():
            errors.append(f"{prefix}.id: must be a non-empty string, unique in this payload")
            continue
        issue_id = issue_id.strip()
        if issue_id in severities:
            errors.append(f"{prefix}.id: {issue_id!r} is already used by an earlier issue")
            continue
        entry_errors = [
            error
            for error in (
                enum_field_error(prefix, "severity", entry.get("severity"), _VALID_SEVERITIES),
                enum_field_error(prefix, "kind", entry.get("kind"), _VALID_KINDS),
                required_field_error(prefix, "summary", entry.get("summary")),
            )
            if error is not None
        ]
        if entry_errors:
            errors.extend(entry_errors)
            continue
        severities[issue_id] = str(entry["severity"])
    return errors, severities


def _decision_join_errors(
    raw_decisions: list[Any],
    issue_severity: dict[str, str],
) -> list[str]:
    """Every issue closed once, and blocker iff escalated (#260 r2).

    This replaces a count comparison. Counting could not tell two
    blockers plus two unrelated escalations from two matched pairs, and
    worse, it read zero against zero as agreement, so any entry the
    parser dropped disabled the halt gate. The join is per record and
    indexed, so a retry can fix the exact entry.
    """
    errors: list[str] = []
    closed_by: dict[str, int] = {}
    for index, entry in enumerate(raw_decisions):
        # No shape guards: the caller returns on any payload error, so
        # every entry here is already a dict with a non-empty string
        # 'issue' and a valid 'disposition'. A `continue` inside the
        # gate that replaced the count comparison would be a silent
        # skip, which is exactly the round-1 defect.
        issue_id = str(entry["issue"]).strip()
        disposition = str(entry["disposition"])
        prefix = f"decisions[{index}]"
        if issue_id not in issue_severity:
            errors.append(
                f"{prefix}.issue: {issue_id!r} is not the id of any entry in 'spec_issues'"
            )
            continue
        if issue_id in closed_by:
            errors.append(
                f"{prefix}.issue: {issue_id!r} is already closed by decisions[{closed_by[issue_id]}]"
            )
            continue
        closed_by[issue_id] = index
        is_blocker = issue_severity[issue_id] == "blocker"
        is_escalated = disposition == DISPOSITION_ESCALATED
        if is_blocker != is_escalated:
            errors.append(
                f"{prefix}: issue {issue_id!r} is severity "
                f"{issue_severity[issue_id]!r} and disposition {disposition!r}. "
                f"'blocker' means 'you escalated this': a blocker needs an "
                f"'escalated' decision and an 'escalated' decision needs a "
                f"'blocker' issue."
            )
    errors.extend(
        f"spec_issues: {issue_id!r} was raised and never closed; every issue "
        f"needs one 'decisions' entry naming it"
        for issue_id in issue_severity
        if issue_id not in closed_by
    )
    return errors


def _decision_register_errors(data: dict[str, Any]) -> list[str]:
    """The register contract, validated on the RAW payload (#260 r2).

    Order matters: the array and its entries first, then the identity
    join. A malformed entry is a fault with an index, never an absence,
    because round 1 turned every malformed entry into an absence and two
    absences agree. Measured on the round-1 code: nine malformed shapes
    were ACCEPTED, including a decision whose disposition was
    ``"Escalated"``.
    """
    errors = decisions_payload_errors(data)
    id_errors, issue_severity = _spec_issue_errors(data)
    errors.extend(id_errors)
    if errors:
        return errors
    return _decision_join_errors(data["decisions"], issue_severity)


#: How many validator messages the retry prompt carries. Round 2
#: replaced one aggregate message with one message per bad record, and
#: the round-2 /simplify pass measured the result: an architect that
#: got every field's type wrong on 32 decisions produced 224 messages,
#: 11,480 characters, against round 1's single 278-character line for
#: the same fault. That is pasted into a prompt that already carries
#: the whole spec, up to ``max_retries`` times. Twenty indexed messages
#: are enough for a retry to see the shape of what it got wrong; the
#: count tells it how much more there is.
_MAX_RETRY_MESSAGES = 20


def _retry_feedback(errors: list[str]) -> str:
    """Validator messages for the retry prompt, bounded."""
    shown = "; ".join(errors[:_MAX_RETRY_MESSAGES])
    extra = len(errors) - _MAX_RETRY_MESSAGES
    if extra <= 0:
        return shown
    return f"{shown}; ... and {extra} more of the same kind"


def _write_decompose_artifact(
    label: str,
    noun: str,
    writer: Callable[[], Path],
    *,
    ui: UI,
    emit: Callable[[Event], None],
    rel_display: Callable[[Path], str],
    required: bool = False,
) -> Path | None:
    """Attempt one durable decompose artifact, and say what happened.

    The write policy the R1.7 audit and the #260 decision register
    share, in one place so the two cannot drift: attempt, announce,
    emit, and on ``OSError`` be loud without masking. Returns the path,
    or ``None`` when the write failed, which is what the caller passes
    to ``SpecBlockerError`` so the halt never points at a file that is
    not there.

    ``required`` says the artifact IS part of the result rather than a
    record of it. The decision register beside a saved manifest is: a
    later ``ks factory`` reads it to bind engineers, and a missing
    register binds nothing and says nothing, so one swallowed write
    error would silently disable the register for every run against
    that manifest. The halting copy is not required, because the halt
    reaches the operator through ``SpecBlockerError`` either way.
    """
    try:
        path = writer()
    except OSError as exc:
        if required:
            raise
        ui.err(f"Failed to persist {noun} to disk: {exc}")
        return None
    ui.ok(f"{noun.capitalize()} written: {path}")
    emit(ArtifactWritten(label=label, path=rel_display(path)))
    return path


def _decision_component_errors(
    decisions: list[SpecDecision],
    known_ids: set[str],
) -> list[str]:
    """Every decision must name a component that exists, or none (#260).

    The same join the validator already does for `dependencies`, applied
    to the register. A decision naming a component that is not in the
    payload is not harmless: the engineer-facing renderer matches the id
    exactly, so a typo or a stale id silently demotes a binding decision
    to the one-line summary tier for every engineer, stripping its
    reason and the alternative it rejected. An empty component is legal
    and means "binds the whole run".
    """
    return [
        f"decisions: '{d.question[:60]}' names unknown component "
        f"'{d.component}'; use a component id from this payload, or "
        f"leave it empty for a decision that binds the whole run"
        for d in decisions
        if d.component and d.component not in known_ids
    ]


def _empty_components_errors(data: dict[str, Any], errors: list[str]) -> list[str]:
    """Whether an empty ``components`` array is a legal halt (#260).

    Legal only when the register is clean AND carries an escalation.
    ``errors`` is required to be empty first: round 1 accepted an empty
    payload whose only escalation was malformed, because the malformed
    entry parsed to nothing and nothing is not an escalation, so the
    caller fell through to the register errors alone and the halt read
    as a retryable shape rather than as the fail-open it was.
    """
    if not errors and escalations(parse_decisions(data)):
        return errors
    return [
        *errors,
        "'components' must not be empty (no well-formed escalated decision to justify a halt)",
    ]


def _stack_output_errors(data: dict[str, Any], *, has_stack: bool) -> list[str]:
    """Why the architect's ``stack`` is not a usable proposal, or [] (#696 slice 7).

    null keeps the [stack] kstrl.toml already holds, and a missing key reads
    as null, so both are refused when kstrl.toml holds none (``has_stack``
    False). An object is checked by ``stack.stack_errors``, the one
    vocabulary of a valid [stack], and every error is named.
    """
    raw = data.get("stack")
    if raw is None:
        if has_stack:
            return []
        return [
            "stack: kstrl.toml has no [stack], so 'stack' must propose one, not be null or missing"
        ]
    if not isinstance(raw, dict):
        return [f"stack: must be an object or null, got {type(raw).__name__}"]
    return [f"stack: {error}" for error in stack_errors(raw)]


def _validate_decompose_output(data: Any, *, has_stack: bool) -> list[str]:
    """Validate the decomposition output structure.

    Empty components is permitted only when the architect escalated -
    it is explicitly halting the pipeline until the owner answers a
    judgement call. Before #260 the same slot keyed on blocker severity,
    which halted five real runs on questions the architect had already
    answered in its own suggestions.

    ``has_stack``: whether kstrl.toml holds a [stack], which decides
    whether the architect may answer ``"stack": null`` (#696 slice 7).
    """
    if not isinstance(data, dict):
        return ["Output must be a JSON object"]

    if "components" not in data:
        return ["Output must have a 'components' key"]

    components = data["components"]
    if not isinstance(components, list):
        return ["'components' must be an array"]

    # The register is checked RAW and first, on both the halting and the
    # decomposing path: an unusable register is a retryable error either
    # way, and after round 2 an entry that does not parse is a named
    # fault rather than a silent zero.
    errors: list[str] = _decision_register_errors(data)
    errors += _stack_output_errors(data, has_stack=has_stack)

    if not components:
        # #639 slice 2: a halt names no story, so the join is skipped.
        errors += requirements_payload_errors(data, None)
        return _empty_components_errors(data, errors)

    # Parsed only once the raw shape is known good, so a parse can no
    # longer be the difference between halting and not.
    decisions = parse_decisions(data)

    seen_ids: set[str] = set()
    seen_story_ids: set[str] = set()

    for i, comp in enumerate(components):
        prefix = f"components[{i}]"

        if not isinstance(comp, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        comp_id = comp.get("id")
        if not isinstance(comp_id, str):
            errors.append(f"{prefix}.id: must be a non-empty string")
        else:
            # R0.6: the id becomes a filesystem path segment
            # (scripts/kstrl/feature/<id>/, .kstrl/worktrees/<id>) and a
            # branch segment (kstrl/factory/<id>), so a traversal id
            # like "../../repo" must be rejected here, where the error
            # feeds back into the decompose retry loop.
            id_error = validate_component_id(comp_id)
            if id_error:
                errors.append(f"{prefix}.id: {id_error}")
            elif comp_id in seen_ids:
                errors.append(f"{prefix}.id: duplicate ID '{comp_id}'")
            else:
                seen_ids.add(comp_id)

        if not isinstance(comp.get("title"), str):
            errors.append(f"{prefix}.title: must be a string")

        if not isinstance(comp.get("description"), str):
            errors.append(f"{prefix}.description: must be a string")

        deps = comp.get("dependencies")
        if not isinstance(deps, list):
            errors.append(f"{prefix}.dependencies: must be an array")
        elif not all(isinstance(d, str) for d in deps):
            errors.append(f"{prefix}.dependencies: all items must be strings")

        # allowedPaths is REQUIRED in architect output. The
        # diff-scope check at Phase 1 is silently disabled when
        # allowed_paths is None, so an architect that forgets to
        # emit this field would bypass the guardrail entirely. This
        # is a v1.2.0 prompt contract: DECOMPOSE_PROMPT rule #12
        # spells it out, and the validator gates it here. Legacy
        # v1.0.0/v1.1.0-from-disk PRDs still load (see PRD.load
        # which keeps the field optional for backward compat with
        # hand-edited PRDs) -- this gate only fires on FRESH
        # architect emissions inside decompose_spec.
        if "allowedPaths" not in comp:
            errors.append(
                f"{prefix}.allowedPaths: required field missing. "
                "The architect must declare a per-component write "
                "scope; see DECOMPOSE_PROMPT rule #12. To halt on "
                "vague layout instead, return an empty `components` "
                "array with a `spec_issues` entry."
            )
        else:
            ap = comp["allowedPaths"]
            if not isinstance(ap, list):
                errors.append(f"{prefix}.allowedPaths: must be an array")
            elif not ap:
                errors.append(
                    f"{prefix}.allowedPaths: must be non-empty -- an empty "
                    "array silently disables diff-scope enforcement"
                )
            elif not all(isinstance(p, str) and p for p in ap):
                errors.append(f"{prefix}.allowedPaths: all items must be non-empty strings")
            else:
                # R1.5 / H-4: content validation. Without this, only
                # the SHAPE was checked and the architect could emit
                # `.kstrl/` or `kstrl/`, reopening the guardrail
                # the prompt claims the harness enforces.
                for p in ap:
                    entry_error = _validate_allowed_path_entry(p)
                    if entry_error:
                        errors.append(f"{prefix}.allowedPaths: {entry_error}")

        stories = comp.get("userStories")
        if not isinstance(stories, list):
            errors.append(f"{prefix}.userStories: must be an array")
            continue

        if not stories:
            # R1.8: a component with zero stories has nothing for the
            # engineer to implement and nothing for the reviewer to
            # fail against - it auto-passes downstream. Vacuous, not
            # minimal; reject with a retryable message.
            errors.append(
                f"{prefix}.userStories: must not be empty -- every "
                "component needs at least one user story"
            )

        for j, story in enumerate(stories):
            sp = f"{prefix}.userStories[{j}]"
            if not isinstance(story, dict):
                errors.append(f"{sp}: must be an object")
                continue

            story_id = story.get("id")
            if isinstance(story_id, str) and story_id:
                if story_id in seen_story_ids:
                    errors.append(f"{sp}.id: duplicate story ID '{story_id}'")
                seen_story_ids.add(story_id)

            # R1.8 vacuous-PRD gates. Type errors (non-list criteria,
            # non-bool passes) are caught by the PRD schema validation
            # stage of the retry loop; these two checks reject shapes
            # that are type-valid but semantically empty.
            criteria = story.get("acceptanceCriteria")
            if isinstance(criteria, list) and not criteria:
                errors.append(
                    f"{sp}.acceptanceCriteria: must not be empty -- a "
                    "story with no criteria is vacuously satisfiable"
                )
            if story.get("passes") is True:
                errors.append(
                    f"{sp}.passes: must be false -- stories start "
                    "unimplemented; passes:true would skip the story "
                    "and auto-pass review"
                )

    # Check dependency references
    for comp in components:
        if not isinstance(comp, dict):
            continue
        comp_id = comp.get("id", "?")
        for dep in comp.get("dependencies", []):
            if isinstance(dep, str) and dep not in seen_ids:
                errors.append(f"Component '{comp_id}' depends on unknown component '{dep}'")

    errors.extend(_decision_component_errors(decisions, seen_ids))
    # #639 slice 2: stories join requirements by id, after the loop above
    # has collected every story id.
    errors.extend(requirements_payload_errors(data, seen_story_ids))

    return errors


def _component_branch(comp_id: str, project_name: str, single_pr: bool) -> str:
    """Branch a component's PRD will target."""
    if single_pr:
        return f"kstrl/factory/{project_name}"
    return f"kstrl/factory/{comp_id}"


def _routed_prd_issues(data: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """The audit findings each component's PRD carries (#260).

    Derived from the decompose payload rather than passed around, so
    the retry-loop validation stage and the write phase can both call
    it and R1.8's "what is validated is what is written" holds for this
    field too. Pure, so calling it twice on one payload is free.

    Blockers are filtered out here: they halt before any PRD exists.
    """
    non_blocking = [i for i in _parse_spec_issues(data) if i.severity != "blocker"]
    return route_spec_issues(non_blocking, data["components"])


def _prd_schema_errors(
    data: dict[str, Any],
    project_name: str,
    single_pr: bool,
) -> list[str]:
    """PRD schema errors across every component of one decompose payload.

    R1.8: this runs INSIDE the retry loop so a malformed story is a
    retryable error the LLM gets to fix, not a post-loop crash. Nothing
    is written to disk until every component's PRD payload validates.
    """
    routed_issues = _routed_prd_issues(data)
    errors: list[str] = []
    for comp_data in data["components"]:
        branch = _component_branch(comp_data["id"], project_name, single_pr)
        schema_errors = PRD.validate_schema(
            _build_prd_data(comp_data, branch, routed_issues[comp_data["id"]])
        )
        if schema_errors:
            errors.append(f"component '{comp_data['id']}' PRD schema: " + "; ".join(schema_errors))
    return errors


def _prd_summary_line(
    comp_id: str,
    story_count: int,
    findings: Sequence[dict[str, str]],
) -> str:
    """The one line the operator sees per generated PRD.

    "The audit reached the engineer" should be visible where the
    operator is already looking rather than only inside the PRD, so the
    findings ride along with the story count instead of getting a
    section of their own.
    """
    line = f"  {comp_id}: {story_count} stories"
    if not findings:
        return line
    own = sum(1 for e in findings if e["appliesTo"] == SPEC_ISSUE_APPLIES_COMPONENT)
    return f"{line}, {len(findings)} spec findings ({own} on its own surface)"


def _build_prd_data(
    comp_data: dict[str, Any],
    branch_name: str,
    spec_issues: Sequence[dict[str, str]],
) -> dict[str, Any]:
    """Assemble the PRD payload for one component.

    Shared by the retry-loop validation stage and the write phase so
    what gets validated is byte-for-byte what gets written (R1.8).
    """
    prd_data: dict[str, Any] = {
        "branchName": branch_name,
        "userStories": comp_data["userStories"],
    }
    # #260: the architect's non-blocker findings on this component's
    # surface, plus the ones it could not place. Written here because
    # the engineer's first instruction is to read this file, so the PRD
    # is the mechanism that already reaches it - no new channel, and no
    # prompt change. Omitted entirely when there is nothing to say, so
    # a clean audit leaves the PRD byte-identical to before.
    if spec_issues:
        prd_data["specIssues"] = list(spec_issues)
    # allowedPaths is emitted by the architect (DECOMPOSE_PROMPT v1.1.0+)
    # and forwarded into the PRD verbatim. The factory then passes them
    # through to verify.check_diff_scope so the diff-scope guardrail
    # actually fires per-component. Older architect outputs may omit
    # this field; the PRD parser tolerates absence by treating it as
    # "scope unconstrained" (the previous global behavior).
    if "allowedPaths" in comp_data:
        prd_data["allowedPaths"] = comp_data["allowedPaths"]
    return prd_data


def _generate_component_prd(
    comp_data: dict[str, Any],
    root_dir: Path,
    branch_name: str,
    spec_issues: Sequence[dict[str, str]] = (),
    *,
    plan_id: str,
) -> Path:
    """Generate a standard PRD file for one component.

    Validates BEFORE touching disk (R1.8): the retry loop has already
    validated this payload, so a failure here indicates a harness bug,
    but the guard preserves the write-only-validated invariant. The
    write itself is atomic, so a crash mid-write never leaves a
    truncated prd.json.

    Written at ``statedir.plan_prd_path``, not at the component's
    ``prd_path``: the component branch commits the engineer's copy at
    ``prd_path``, so a copy there in the root checkout blocks the merge
    of that branch (#545). Under ``plan_id``, the id this decompose
    stamps on every component it puts in the manifest, so a later
    decompose reusing the id writes beside this copy (#568).

    Returns the path to the generated prd.json.
    """
    comp_id: str = comp_data["id"]
    prd_data = _build_prd_data(comp_data, branch_name, spec_issues)

    errors = PRD.validate_schema(prd_data)
    if errors:
        raise ValueError(f"Generated PRD for '{comp_id}' has schema errors: {'; '.join(errors)}")

    prd_path = plan_prd_path(root_dir, comp_id, plan_id=plan_id)
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(prd_path, prd_data)
    return prd_path


def _report_architect_usage(
    agent: Agent,
    ui: UI,
    bus: EventBus | None,
    *,
    since: int,
) -> None:
    """Publish what the architect's LLM calls cost, to the bus and the
    terminal (#257).

    ``since`` is the agent's record count before this decomposition ran.
    ``decompose_spec`` is public and takes an ``Agent``, so a caller can
    hold one across two calls - re-running after editing the spec is the
    obvious way - and would then see run 1 folded into run 2's event AND
    its table. Every in-tree caller passes a fresh agent; the offset
    removes the dependency rather than documenting an invariant a caller
    cannot see (#257 review).

    The component id is the pseudo-component ``_decompose_spec_impl``
    already announced in the plan, so the event lands on an existing row
    rather than conjuring one. The PHASE key is the bare role name
    because a meter's phase key is the ROLE - it is what the coverage
    footer prints - and the taxonomy's fifth role is the architect. It
    is deliberately not ``decompose``/``audit``, the lifecycle phases
    this component reports elsewhere, which name steps rather than roles.

    The two keys are no longer the same string (#281): the phase axis is
    kstrl's vocabulary on both sides, but the component axis is one the
    architect LLM also writes to, so ``ARCHITECT_COMPONENT`` carries the
    role namespace and ``ARCHITECT_ROLE`` stays bare.

    Called from a ``finally``, so it must not raise: ``collect_usage``
    already swallows a malformed record set, and zero calls (an agent
    predating R3.1, or one that never ran) reports nothing at all.
    """
    totals = collect_usage(agent, since=since)
    if totals.calls == 0:
        return
    if bus is not None:
        bus.emit(
            ComponentUsage(
                component=ARCHITECT_COMPONENT,
                phase=ARCHITECT_ROLE,
                **totals.to_dict(),
            )
        )
    print_usage_rollup(
        ui,
        {ARCHITECT_COMPONENT: {ARCHITECT_ROLE: totals}},
        totals,
        # NOT "Usage rollup": `ks factory` decomposes and then prints its
        # own run-level rollup, whose total excludes the architect until
        # piece B. Two tables under one heading would invite reading the
        # later one as the run's whole spend.
        title="Architect usage",
    )


def _file_stack_proposal(
    data: dict[str, Any],
    root_dir: Path,
    stack_pin: str,
    spec_source: str,
    bus: EventBus | None,
    ui: UI,
) -> None:
    """File the architect's proposed [stack] for a person to confirm (#696 slice 7).

    Nothing when it answered null, or proposed the very text kstrl.toml holds
    (``stack_pin``). kstrl confirms nothing: the item is a proposal, which
    ``ks inbox approve`` refuses until kstrl.toml holds its exact text. A
    filing that fails is said, with the table, and never raised: the plan
    does not depend on it, and a run under no [stack] refuses on its own.
    """
    if data.get("stack") is None:
        return
    proposed = stack_from_table(data["stack"])
    if proposed.digest == stack_pin:
        return
    try:
        item = file_stack_item(
            root_dir,
            proposed,
            run_id=bus.run_id if bus is not None else "",
            proposal=f"the architect, for the spec {spec_source}",
        )
    except Exception as exc:  # noqa: BLE001 - a filing that failed is said, never raised
        ui.err(f"The architect proposed [stack] {proposed.digest[:12]}; no item was filed: {exc}")
        ui.info(stack_toml(proposed))
        return
    ui.ok(
        f"The architect proposed [stack] {proposed.digest[:12]}: inbox item {item.id[:8]}. "
        f"ks inbox show {item.id[:8]} prints the table to put in kstrl.toml; then "
        f"ks inbox approve {item.id[:8]} confirms it."
    )


def _say_owner_answers(answers: OwnerAnswers, ui: UI) -> None:
    """Name the owner answers appended to the architect's input, if any (#639)."""
    if answers.item_ids:
        ui.kv("Owner answers", ", ".join(item_id[:8] for item_id in answers.item_ids))


def _for_attempt(call: AgentCall, attempt: int) -> AgentCall:
    """The architect's record identity for one decompose attempt (#532)."""
    return dataclasses.replace(call, attempt=attempt)


def _attempt_stopped(
    agent: Agent, cursor: int, *, too_large: bool, timeout: float | None
) -> str | None:
    """Why an architect attempt's reply must not be read, or None.

    A reply over the size cap, or one from a call the adapter killed at
    ``timeout`` (#603), is one failed attempt, and its lines are not parsed.
    """
    if too_large:
        return "agent output exceeded size cap"
    if timed_out_since(agent, cursor):
        return timed_out_message(timeout)
    return None


def _decompose_spec_impl(
    spec_path: Path,
    project_name: str,
    base_branch: str,
    single_pr: bool,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    max_retries: int = 3,
    *,
    bus: EventBus | None = None,
    transcript: Callable[[str], None] | None = None,
    prompt_call: AgentCall,
    force_lock: bool = False,
    run_lock: _RunLock | None = None,
    timeout: float | None,
    before_spend: Callable[[], object],
) -> Manifest:
    """Decompose a spec into components and generate PRDs.

    Args:
        spec_path: Path to the markdown spec file
        project_name: Name for the project/factory run
        base_branch: Base git branch
        single_pr: Whether to use a single branch for all components
        agent: Agent to use for decomposition
        ui: UI for output
        root_dir: Project root directory
        max_retries: Max attempts for JSON parsing
        bus: Optional run event bus (TUI surface C4): the work is
            projected onto the pseudo-component "architect" - attempts
            as decompose phases, the red-team pass as an audit phase,
            spec issues and artifacts as typed events. None = today's
            behavior exactly.
        transcript: Optional sink for the architect's streamed lines
            (the run's transcript file); terminal streaming through
            ``ui`` is unchanged either way.
        prompt_call: Who the architect's prompt is recorded for (#532),
            with each attempt's number put in. Required (#567): every
            caller is inside a run, and the run is what the record names.
        force_lock: Passed to ``_acquire_run_lock`` when this call takes
            its own lock (``run_lock`` is None); ignored otherwise, since
            a caller that already holds one decided ``force`` itself.
        run_lock: A lock the caller already holds (#597), e.g. `ks
            factory --spec`, which takes it before this call so the
            architect's spend and the manifest write share one acquire.
            None (the `ks decompose` command's own case) means this call
            takes and releases its own lock around the manifest write.
        before_spend: Called once, after this run has started and before
            the first architect call (#696 slice 7). `ks factory --spec`
            measures the base in it and exits 2 there on a red one, so
            the refusal and its record belong to this run.

    Returns:
        Manifest with generated components and PRD files
    """

    def emit(event: Event) -> None:
        if bus is not None:
            bus.emit(event)

    def rel_display(path: Path) -> str:
        try:
            return path.relative_to(root_dir).as_posix()
        except ValueError:
            return str(path)

    run_started = time.monotonic()
    emit(RunStarted(project=project_name, components=0, pid=os.getpid()))
    emit(
        RunPlan(
            components=(
                {"id": ARCHITECT_COMPONENT, "title": "Architect / PRD red-team", "deps": []},
            )
        )
    )
    emit(ComponentStarted(component=ARCHITECT_COMPONENT))

    ui.section("Spec Decomposition")
    ui.kv("Spec", str(spec_path))
    if spec_path.is_dir():
        present = [name for name, _ in SPECKIT_ARTIFACTS if (spec_path / name).is_file()]
        ui.kv("SpecKit artifacts", ", ".join(present) or "<none>")
    ui.kv("Project", project_name)

    spec_content = load_spec_input(spec_path)
    # #639: the pin is the text the architect is about to read, never a
    # second read of the file, and spec_source is the path a later run
    # re-reads it from.
    pin = spec_digest(spec_content)
    spec_source = spec_ref(spec_path, root_dir)
    # #696: the [stack] this plan is made under, pinned as text; the run
    # that builds the plan refuses under any other (plan_gate.stack_pin_errors).
    stack_pin = stack_text_digest(root_dir, warn=ui.warn)
    ui.kv("Spec digest", pin[:12])
    # #639 slice 4: the owner's inbox answers to this spec's escalations,
    # appended to the architect's input AFTER the pin, so specDigest stays
    # the digest of the spec alone and the run-time re-read reproduces it.
    # An unreadable inbox raises OwnerAnswerError here, before any spend.
    answers = read_owner_answers(root_dir, project_name, spec_source)
    _say_owner_answers(answers, ui)
    # #199: the architect runs with cwd=root_dir (see `agent.run` below),
    # so it is told to read the repository rather than handed a paste.
    # The map path is the operator's: `[paths] codebase_map` can move it,
    # and a prompt naming the default would name a file that is not
    # there. One read, measured at 0.473 ms against a 23 KB kstrl.toml,
    # before the architect call this repo measures at 119 to 210 s.
    #
    # load_or_anchored, not the strict load: this line runs BEFORE the
    # halt path below that writes scripts/kstrl/spec-issues.json, so a
    # malformed kstrl.toml raising here would cost the audit artifact
    # the halt path exists to produce. EvolutionConfig.load_or_none
    # (kstrl/evolution.py:339) is the precedent for the same file at the
    # same kind of call site. Every other command still gets the strict
    # load through config_preflight at entry; only this one path, ahead
    # of the halt artifact, is tolerant.
    config = KstrlConfig.load_or_anchored(root_dir, warn=ui.warn)
    prompt = build_decompose_prompt(
        project_name,
        spec_content + answers.text,
        codebase_map_path=relative_to_root(config.codebase_map_file, root_dir),
    )

    # #696 slice 7: the caller's last check before the first paid call
    # (`ks factory --spec` measures the base here, in this run).
    before_spend()
    data = None
    last_error: str | None = None
    attempts_used = 0

    for attempt in range(1, max_retries + 1):
        attempts_used = attempt
        ui.info(f"Decomposition attempt {attempt}/{max_retries}")
        emit(
            PhaseStarted(
                component=ARCHITECT_COMPONENT,
                phase="decompose",
                attempt=attempt,
            )
        )
        phase_start = time.monotonic()

        def attempt_failed(detail: str, *, started_at: float) -> None:
            emit(
                PhaseCompleted(
                    component=ARCHITECT_COMPONENT,
                    phase="decompose",
                    passed=False,
                    detail=detail[:200],
                    duration_seconds=round(time.monotonic() - started_at, 2),
                )
            )

        if last_error:
            retry_prompt = (
                f"{prompt}\n\n"
                f"PREVIOUS ATTEMPT FAILED with error:\n{last_error}\n\n"
                f"Please fix the error and output valid JSON."
            )
        else:
            retry_prompt = prompt

        output_lines: list[str] = []
        total_bytes = 0
        too_large = False
        cursor = usage_cursor(agent)
        try:
            with recording_prompts(_for_attempt(prompt_call, attempt)):
                for line in agent.run(retry_prompt, cwd=root_dir, timeout=timeout):
                    output_lines.append(line)
                    ui.stream_line("AI", line)
                    if transcript is not None:
                        transcript(line)
                    total_bytes += len(line) + 1
                    if total_bytes > MAX_AGENT_OUTPUT_BYTES:
                        too_large = True
                        ui.warn(
                            "Decompose agent emitted "
                            f">{MAX_AGENT_OUTPUT_BYTES // 1024 // 1024}MB; "
                            "aborting this attempt."
                        )
                        break
        except BaseException as exc:
            attempt_failed(
                f"{type(exc).__name__}: {exc}",
                started_at=phase_start,
            )
            raise

        stopped = _attempt_stopped(agent, cursor, too_large=too_large, timeout=timeout)
        if stopped is not None:
            last_error = stopped
            attempt_failed(last_error, started_at=phase_start)
            continue

        try:
            data = _extract_agent_json(agent, output_lines)
        except ValueError as exc:
            last_error = str(exc)
            ui.warn(f"JSON extraction failed: {last_error}")
            attempt_failed(last_error, started_at=phase_start)
            continue

        validation_errors = _validate_decompose_output(data, has_stack=bool(stack_pin))
        if validation_errors:
            last_error = _retry_feedback(validation_errors)
            ui.warn(f"Validation failed: {last_error}")
            attempt_failed(last_error, started_at=phase_start)
            data = None
            continue

        prd_errors = _prd_schema_errors(data, project_name, single_pr)
        if prd_errors:
            last_error = _retry_feedback(prd_errors)
            ui.warn(f"PRD validation failed: {last_error}")
            attempt_failed(last_error, started_at=phase_start)
            data = None
            continue

        last_error = None
        emit(
            PhaseCompleted(
                component=ARCHITECT_COMPONENT,
                phase="decompose",
                passed=True,
                duration_seconds=round(time.monotonic() - phase_start, 2),
            )
        )
        break

    if data is None:
        # No files were written in the retry loop, so terminal failure
        # leaves no partial state behind (R1.8).
        raise ValueError(
            f"Failed to decompose spec after {max_retries} attempts. Last error: {last_error}"
        )

    # Surface AND persist red-team findings before doing any further
    # work (R1.7): the artifact and journal event are written for halt,
    # success, and clean-audit outcomes alike. If any issue is a
    # blocker, halt before generating PRDs - the architect explicitly
    # judged the spec un-decomposable.
    emit(
        PhaseStarted(
            component=ARCHITECT_COMPONENT,
            phase="audit",
            attempt=1,
        )
    )
    audit_start = time.monotonic()
    spec_issues = _parse_spec_issues(data)
    _surface_spec_issues(spec_issues, ui)
    for issue in spec_issues:
        emit(
            SpecIssueRecorded(
                severity=issue.severity,
                kind=issue.kind,
                summary=issue.summary,
                location=issue.location,
                suggestion=issue.suggestion,
            )
        )
    # #260: the halt keys on the architect's own disposition, not on a
    # severity label. Everything it decided, assumed or spiked rides
    # along; only a question it refused to answer stops the run.
    decisions = parse_decisions(data)
    requirements = parse_requirements(data)
    escalated = escalations(decisions)
    _surface_spec_decisions(decisions, ui)

    # The R1.7 artifacts are written FIRST, before any optional journal
    # work. On the halt path they are the only durable record the
    # operator gets, so nothing that can fail is allowed upstream of
    # them: #260 briefly put a config load in front of this and a typo
    # in an unrelated journal knob destroyed the findings of a paid run.
    #
    # One helper, called twice, so the two writes cannot drift on the
    # policy they share: attempt, announce, emit, and on OSError be
    # loud without masking. They stay two independent attempts because
    # neither may take the other down, and after #260 they hold
    # different halves of a halt.
    write_artifact = functools.partial(
        _write_decompose_artifact,
        ui=ui,
        emit=emit,
        rel_display=rel_display,
    )
    artifact_path = write_artifact(
        "spec_issues",
        "spec audit",
        lambda: persist_spec_issues(
            spec_issues,
            root_dir=root_dir,
            project_name=project_name,
            spec_file=spec_path.name,
            halted=bool(escalated),
        ),
    )
    # #260 round 2: the register is written on the HALT path here, and
    # on the success path only after the manifest commits (below).
    # Round 1 wrote it here unconditionally, so a decompose that halted
    # or failed later left a fresh register beside an OLDER manifest,
    # and the factory read it as that manifest's decisions. The halt
    # copy is stamped ``halted`` and the factory refuses to bind one.
    write_register = functools.partial(
        write_artifact,
        "decisions",
        "architect decisions",
    )
    decisions_path = None
    # #260: what this audit says about the previous one, read BEFORE
    # this run is appended to the journal below - otherwise the
    # "previous run" the report compares against would be this one.
    journal = _spec_audit_journal(root_dir, ui)
    # ONE read feeds both the trend and the accounting under it, so the
    # two are computed over the same snapshot and cannot disagree
    # because the file changed between two reads.
    snapshot = _journal_snapshot(journal, project_name)
    # #280: the trend is keyed on the project name, so a rename starts a
    # fresh one and the runs before it drop out of view. Keying
    # differently would be worse (the spec is edited every round, so a
    # content hash never matches, and #260's own loop renamed the file
    # too), so what is fixed is the silence rather than the key.
    _surface_convergence(
        _build_convergence(spec_issues, spec_path.name, snapshot.window),
        _excluded_history(snapshot.audits, project_name, spec_path.name, snapshot.lookback),
        project_name,
        ui,
    )
    _record_spec_issues_event(
        spec_issues,
        journal=journal,
        project_name=project_name,
        spec_file=spec_path.name,
        halted=bool(escalated),
        ui=ui,
    )
    # #696 slice 7: on the halt path too, since a conflict with the [stack]
    # kstrl.toml holds is one the architect escalates.
    _file_stack_proposal(data, root_dir, stack_pin, spec_source, bus, ui)
    emit(
        PhaseCompleted(
            component=ARCHITECT_COMPONENT,
            phase="audit",
            passed=not escalated,
            detail=f"{len(escalated)} escalation(s)" if escalated else "",
            duration_seconds=round(time.monotonic() - audit_start, 2),
        )
    )
    if escalated:
        decisions_path = write_register(
            lambda: write_decisions(
                decisions,
                root_dir=root_dir,
                project_name=project_name,
                spec_file=spec_path.name,
                halted=True,
                spec_digest=pin,
                answered_items=answers.item_ids,
                answers_digest=answers.digest,
                requirements=requirements,
            ),
        )
        open_escalation_item(
            escalated,
            root_dir,
            project_name,
            spec_source,
            spec_digest=pin,
            register_path=rel_display(decisions_path) if decisions_path is not None else "",
            run_id=bus.run_id if bus is not None else "",
            warn=ui.warn,
            spec_path=spec_path,
        )
        # The run dir must read as FINISHED, not dead: the halt is the
        # architect's judgment, delivered before the error propagates.
        emit(
            ComponentFailed(
                component=ARCHITECT_COMPONENT,
                error=f"spec halted: {len(escalated)} escalated question(s)",
            )
        )
        emit(
            RunCompleted(
                completed=0,
                failed=1,
                duration_seconds=round(time.monotonic() - run_started, 2),
            )
        )
        raise SpecBlockerError(
            escalated,
            artifact_path=artifact_path,
            decisions_path=decisions_path,
        )

    # The forming DAG, the moment it is known (C5's board draws from
    # this - no manifest read needed). The architect row stays first.
    emit(
        RunPlan(
            components=(
                {"id": ARCHITECT_COMPONENT, "title": "Architect / PRD red-team", "deps": []},
                *(
                    {
                        "id": comp_data["id"],
                        "title": comp_data["title"],
                        "deps": comp_data.get("dependencies", []),
                    }
                    for comp_data in data["components"]
                ),
            )
        )
    )

    # R7.4: Linear hook - one project per manifest, one issue per
    # component, non-blocker spec findings into Triage. Runs BEFORE
    # branch derivation so issue identifiers can ride the branch names
    # (Linear's GitHub integration links PRs by identifier-in-branch,
    # so status transitions cost zero API calls). The hook never
    # raises; any failure warns and decompose proceeds without Linear.
    linear_config = LinearConfig.load(root_dir)
    linear_sync = None
    if linear_config.enabled:
        ui.section("Syncing to Linear")
        linear_sync = sync_decompose(
            project_name=project_name,
            components=data["components"],
            spec_issues=spec_issues,
            config=linear_config,
            client=LinearClient(linear_config, warn=ui.warn, log=ui.info),
            warn=ui.warn,
        )
        if linear_sync is not None:
            ui.ok(f"Linear project created with {len(linear_sync.issues)} component issue(s)")
        if linear_sync is not None and single_pr:
            # All components share one branch in single-PR mode, so a
            # per-component identifier cannot ride the branch name.
            ui.warn(
                "linear: single_pr mode - branch names and PR bodies "
                "are not issue-linked; issues and the progress sink "
                "still work"
            )

    # Pre-validate every branch name before any file is written.
    # Component ids were validated in the retry loop; this can only
    # fire for a project_name (user input) that is not branch-safe.
    # Reject rather than sanitize so the caller sees exactly what
    # was wrong (R0.6).
    component_branches: dict[str, str] = {}
    for comp_data in data["components"]:
        comp_id = comp_data["id"]
        branch = _component_branch(comp_id, project_name, single_pr)
        issue_ref = (
            linear_sync.issues.get(comp_id) if linear_sync is not None and not single_pr else None
        )
        if issue_ref is not None:
            linear_branch = linear_branch_name(issue_ref.identifier, comp_id)
            if validate_branch_name(linear_branch) is None:
                branch = linear_branch
            else:
                # An identifier that breaks branch validation would be
                # a Linear-side surprise; degrade to the default branch
                # (losing auto-linking) rather than failing decompose.
                ui.warn(
                    f"linear: issue identifier "
                    f"'{issue_ref.identifier}' does not form a valid "
                    f"branch; component '{comp_id}' keeps its default "
                    f"branch name"
                )
        branch_error = validate_branch_name(branch)
        if branch_error:
            raise ValueError(
                f"Cannot derive a git branch for component '{comp_id}': {branch_error}"
            )
        component_branches[comp_id] = branch

    # Generate PRDs and build manifest components. Everything below has
    # already validated, so a failure here is an I/O problem or a
    # harness bug; either way, remove the files written so far rather
    # than leaving partial decompose state for the next run to trip
    # over (R1.8). The spec-issues artifact is deliberately NOT cleaned
    # up - it is the audit record.
    ui.section("Generating PRDs")
    manifest_components: list[Component] = []
    written_prds: list[Path] = []
    created_dirs: list[Path] = []
    # #260: recomputed from the same payload the retry loop validated,
    # so the specIssues block written below is the one that passed
    # PRD.validate_schema up there.
    routed_issues = _routed_prd_issues(data)
    # #568: one id for this decompose, stamped on every component it puts
    # in the manifest and naming the directory its planned PRDs go in.
    plan_id = mint_run_id("decompose")
    try:
        for comp_data in data["components"]:
            comp_id = comp_data["id"]
            branch = component_branches[comp_id]

            # Track directories this run creates so cleanup can remove
            # them; pre-existing directories are left alone.
            probe = plan_prd_path(root_dir, comp_id, plan_id=plan_id).parent
            while not probe.exists() and probe != root_dir:
                created_dirs.append(probe)
                probe = probe.parent

            prd_path = _generate_component_prd(
                comp_data,
                root_dir,
                branch,
                routed_issues[comp_id],
                plan_id=plan_id,
            )
            written_prds.append(prd_path)
            # The path the engineer's copy has in its worktree and on
            # the component branch. Nothing writes it in the root
            # checkout (#545).
            rel_prd = f"scripts/kstrl/feature/{comp_id}/prd.json"

            issue_ref = linear_sync.issues.get(comp_id) if linear_sync is not None else None
            manifest_components.append(
                Component(
                    id=comp_id,
                    title=comp_data["title"],
                    description=comp_data["description"],
                    dependencies=comp_data.get("dependencies", []),
                    prd_path=rel_prd,
                    branch_name=branch,
                    status=ComponentStatus.PENDING.value,
                    plan_id=plan_id,
                    linear_issue_id=issue_ref.id if issue_ref else "",
                    linear_issue_identifier=(issue_ref.identifier if issue_ref else ""),
                )
            )
            ui.ok(
                _prd_summary_line(
                    comp_id,
                    len(comp_data["userStories"]),
                    routed_issues[comp_id],
                )
            )

        manifest = Manifest(
            version="1",
            spec_file=spec_path.name,
            project_name=project_name,
            base_branch=base_branch,
            single_pr=single_pr,
            components=manifest_components,
            linear_project_id=(linear_sync.project_id if linear_sync is not None else ""),
            linear_sync_key=(linear_sync.sync_key if linear_sync is not None else ""),
            spec_path=spec_source,
            spec_digest=pin,
            stack_digest=stack_pin,
        )

        # Validate DAG
        dag_errors = manifest.validate_dag()
        if dag_errors:
            ui.warn("DAG validation warnings:")
            for err in dag_errors:
                ui.warn(f"  {err}")

        # Save manifest (atomic write; covered by the cleanup scope so
        # a save failure does not strand PRDs without a manifest)
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
        # #597: a live factory run owns this manifest and saves its whole
        # in-memory copy, so the manifest and the register are written under
        # the run lock. `ks factory --spec` already holds it (acquired
        # before this call, ahead of the architect's spend, #597); `ks
        # decompose` has no caller's lock and takes its own here. Either
        # way a held lock raises FactoryLockHeldError inside the cleanup
        # scope, so a refused decompose leaves no PRDs behind.
        from kstrl.factory_lock import held_or_acquired_run_lock

        with held_or_acquired_run_lock(run_lock, root_dir, ui, force_lock):
            manifest.save(manifest_path)
            ui.ok(f"Manifest saved: {manifest_path}")
            # AFTER the manifest, never before: the register binds engineers
            # to this manifest, so it must not exist unless this manifest
            # does. Inside the cleanup scope for the same reason.
            write_register(
                lambda: write_decisions(
                    decisions,
                    root_dir=root_dir,
                    project_name=project_name,
                    spec_file=spec_path.name,
                    halted=False,
                    spec_digest=pin,
                    answered_items=answers.item_ids,
                    answers_digest=answers.digest,
                    requirements=requirements,
                ),
                required=True,
            )
    except BaseException:
        for prd_file in written_prds:
            try:
                prd_file.unlink()
            except OSError:
                pass
        # Deepest-first so children go before parents; rmdir refuses
        # non-empty directories, which protects anything user-owned.
        for created in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
            try:
                created.rmdir()
            except OSError:
                pass
        raise
    # #449: after the manifest and the register commit, never inside the
    # cleanup scope above, so an escalation is resolved only for a
    # decompose that is on disk.
    resolve_escalation_items(
        root_dir,
        project_name,
        spec_source,
        spec_digest=pin,
        run_id=bus.run_id if bus is not None else "",
        info=ui.info,
        warn=ui.warn,
    )

    # Publish transactional artifacts only after the PRD + manifest
    # write set commits. If a later write failed, the cleanup above
    # removed the PRDs and the event stream must not claim they exist.
    for component_data, prd_file in zip(
        data["components"],
        written_prds,
        strict=True,
    ):
        emit(
            ArtifactWritten(
                component=component_data["id"],
                label="prd",
                path=rel_display(prd_file),
            )
        )
    emit(
        ArtifactWritten(
            label="manifest",
            path=rel_display(manifest_path),
        )
    )

    ui.section("Decomposition Summary")
    ui.kv("Components", str(len(manifest.components)))
    total_stories = sum(len(comp_data.get("userStories", [])) for comp_data in data["components"])
    ui.kv("Total stories", str(total_stories))

    duration = round(time.monotonic() - run_started, 2)
    emit(
        ComponentCompleted(
            component=ARCHITECT_COMPONENT,
            duration_seconds=duration,
            iterations=attempts_used,
        )
    )
    # completed counts THIS run's work item (the architect); the
    # planned components are the FACTORY run's job.
    emit(RunCompleted(completed=1, duration_seconds=duration))

    return manifest


def decompose_spec(
    spec_path: Path,
    project_name: str,
    base_branch: str,
    single_pr: bool,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    max_retries: int = 3,
    *,
    bus: EventBus | None = None,
    transcript: Callable[[str], None] | None = None,
    prompt_call: AgentCall,
    force_lock: bool = False,
    run_lock: _RunLock | None = None,
    timeout: float | None,
    before_spend: Callable[[], object],
) -> Manifest:
    """Run decomposition, guaranteeing a ``RunCompleted`` and a usage
    capture on every exit.

    ``run_lock``: see :func:`_decompose_spec_impl` (#597).

    ``timeout``: the architect call's limit in seconds, None for no limit
    (#603). A call the adapter kills at it is one failed attempt.

    #257: the capture sits in a ``finally`` because the blocker halt is
    the COMMON outcome on a first spec, and it is the path where the
    operator most needs the number before editing and re-running. An
    emit-after-return would miss exactly that case.

    Consequence, stated plainly: ``RunCompleted`` is emitted by the impl
    (both the halt site and the success tail) or by the ``except`` below,
    so ``ComponentUsage`` trails it on EVERY path, not just the halt. No
    consumer treats ``RunCompleted`` as a stream terminator - the reducer
    sets a flag and keeps folding - and every reader folds the whole
    stream, so position changes no total. A consumer that ever does stop
    there has to move this emit, not just handle it.
    """
    started = time.monotonic()
    # Read BEFORE the work so the report covers this call only.
    usage_before = usage_cursor(agent)
    try:
        return _decompose_spec_impl(
            spec_path=spec_path,
            project_name=project_name,
            base_branch=base_branch,
            single_pr=single_pr,
            agent=agent,
            ui=ui,
            root_dir=root_dir,
            max_retries=max_retries,
            bus=bus,
            transcript=transcript,
            prompt_call=prompt_call,
            force_lock=force_lock,
            run_lock=run_lock,
            timeout=timeout,
            before_spend=before_spend,
        )
    except SpecBlockerError:
        # Blocker halts are deliberately finalized at the audit site so
        # the durable artifact path can be attached to the exception.
        raise
    except BaseException as exc:
        if bus is not None:
            detail = f"{type(exc).__name__}: {exc}"
            bus.emit(
                ComponentFailed(
                    component=ARCHITECT_COMPONENT,
                    error=detail,
                )
            )
            bus.emit(
                RunCompleted(
                    failed=1,
                    duration_seconds=round(time.monotonic() - started, 2),
                )
            )
        raise
    finally:
        # Isolated because a `finally` that raises REPLACES the exception
        # propagating through it, which here would destroy the very halt
        # this reporting exists to cover: the SpecBlockerError becomes a
        # BrokenPipeError traceback and the documented exit code 2 is
        # lost.
        #
        # Measured (2026-08-30), because the obvious repro is the wrong
        # one: `ks decompose | head -5` does NOT do it. Both UIs default
        # to sys.stderr (ui/plain.py, ui/rich_ui.py), so piping stdout
        # leaves the stream the rollup writes to untouched. The shape
        # that fires is `ks decompose 2>&1 | head -5`, which raises
        # BrokenPipeError out of PlainUI._print's `print(...)` once the
        # writes pass the pipe buffer.
        #
        # `collect_usage` and `EventBus.emit` are already defensive; the
        # UI writes are not, and must not be made so inside
        # `print_usage_rollup` - the factory epilogue shares it and a
        # failed write there should be loud.
        try:
            _report_architect_usage(agent, ui, bus, since=usage_before)
        except Exception as exc:  # noqa: BLE001 - accounting never gates a run
            logger.warning("Failed to report architect usage: %s", exc)
