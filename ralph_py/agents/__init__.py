"""Agent implementations for Ralph."""

from ralph_py.agents.base import Agent
from ralph_py.agents.claude_code import ClaudeCodeAgent
from ralph_py.agents.codex import CodexAgent
from ralph_py.agents.custom import CustomAgent

__all__ = ["Agent", "ClaudeCodeAgent", "CodexAgent", "CustomAgent", "get_agent"]


def get_agent(
    agent_cmd: str | None = None,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    agent_type: str | None = None,
) -> Agent:
    """Get appropriate agent based on configuration.

    Args:
        agent_cmd: Custom shell command (takes precedence over everything)
        model: Model name for the agent
        model_reasoning_effort: Reasoning effort for codex
        agent_type: Agent type: "claude-code", "codex", "auto", or None
    """
    if agent_cmd:
        return CustomAgent(agent_cmd)
    if agent_type == "claude-code":
        return ClaudeCodeAgent(model=model)
    if agent_type == "codex":
        return CodexAgent(model=model, reasoning_effort=model_reasoning_effort)
    # Auto-detect: prefer claude-code, fall back to codex
    if agent_type is None or agent_type == "auto":
        if ClaudeCodeAgent.is_available():
            return ClaudeCodeAgent(model=model)
    return CodexAgent(model=model, reasoning_effort=model_reasoning_effort)
