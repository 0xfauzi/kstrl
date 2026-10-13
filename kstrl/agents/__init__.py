"""The coding agents that kstrl calls.

The Agent protocol, the adapters, the adapter choice, the liveness reading,
the prompt records and the sandbox settings.
"""

from __future__ import annotations

from kstrl.agents.base import Agent
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.agents.select import UnknownAgentTypeError, get_agent

__all__ = [
    "Agent",
    "ClaudeCodeAgent",
    "ClaudeSdkAgent",
    "CodexAgent",
    "CustomAgent",
    "UnknownAgentTypeError",
    "get_agent",
]
