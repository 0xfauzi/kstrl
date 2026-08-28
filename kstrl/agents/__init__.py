"""Agent implementations for kstrl."""

from kstrl.agents.base import Agent
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.sandbox import SandboxConfig

__all__ = [
    "AGENT_TYPE_ALIASES",
    "UnknownAgentTypeError",
    "VALID_AGENT_TYPES",
    "canonical_agent_type",
    "Agent",
    "ClaudeCodeAgent",
    "ClaudeSdkAgent",
    "CodexAgent",
    "CustomAgent",
    "get_agent",
]


class UnknownAgentTypeError(ValueError):
    """``[agent] type`` names an adapter that does not exist.

    Raised rather than tolerated because the old behavior was the worst
    possible one: an unrecognized type fell through every branch to the
    codex fallback, so `type = "claude"` silently ran Codex while the
    operator believed they were running Claude. A typo changed which
    model wrote the code and nothing said so.
    """


#: The one agent-type vocabulary, shared with the CLI preflight. Two
#: layers previously disagreed: the CLI accepted "claude" as an alias for
#: "claude-code", while get_agent did not and fell through to the codex
#: fallback, so a programmatic caller passing the DOCUMENTED spelling got
#: a different model than the CLI would have chosen. One table, one
#: vocabulary.
AGENT_TYPE_ALIASES: dict[str, str] = {
    "": "auto",
    "auto": "auto",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claude-sdk": "claude-sdk",
    "codex": "codex",
    "custom": "custom",
}

#: Accepted spellings for ``[agent] type``.
VALID_AGENT_TYPES: tuple[str, ...] = tuple(sorted(name for name in AGENT_TYPE_ALIASES if name))


def canonical_agent_type(agent_type: str | None) -> str | None:
    """Canonical spelling for a configured type, or None if unknown."""
    if agent_type is None:
        return None
    return AGENT_TYPE_ALIASES.get(agent_type.strip().lower())


def get_agent(
    agent_cmd: str | None = None,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    agent_type: str | None = None,
    sandbox: SandboxConfig | None = None,
    max_budget_usd: float | None = None,
) -> Agent:
    """Get appropriate agent based on configuration.

    Args:
        agent_cmd: Custom shell command (takes precedence over everything)
        model: Model name for the agent
        model_reasoning_effort: Reasoning effort for codex
        agent_type: Agent type: "claude-code", "claude-sdk", "codex",
            "auto", or None. "claude-sdk" is opt-in only - "auto" never
            selects it (the subprocess adapters stay the default; the
            SDK is an optional dependency).
        sandbox: OS-level sandbox intent (R7.5). Applied by the
            claude-code, claude-sdk, and codex adapters; a CustomAgent
            command has no generic sandbox surface, so the setting is
            ignored there and callers that enable it with a custom
            command must warn.
        max_budget_usd: Per-turn USD budget ceiling (R7.6). Only the
            claude-sdk adapter has an enforcement surface for it; the
            subprocess adapters ignore it. Their ceiling is the
            run-level ``[factory] max_total_tokens``, enforced between
            engineer iterations (R8) and at phase boundaries (R3.1) -
            adapter-agnostic, but coarser: it cannot interrupt a call
            already in flight.
    """
    if agent_cmd:
        return CustomAgent(agent_cmd)
    if agent_type is not None:
        canonical = canonical_agent_type(agent_type)
        if canonical is None:
            raise UnknownAgentTypeError(
                f"unknown [agent] type {agent_type!r}; expected one of "
                f"{', '.join(VALID_AGENT_TYPES)}. Leave it unset (or use "
                "'auto') to auto-detect. This raises rather than falling "
                "back because an unrecognized type used to resolve silently "
                "to the codex adapter - a typo changed which model wrote "
                "your code and nothing said so."
            )
        if canonical == "custom":
            raise UnknownAgentTypeError(
                'agent type "custom" is configured but no agent command is '
                "set; set [agent] command in kstrl.toml, AGENT_CMD, or "
                "--agent-cmd. Without one there is nothing to run, and this "
                "used to fall through to the codex adapter instead of saying so."
            )
        agent_type = canonical
    if agent_type == "claude-code":
        return ClaudeCodeAgent(
            model=model,
            effort=model_reasoning_effort,
            sandbox=sandbox,
        )
    if agent_type == "claude-sdk":
        return ClaudeSdkAgent(
            model=model,
            effort=model_reasoning_effort,
            sandbox=sandbox,
            max_budget_usd=max_budget_usd,
        )
    if agent_type == "codex":
        return CodexAgent(
            model=model,
            reasoning_effort=model_reasoning_effort,
            sandbox=sandbox,
        )
    # Auto-detect: prefer claude-code, fall back to codex
    if agent_type is None or agent_type == "auto":
        if ClaudeCodeAgent.is_available():
            return ClaudeCodeAgent(
                model=model,
                effort=model_reasoning_effort,
                sandbox=sandbox,
            )
    return CodexAgent(
        model=model,
        reasoning_effort=model_reasoning_effort,
        sandbox=sandbox,
    )
