"""Every string-valued configuration field, and whether its values are closed (#562).

A closed field takes one of a fixed set of values. Before #562
``[factory] review_mode`` was checked only when Phase 2 parsed it into a
``ReviewMode``, so a typo in kstrl.toml ran and paid for the engineer
before it failed. The rule this ledger serves: a closed field is refused
where it is READ, so ``config_preflight`` reports it before any agent
call, against the same vocabulary the use site parses with.

The census is closed by construction over TYPES. :func:`string_fields`
enumerates every field of every class ``config_sections()`` loads whose
annotation can hold a string (``str``, a ``str`` subclass such as a
``StrEnum``, a ``Literal``, or a union or container of those), and
``tests/test_closed_vocabulary_config.py`` fails unless each one is in
exactly one of the three tables below. A new field shows up as an
unclassified name, not as a hole in a list.

The tables are the human half and the census cannot check them: a
closed field written into ``OPEN`` is trusted. The use-site walk in the
test file catches that for a field parsed through a ``StrEnum``; a
field checked with ``in`` or ``==`` at its use site is a disclosed blind
spot there, with a strict xfail.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from dataclasses import dataclass
from typing import Literal

from kstrl.agents import VALID_AGENT_TYPES
from kstrl.config_preflight import config_sections
from kstrl.contract import ContractMode
from kstrl.divergence import DivergenceMode
from kstrl.factory import VALID_CLAIM_AGREEMENT
from kstrl.gateparse import GATE_LINT, GATE_TEST, GATE_TOOLS, GATE_TYPECHECK
from kstrl.knowledge import _VALID_DEPENDENCY_SCOPES
from kstrl.linear import _VALID_AUTH_MODES
from kstrl.review import ReviewMode
from kstrl.security import VALID_SEVERITIES, SecurityMode
from kstrl.verify import FAST_ITERATION_GATES

#: A value no closed field accepts. Distinct enough to find in output.
BAD_VALUE = "bogus-562"


@dataclass(frozen=True)
class ClosedField:
    """Where a closed field is read and what it accepts."""

    section: str
    key: str
    #: The environment override, or None when the field has none.
    env: str | None
    #: The vocabulary the use site parses with, taken from kstrl where
    #: kstrl names one.
    accepted: tuple[str, ...]
    #: True when the toml value is a list of names rather than one name.
    is_list: bool = False


#: (class, field) -> where it is read and what it accepts.
CLOSED: dict[tuple[str, str], ClosedField] = {
    ("KstrlConfig", "agent_type"): ClosedField(
        "agent", "type", "KSTRL_AGENT_TYPE", VALID_AGENT_TYPES
    ),
    ("FactoryConfig", "review_mode"): ClosedField(
        "factory", "review_mode", None, tuple(m.value for m in ReviewMode)
    ),
    ("FactoryConfig", "claim_agreement"): ClosedField(
        "factory", "claim_agreement", "KSTRL_FACTORY_CLAIM_AGREEMENT", VALID_CLAIM_AGREEMENT
    ),
    ("VerifyConfig", "test_tool"): ClosedField(
        "verify", "test_tool", "KSTRL_VERIFY_TEST_TOOL", GATE_TOOLS[GATE_TEST]
    ),
    ("VerifyConfig", "typecheck_tool"): ClosedField(
        "verify", "typecheck_tool", "KSTRL_VERIFY_TYPECHECK_TOOL", GATE_TOOLS[GATE_TYPECHECK]
    ),
    ("VerifyConfig", "lint_tool"): ClosedField(
        "verify", "lint_tool", "KSTRL_VERIFY_LINT_TOOL", GATE_TOOLS[GATE_LINT]
    ),
    ("VerifyConfig", "fast_iteration_checks"): ClosedField(
        "verify",
        "fast_iteration_checks",
        "KSTRL_VERIFY_FAST_ITERATION_CHECKS",
        FAST_ITERATION_GATES,
        is_list=True,
    ),
    ("SecurityConfig", "mode"): ClosedField(
        "security", "mode", "KSTRL_SECURITY_MODE", tuple(m.value for m in SecurityMode)
    ),
    ("SecurityConfig", "fail_threshold"): ClosedField(
        "security",
        "fail_threshold",
        "KSTRL_SECURITY_FAIL_THRESHOLD",
        tuple(sorted(VALID_SEVERITIES)),
    ),
    ("SecurityConfig", "agent_type"): ClosedField(
        "security", "agent_type", "KSTRL_SECURITY_AGENT_TYPE", VALID_AGENT_TYPES
    ),
    ("ContractConfig", "mode"): ClosedField(
        "contract", "mode", "KSTRL_CONTRACT_MODE", tuple(m.value for m in ContractMode)
    ),
    # kstrl/adequacy.py and kstrl/policy.py write these two vocabularies
    # inline in their validators and name no constant, so they are
    # restated here. Both were already refused at load before #562.
    ("AdequacyConfig", "layer0"): ClosedField(
        "adequacy", "layer0", "KSTRL_ADEQUACY_LAYER0", ("advisory", "block")
    ),
    ("PolicyConfig", "license_unresolved"): ClosedField(
        "policy", "license_unresolved", "KSTRL_POLICY_LICENSE_UNRESOLVED", ("block", "advisory")
    ),
    ("DivergenceConfig", "mode"): ClosedField(
        "divergence", "mode", "KSTRL_DIVERGENCE_MODE", tuple(m.value for m in DivergenceMode)
    ),
    ("KnowledgeConfig", "dependency_scope"): ClosedField(
        "knowledge",
        "dependency_scope",
        "KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE",
        tuple(sorted(_VALID_DEPENDENCY_SCOPES)),
    ),
    ("LinearConfig", "auth_mode"): ClosedField(
        "linear", "auth_mode", "KSTRL_LINEAR_AUTH_MODE", tuple(sorted(_VALID_AUTH_MODES))
    ),
}

#: (class, field) -> why its values are not a fixed set kstrl refuses.
OPEN: dict[tuple[str, str], str] = {
    ("KstrlConfig", "allowed_paths"): "paths",
    ("KstrlConfig", "kstrl_branch"): "a git branch name",
    ("KstrlConfig", "agent_cmd"): "a shell command",
    ("KstrlConfig", "model"): "a model name; the agent CLI owns that vocabulary",
    ("KstrlConfig", "model_reasoning_effort"): (
        "passed verbatim to the agent CLI; kstrl names no vocabulary for it"
    ),
    ("KstrlConfig", "ui_mode"): (
        "display only: config_report.normalize_ui_mode maps an unknown value "
        "to auto, and nothing refuses it"
    ),
    ("FactoryConfig", "review_agent_cmd"): "a shell command",
    ("FactoryConfig", "review_model"): "a model name",
    ("VerifyConfig", "test_command"): "a shell command",
    ("VerifyConfig", "typecheck_command"): "a shell command",
    ("VerifyConfig", "lint_command"): "a shell command",
    ("VerifyConfig", "dead_code_command"): "a shell command",
    ("VerifyConfig", "progress_file_path"): "a path",
    ("SecurityConfig", "agent_cmd"): "a shell command",
    ("SecurityConfig", "model"): "a model name",
    ("ContractConfig", "test_command"): "a shell command",
    ("PolicyConfig", "paths_deny"): "path globs",
    ("PolicyConfig", "secret_patterns"): "regular expressions",
    ("PolicyConfig", "enforcement_paths_extra"): "paths",
    ("PolicyConfig", "license_allow"): "SPDX identifiers, an open set",
    ("PolicyConfig", "license_deny_partial"): "SPDX identifier fragments, an open set",
    ("BreakerConfig", "test_command"): "a shell command",
    ("KnowledgeConfig", "distill_model"): "a model name",
    ("GitHubIntakeConfig", "repo"): "owner/name of a repository",
    ("GitHubIntakeConfig", "queued_label"): "a label name",
    ("GitHubIntakeConfig", "label_prefix"): "a label prefix",
    ("GitHubIntakeConfig", "allowed_actors"): "GitHub logins",
    ("NotifyConfig", "on_complete"): "a shell command",
    ("NotifyConfig", "on_first_failure"): "a shell command",
    ("NotifyConfig", "on_inbox_item"): "a shell command",
    ("LinearConfig", "team_id"): "a Linear team id",
    ("LinearConfig", "token_env"): "an environment variable name",
    ("LinearConfig", "api_url"): "a URL",
    ("ReleaseConfig", "environment"): "a deployment environment name",
    ("SignalsConfig", "product"): "a product name",
    ("SignalsConfig", "base_url"): "a URL",
    ("SignalsConfig", "project_id"): "a tracker project id",
    ("SignalsConfig", "token_env"): "an environment variable name",
}

#: Every CLI flag that sets a CLOSED field (#565): (command path, option)
#: -> the CLOSED row it sets. ``tests/test_cli_flag_vocabularies.py``
#: drives each one through the real CLI with every value the field accepts
#: and values it refuses; ``tests/test_cli_vocabularies.py`` fails on a
#: Choice option that is in neither this table nor its own ledger, and on
#: an option named after a CLOSED field that is not in this table.
FLAG_FIELDS: dict[tuple[tuple[str, ...], str], tuple[str, str]] = {
    (("config", "show"), "--agent-type"): ("KstrlConfig", "agent_type"),
    (("decompose",), "--agent-type"): ("KstrlConfig", "agent_type"),
    (("factory",), "--agent-type"): ("KstrlConfig", "agent_type"),
    (("factory",), "--review-mode"): ("FactoryConfig", "review_mode"),
    (("factory",), "--security-mode"): ("SecurityConfig", "mode"),
    (("factory",), "--security-fail-threshold"): ("SecurityConfig", "fail_threshold"),
    (("factory",), "--contract-check"): ("ContractConfig", "mode"),
}

#: (class, field) -> the kstrl.toml key that must NOT be read for it.
#: Closed values, but no loader reads the field, so there is nothing to
#: refuse at load. The test proves the key is refused as unread (#525).
NOT_READ: dict[tuple[str, str], tuple[str, str]] = {
    ("FactoryConfig", "review_agent_type"): ("factory", "review_agent_type"),
}


def _holds_string(annotation: object) -> bool:
    if annotation is str:
        return True
    if isinstance(annotation, type):
        return issubclass(annotation, str)
    if typing.get_origin(annotation) is Literal:
        return True
    return any(_holds_string(arg) for arg in typing.get_args(annotation) if arg is not Ellipsis)


def _names_a_vocabulary(annotation: object) -> bool:
    if isinstance(annotation, type):
        return issubclass(annotation, enum.Enum)
    if typing.get_origin(annotation) is Literal:
        return True
    return any(
        _names_a_vocabulary(arg) for arg in typing.get_args(annotation) if arg is not Ellipsis
    )


def typed_closed_fields(cls: type) -> list[str]:
    """Every field of ``cls`` whose TYPE names its vocabulary: an Enum or a Literal.

    Such a field is closed by its own annotation, so it may not be
    classified OPEN. No config field is typed this way on ae01e66; the
    rule is for the next one.
    """
    hints = typing.get_type_hints(cls)
    return [
        f.name
        for f in dataclasses.fields(cls)
        if not f.metadata.get("provenance") and _names_a_vocabulary(hints[f.name])
    ]


def string_fields(cls: type) -> list[str]:
    """Every field of ``cls`` whose annotation can hold a string.

    Provenance fields (``metadata["provenance"]``) are skipped: they
    have no kstrl.toml key, no environment variable and no flag.
    """
    hints = typing.get_type_hints(cls)
    return [
        f.name
        for f in dataclasses.fields(cls)
        if not f.metadata.get("provenance") and _holds_string(hints[f.name])
    ]


def string_field_census() -> set[tuple[str, str]]:
    """(class, field) for every string-valued field of every loaded section."""
    found: set[tuple[str, str]] = set()
    for section in config_sections():
        cls = section.loader.__self__
        found.update((cls.__name__, name) for name in string_fields(cls))
    return found
