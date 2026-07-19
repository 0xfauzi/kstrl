"""Agent implementations for Ralph."""

from ralph_py.agents.base import Agent
from ralph_py.agents.claude_code import ClaudeCodeAgent
from ralph_py.agents.codex import CodexAgent
from ralph_py.agents.custom import CustomAgent
from ralph_py.sandbox import SandboxConfig

__all__ = ["Agent", "ClaudeCodeAgent", "CodexAgent", "CustomAgent", "get_agent"]


def get_agent(
    agent_cmd: str | None = None,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    agent_type: str | None = None,
    sandbox: SandboxConfig | None = None,
) -> Agent:
    """Get appropriate agent based on configuration.

    Args:
        agent_cmd: Custom shell command (takes precedence over everything)
        model: Model name for the agent
        model_reasoning_effort: Reasoning effort for codex
        agent_type: Agent type: "claude-code", "codex", "auto", or None
        sandbox: OS-level sandbox intent (R7.5). Applied by the
            claude-code and codex adapters; a CustomAgent command has no
            generic sandbox surface, so the setting is ignored there and
            callers that enable it with a custom command must warn.
    """
    if agent_cmd:
        return CustomAgent(agent_cmd)
    if agent_type == "claude-code":
        return ClaudeCodeAgent(
            model=model, effort=model_reasoning_effort, sandbox=sandbox,
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
