"""The calibration scorer: the matchers that grade a role's reply (#530).

Moved out of ``tests/test_calibration.py`` unchanged, so the paid
calibration suite and the prompt optimizer in :mod:`kstrl.gepa_adapter`
grade a reply with the same functions. Two scorers would let an
optimizer improve against one of them while calibration reads the other.
``tests/test_calibration.py`` imports every name here back, and the unit
tests that pin the matchers (``tests/test_calibration_matchers.py``)
reach them through that import.

The only edits made by the move are type parameters (``dict`` became
``dict[str, Any]``) that ``mypy --strict`` requires of ``kstrl/``.
Since then, #550 replaced the scorer's copy of the severity ranking with
``security._SEVERITY_ORDER``, the ranking the Phase 2.5 gate reads.

#564: every field a matcher reads from a fixture meta is read here first,
against the vocabulary the matcher compares it with, and a field that
cannot be read is refused with :class:`FixtureMetaError`. It used to be
defaulted: an unknown ``severity_at_least`` ranked 0, so every finding met
it and the fixture scored as caught whatever the role reported. Every
field is required and no block may carry a field no matcher reads, so a
misspelt field name is refused too instead of switching its gate off.
The architect matchers in ``tests/test_calibration.py`` and the reuse
matcher in ``tests/helpers/calibration_repo_fixture.py`` read their blocks
through the readers here, and the calibration loader and
:func:`kstrl.gepa_adapter.split_fixtures` refuse a whole meta through
:func:`check_fixture_meta` before any agent call.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from kstrl.decisions import enum_field_error, required_field_error
from kstrl.decompose import _VALID_KINDS
from kstrl.review import VALID_CONCERN_CATEGORIES, VALID_CONCERN_SEVERITIES, ReviewResult
from kstrl.security import _SEVERITY_ORDER, VALID_CATEGORIES, SecurityResult

# Realistic mechanical-verification context. The old ``_VERIFICATION_STUB``
# used check names the harness never emits ("tests", "lint"); the real
# reviewer prompt is fed one ``- <check.name>: <PASS|FAIL> - <message>``
# line per check by review.build_review_prompt, where the names come from
# verify.py (test_suite / typecheck / linter / diff_scope / self_critique).
# A clean, passing diff produces this.
_DEFAULT_VERIFICATION: tuple[dict[str, Any], ...] = (
    {"name": "test_suite", "passed": True, "message": "Tests passed"},
    {"name": "typecheck", "passed": True, "message": "Typecheck passed"},
    {"name": "linter", "passed": True, "message": "Linter passed"},
    {"name": "diff_scope", "passed": True, "message": "all files within scope"},
    {"name": "self_critique", "passed": True, "message": "failure modes listed"},
)


def render_verification(meta: dict[str, Any]) -> str:
    """Render a fixture's verification context into the exact shape the
    factory feeds Phase 2 review: one ``- <name>: <PASS|FAIL> - <message>``
    line per check (see review.build_review_prompt).

    Uses the fixture's ``verification`` list when present, so a fixture
    can encode a realistic mixed pass/fail context; otherwise falls back
    to a production-shaped all-pass default. This replaces the old
    all-PASS ``_VERIFICATION_STUB`` (R5.2 context realism) whose check
    names matched no real verifier, so measured reviewer detection now
    transfers to production.
    """
    checks: Sequence[dict[str, Any]] = meta.get("verification") or _DEFAULT_VERIFICATION
    lines: list[str] = []
    for check in checks:
        status = "PASS" if check.get("passed", True) else "FAIL"
        name = str(check.get("name", "check"))
        message = str(check.get("message", ""))
        lines.append(f"- {name}: {status} - {message}")
    return "\n".join(lines)


# Severity ordering: security._SEVERITY_ORDER, the one the gate reads (#550)


def _meets_severity(actual: str, threshold: str) -> bool:
    # ``threshold`` is the fixture's floor. The reader refuses one outside
    # the ranking, so it is looked up without a default (#564).
    # ``actual`` is the role's reply: the parser drops a finding whose
    # severity is outside the ranking, and one built by hand ranks 0.
    return _SEVERITY_ORDER.get(actual, 0) >= _SEVERITY_ORDER[threshold]


# ---------------------------------------------------------------------------
# Fixture meta readers (#564)
#
# Each returns every unreadable field of one block as a message that names
# the field, and an empty list when the block can be read. They check the
# RAW block before any matcher reads it, against the constant the matcher
# compares with: the severity ranking the gate reads, the categories and
# concern severities the reply parsers keep, the spec-issue kinds the
# decompose parser keeps.
# ---------------------------------------------------------------------------


class FixtureMetaError(ValueError):
    """A calibration fixture meta a matcher cannot read (#564).

    Raised in place of a score, never counted as a miss or a clean run.
    The message names every unreadable field; a caller that knows the
    fixture (:func:`check_fixture_meta`) names the fixture as well.
    """


def refuse_unreadable(errors: Sequence[str]) -> None:
    """Raise :class:`FixtureMetaError` when a reader found anything."""
    if errors:
        raise FixtureMetaError("; ".join(errors))


def _block_errors(prefix: str, block: Any, fields: Collection[str]) -> list[str]:
    """``block`` is an object holding exactly ``fields``.

    A missing field is refused rather than defaulted, and a field no
    matcher reads is refused rather than ignored, because a misspelt
    ``evidence_path_contains`` used to switch the path gate off.
    """
    if not isinstance(block, dict):
        return [f"{prefix}: must be an object, got {type(block).__name__}"]
    missing = [f"{prefix}.{name}: missing" for name in sorted(set(fields) - set(block))]
    unknown = [
        f"{prefix}.{name}: no matcher reads this field (it reads {sorted(fields)})"
        for name in sorted(set(block) - set(fields))
    ]
    return missing + unknown


def _bool_error(prefix: str, name: str, value: Any) -> str | None:
    if not isinstance(value, bool):
        return f"{prefix}.{name}: must be true or false, got {value!r}"
    return None


def _count_error(prefix: str, name: str, value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"{prefix}.{name}: must be a whole number of at least 1, got {value!r}"
    return None


def _list_errors(
    prefix: str,
    name: str,
    value: Any,
    valid: Collection[str] | None,
    *,
    empty_ok: bool = False,
) -> list[str]:
    """A list of strings, each one of ``valid``, or any non-empty string
    when ``valid`` is None. Empty only when ``empty_ok``: an empty list
    of categories matches nothing, so its fixture could never be caught
    or never be flagged."""
    if not isinstance(value, list):
        return [f"{prefix}.{name}: must be a list, got {type(value).__name__}"]
    if not value and not empty_ok:
        return [f"{prefix}.{name}: must not be empty"]
    found = [
        required_field_error(prefix, f"{name}[{index}]", item)
        if valid is None
        else enum_field_error(prefix, f"{name}[{index}]", item, valid)
        for index, item in enumerate(value)
    ]
    return [error for error in found if error]


def _detect_errors(
    prefix: str,
    block: Any,
    categories: Collection[str],
    severities: Collection[str],
) -> list[str]:
    """A positive fixture's block: one category field, a floor and a path."""
    if isinstance(block, dict) and ("category" in block) == ("category_any_of" in block):
        return [f"{prefix}.category, {prefix}.category_any_of: must carry exactly one of the two"]
    field = "category" if isinstance(block, dict) and "category" in block else "category_any_of"
    errors = _block_errors(prefix, block, (field, "severity_at_least", "evidence_path_contains"))
    if errors:
        return errors
    found = [
        enum_field_error(prefix, "severity_at_least", block["severity_at_least"], severities),
        required_field_error(prefix, "evidence_path_contains", block["evidence_path_contains"]),
    ]
    if field == "category":
        found.append(enum_field_error(prefix, field, block[field], categories))
    else:
        found.extend(_list_errors(prefix, field, block[field], categories))
    return [error for error in found if error]


def _flag_errors(
    prefix: str,
    block: Any,
    categories: Collection[str],
    severities: Collection[str],
) -> list[str]:
    """A negative fixture's block: the forbidden categories and the floor."""
    errors = _block_errors(prefix, block, ("categories", "severity_at_least"))
    if errors:
        return errors
    found = [
        *_list_errors(prefix, "categories", block["categories"], categories),
        enum_field_error(prefix, "severity_at_least", block["severity_at_least"], severities),
    ]
    return [error for error in found if error]


def security_detect_errors(prefix: str, block: Any) -> list[str]:
    return _detect_errors(prefix, block, VALID_CATEGORIES, _SEVERITY_ORDER)


def security_flag_errors(prefix: str, block: Any) -> list[str]:
    return _flag_errors(prefix, block, VALID_CATEGORIES, _SEVERITY_ORDER)


def reviewer_detect_errors(prefix: str, block: Any) -> list[str]:
    return _detect_errors(prefix, block, VALID_CONCERN_CATEGORIES, VALID_CONCERN_SEVERITIES)


def reviewer_flag_errors(prefix: str, block: Any) -> list[str]:
    return _flag_errors(prefix, block, VALID_CONCERN_CATEGORIES, VALID_CONCERN_SEVERITIES)


def architect_detect_errors(prefix: str, block: Any) -> list[str]:
    """A halting spec fixture's block, read by ``architect_caught``."""
    errors = _block_errors(
        prefix, block, ("spec_issues_min", "must_include_kind", "blocker_or_major")
    )
    if errors:
        return errors
    found = [
        _count_error(prefix, "spec_issues_min", block["spec_issues_min"]),
        *_list_errors(
            prefix, "must_include_kind", block["must_include_kind"], _VALID_KINDS, empty_ok=True
        ),
        _bool_error(prefix, "blocker_or_major", block["blocker_or_major"]),
    ]
    return [error for error in found if error]


def allowed_paths_errors(prefix: str, block: Any) -> list[str]:
    """A non-halting spec fixture's block, read by ``architect_allowed_paths_caught``."""
    errors = _block_errors(
        prefix,
        block,
        (
            "non_halting",
            "every_component_has_allowed_paths",
            "excludes_harness_internals",
            "includes_test_root_prefix",
            "includes_feature_subtree",
        ),
    )
    if errors:
        return errors
    found = [
        _bool_error(prefix, "non_halting", block["non_halting"]),
        _bool_error(
            prefix, "every_component_has_allowed_paths", block["every_component_has_allowed_paths"]
        ),
        *_list_errors(
            prefix,
            "excludes_harness_internals",
            block["excludes_harness_internals"],
            None,
            empty_ok=True,
        ),
        required_field_error(
            prefix, "includes_test_root_prefix", block["includes_test_root_prefix"]
        ),
        _bool_error(prefix, "includes_feature_subtree", block["includes_feature_subtree"]),
    ]
    return [error for error in found if error]


def reuse_errors(prefix: str, block: Any) -> list[str]:
    """A reuse fixture's block, read by the reuse matcher (#401)."""
    errors = _block_errors(prefix, block, ("existing_module", "existing_symbol", "module_markers"))
    if errors:
        return errors
    found = [
        required_field_error(prefix, "existing_module", block["existing_module"]),
        required_field_error(prefix, "existing_symbol", block["existing_symbol"]),
        *_list_errors(prefix, "module_markers", block["module_markers"], None),
    ]
    return [error for error in found if error]


#: The matcher blocks a fixture of each role may carry. A fixture carries
#: exactly one of its role's blocks.
_BLOCKS: dict[str, tuple[str, ...]] = {
    "security": ("must_detect", "must_not_flag"),
    "reviewer": ("must_detect", "must_not_flag"),
    "architect": ("must_detect", "must_emit_allowed_paths"),
    "architect_reuse": ("must_reuse",),
}


def _read_block(role: str, block: str, value: Any) -> list[str]:
    """``value`` read by the reader of ``role``'s ``block``.

    Direct calls rather than a table of functions, so a static walk of
    ``kstrl/`` resolves every callee (tests/test_astwalk.py pins the
    package's dispatch tables at four).
    """
    if role == "security":
        if block == "must_detect":
            return security_detect_errors(block, value)
        return security_flag_errors(block, value)
    if role == "reviewer":
        if block == "must_detect":
            return reviewer_detect_errors(block, value)
        return reviewer_flag_errors(block, value)
    if role == "architect":
        if block == "must_detect":
            return architect_detect_errors(block, value)
        return allowed_paths_errors(block, value)
    return reuse_errors(block, value)


def fixture_meta_errors(meta: Any) -> list[str]:
    """Every field of one fixture meta that no matcher can read."""
    if not isinstance(meta, dict):
        return [f"meta: must be an object, got {type(meta).__name__}"]
    error = enum_field_error("meta", "role", meta.get("role"), _BLOCKS)
    if error:
        return [error]
    role = meta["role"]
    present = [name for name in _BLOCKS[role] if name in meta]
    if len(present) != 1:
        return [f"meta: a {role} fixture carries exactly one of {sorted(_BLOCKS[role])}"]
    return _read_block(role, present[0], meta[present[0]])


def check_fixture_meta(meta: Any, fixture: str) -> None:
    """Refuse ``meta`` unless a matcher can read every field it carries.

    ``fixture`` names the fixture in the message. Called before any agent
    call, so a fixture no matcher can read costs nothing and scores nothing.
    """
    errors = fixture_meta_errors(meta)
    if errors:
        raise FixtureMetaError(f"{fixture}: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# Matcher helpers (F5-matchers)
#
# Extracted as top-level functions so they can be unit-tested against
# synthetic inputs in tests/test_calibration_matchers.py without
# requiring a real LLM call. The paid tests in tests/test_calibration.py feed the same
# helpers with output produced by a live agent under
# KSTRL_RUN_CALIBRATION=1. Identical matcher means: a passing unit
# test = the matcher works against the assumptions encoded in the
# fixture meta; a passing integration test = the matcher AND the LLM
# both held up.
# ---------------------------------------------------------------------------


def _acceptable_categories(requirement: dict[str, Any]) -> set[str]:
    """Categories that count as a hit for a positive fixture.

    Supports either a single ``category`` (legacy fixtures) or a
    ``category_any_of`` list. The list form exists for the R5.2 subtle
    bugs whose genuine finding could be labelled under more than one
    taxonomy bucket -- e.g. a signature-comparison timing oracle is
    defensibly ``broken_crypto`` OR ``information_disclosure`` OR
    ``auth_bypass``. Accepting any of them measures *whether the model
    saw the flaw*, not whether it guessed our preferred label; the
    ``evidence_path_contains`` gate still pins the finding to the right
    file so this does not become a free pass.
    """
    if "category_any_of" in requirement:
        return {str(c) for c in requirement["category_any_of"]}
    return {str(requirement["category"])}


def security_caught(
    result: SecurityResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the security matcher.

    A finding counts as catching the planted bug when its category is
    among the acceptable categories, severity meets the threshold, and
    location contains the expected path substring. First
    match wins; subsequent findings are ignored even if more severe --
    this mirrors how the review path would gate (one matching finding
    is enough).
    """
    refuse_unreadable(security_detect_errors("must_detect", requirement))
    acceptable = _acceptable_categories(requirement)
    for finding in result.findings:
        if finding.category not in acceptable:
            continue
        if not _meets_severity(finding.severity, requirement["severity_at_least"]):
            continue
        if requirement["evidence_path_contains"] not in finding.location:
            continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def security_false_positive(
    result: SecurityResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(is_false_positive, detail)`` for a security NEGATIVE
    fixture.

    ``requirement`` is the fixture's ``must_not_flag`` block:
    ``{"categories": [...], "severity_at_least": "high"}``. A false
    positive is any finding whose category is forbidden AND whose
    severity meets the floor (every saved negative uses ``medium``).
    Sub-floor findings are ignored so a low-severity nit does not
    inflate the FP rate.
    """
    refuse_unreadable(security_flag_errors("must_not_flag", requirement))
    forbidden = set(requirement["categories"])
    floor = requirement["severity_at_least"]
    for finding in result.findings:
        if finding.category not in forbidden:
            continue
        if not _meets_severity(finding.severity, floor):
            continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def reviewer_caught(
    result: ReviewResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the reviewer matcher.

    A concern counts as catching the planted issue when its category is
    among the acceptable categories, its severity is ``"fail"`` when the
    fixture demands ``severity_at_least == "fail"`` (``"advisory"``
    accepts either severity), and its location contains the expected
    path substring.
    """
    refuse_unreadable(reviewer_detect_errors("must_detect", requirement))
    acceptable = _acceptable_categories(requirement)
    for concern in result.concerns:
        if concern.category not in acceptable:
            continue
        if requirement["severity_at_least"] == "fail" and concern.severity != "fail":
            continue
        if requirement["evidence_path_contains"] not in concern.location:
            continue
        return True, (f"{concern.severity} {concern.category} at {concern.location}")
    return False, ""


def reviewer_false_positive(
    result: ReviewResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(is_false_positive, detail)`` for a reviewer NEGATIVE
    fixture.

    ``requirement`` is the fixture's ``must_not_flag`` block. A false
    positive is a concern whose category is forbidden. When the floor is
    ``fail``, only *blocking* concerns count: an advisory concern does
    not halt the PR, so it does not erode halt credibility. A floor of
    ``advisory`` also counts advisory concerns.
    """
    refuse_unreadable(reviewer_flag_errors("must_not_flag", requirement))
    forbidden = set(requirement["categories"])
    floor = requirement["severity_at_least"]
    for concern in result.concerns:
        if concern.category not in forbidden:
            continue
        if floor == "fail" and concern.severity != "fail":
            continue
        return True, (f"{concern.severity} {concern.category} at {concern.location}")
    return False, ""
