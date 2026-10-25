"""The coding agents that kstrl calls.

The Agent protocol, the adapters, the adapter choice, the liveness reading,
the prompt records and the sandbox settings.
"""

from __future__ import annotations

from kstrl.agents.agent_output import (
    MAX_AGENT_OUTPUT_BYTES,
    AgentOutputTooLarge,
    collect_agent_output,
    extract_agent_json,
    extract_json,
    select_agent_output,
    timed_out_message,
)
from kstrl.agents.base import Agent
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.agents.liveness import CLAUDE_FAMILY, PROBE_ENV_VAR, UNPROBED, ProbeResult, probe_family
from kstrl.agents.logging import LoggingAgent
from kstrl.agents.prompt_record import AgentCall, recording_prompts
from kstrl.agents.sandbox import (
    SANDBOX_SECTION,
    SandboxConfig,
    warn_unconfined,
    with_stack_writable,
)
from kstrl.agents.select import UnknownAgentTypeError, get_agent
from kstrl.agents.tmpdir import agent_tmpdir

__all__ = [
    "Agent",
    "AgentCall",
    "AgentOutputTooLarge",
    "CLAUDE_FAMILY",
    "ClaudeCodeAgent",
    "ClaudeSdkAgent",
    "CodexAgent",
    "CustomAgent",
    "LoggingAgent",
    "MAX_AGENT_OUTPUT_BYTES",
    "PROBE_ENV_VAR",
    "ProbeResult",
    "SANDBOX_SECTION",
    "SandboxConfig",
    "UNPROBED",
    "UnknownAgentTypeError",
    "agent_tmpdir",
    "collect_agent_output",
    "extract_agent_json",
    "extract_json",
    "get_agent",
    "probe_family",
    "recording_prompts",
    "select_agent_output",
    "timed_out_message",
    "warn_unconfined",
    "with_stack_writable",
]
