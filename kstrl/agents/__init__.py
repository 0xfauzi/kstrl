"""Agent implementations for Ralph."""

from kstrl.agents.base import Agent
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.sandbox import SandboxConfig

__all__ = [
    "Agent",
    "ClaudeCodeAgent",
    "ClaudeSdkAgent",
    "CodexAgent",
    "CustomAgent",
    "get_agent",
]


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
        max_budget_usd: In-loop USD budget ceiling (R7.6). Only the
            claude-sdk adapter has an enforcement surface for it; the
            subprocess adapters ignore it (phase-boundary token budgets
            from R3.1 still apply to them).
    """
    if agent_cmd:
        return CustomAgent(agent_cmd)
    if agent_type == "claude-code":
        return ClaudeCodeAgent(
            model=model, effort=model_reasoning_effort, sandbox=sandbox,
        )
    if agent_type == "claude-sdk":
        return ClaudeSdkAgent(
            model=model, effort=model_reasoning_effort, sandbox=sandbox,
            max_budget_usd=max_budget_usd,
        )
    if agent_type == "codex":
        return CodexAgent(
            model=model, reasoning_effort=model_reasoning_effort,
            sandbox=sandbox,
        )
    # Auto-detect: prefer claude-code, fall back to codex
    if agent_type is None or agent_type == "auto":
        if ClaudeCodeAgent.is_available():
            return ClaudeCodeAgent(
                model=model, effort=model_reasoning_effort, sandbox=sandbox,
            )
    return CodexAgent(
        model=model, reasoning_effort=model_reasoning_effort, sandbox=sandbox,
    )
