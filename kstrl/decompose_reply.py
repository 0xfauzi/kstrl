"""Examine the architect's JSON reply entry by entry and make one PRD file from each component of an accepted reply."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.atomicio import atomic_write_json
from kstrl.decisions import (
    DISPOSITION_ESCALATED,
    SpecDecision,
    decisions_payload_errors,
    escalations,
    parse_decisions,
)
from kstrl.events import EventBus
from kstrl.guards import ScopeHazard, scope_entry_hazard
from kstrl.names import validate_component_id
from kstrl.prd import PRD
from kstrl.requirements import (
    enum_field_error,
    required_field_error,
    requirements_payload_errors,
)
from kstrl.spec_issues import (
    _VALID_SEVERITIES,
    SPEC_ISSUE_APPLIES_COMPONENT,
    VALID_KINDS,
    _parse_spec_issues,
    route_spec_issues,
)
from kstrl.stack import file_stack_item, stack_errors, stack_from_table, stack_toml
from kstrl.statedir import plan_prd_path

if TYPE_CHECKING:
    from kstrl.ui.base import UI


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
                enum_field_error(prefix, "kind", entry.get("kind"), VALID_KINDS),
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
