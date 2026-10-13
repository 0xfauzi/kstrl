"""The choice of the agent adapter from the [agent] settings."""

from __future__ import annotations

from pathlib import Path

from kstrl.agents.base import Agent
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.config import VALID_AGENT_TYPES, canonical_agent_type
from kstrl.sandbox import SandboxConfig


class UnknownAgentTypeError(ValueError):
    """``[agent] type`` names an adapter that does not exist.

    Raised rather than tolerated because the old behavior was the worst
    possible one: an unrecognized type fell through every branch to the
    codex fallback, so `type = "claude"` silently ran Codex while the
    operator believed they were running Claude. A typo changed which
    model wrote the code and nothing said so.
    """


def get_agent(
    agent_cmd: str | None = None,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    agent_type: str | None = None,
    sandbox: SandboxConfig | None = None,
    max_budget_usd: float | None = None,
    read_only: bool = False,
    *,
    root_dir: Path | None,
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
            command must refuse to start the role (#701).
        max_budget_usd: Per-turn USD budget ceiling (R7.6). Only the
            claude-sdk adapter has an enforcement surface for it; the
            subprocess adapters ignore it. Their ceiling is the
            run-level ``[factory] max_total_tokens``, enforced between
            engineer iterations (R8) and at phase boundaries (R3.1) -
            adapter-agnostic, but coarser: it cannot interrupt a call
            already in flight.
        read_only: #266 - the agent reads the tree it is judging and
            must not write it (the review and security roles). Applied
            by the claude-code, claude-sdk, and codex adapters and
            OVERRIDES ``sandbox`` there. A CustomAgent command has no
            generic sandbox surface, so - exactly as with ``sandbox`` -
            the setting is ignored and callers that need the guarantee
            must not use one.
        root_dir: The project root. Every agent process the adapter
            starts is recorded under this project's control directory
            until it is disposed of, so the next command can name one a
            killed kstrl left running (#642). Required, so no caller can
            leave it out by accident; None records nothing.
    """
    if agent_cmd:
        return CustomAgent(agent_cmd, root_dir=root_dir)
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
            read_only=read_only,
            root_dir=root_dir,
        )
    if agent_type == "claude-sdk":
        return ClaudeSdkAgent(
            model=model,
            effort=model_reasoning_effort,
            sandbox=sandbox,
            max_budget_usd=max_budget_usd,
            read_only=read_only,
            root_dir=root_dir,
        )
    if agent_type == "codex":
        return CodexAgent(
            model=model,
            reasoning_effort=model_reasoning_effort,
            sandbox=sandbox,
            read_only=read_only,
            root_dir=root_dir,
        )
    # Auto-detect: prefer claude-code, fall back to codex
    if agent_type is None or agent_type == "auto":
        if ClaudeCodeAgent.is_available():
            return ClaudeCodeAgent(
                model=model,
                effort=model_reasoning_effort,
                sandbox=sandbox,
                read_only=read_only,
                root_dir=root_dir,
            )
    return CodexAgent(
        model=model,
        reasoning_effort=model_reasoning_effort,
        sandbox=sandbox,
        read_only=read_only,
        root_dir=root_dir,
    )
