"""SDK runner subprocess for the claude-sdk adapter (R7.6).

Executed as ``python -m ralph_py.agents.sdk_runner`` by
:class:`~ralph_py.agents.claude_sdk.ClaudeSdkAgent` through the
R0.1-proven DeadlineStreamer. This process is the session leader; the
claude CLI the SDK spawns (and every tool process under it) is a
grandchild in this session, so the adapter's deadline breach
group-kills the whole tree.

Contract (stdin): ONE JSON document::

    {"prompt": str, "model": str|null, "effort": str|null,
     "settings": str|null, "bypass_permissions": bool,
     "max_budget_usd": float|null, "workspace_guard": bool,
     "cwd": str|null, "cli_path": str|null}

Contract (stdout): human-readable display lines, plus at most one of
each prefixed record line (see claude_sdk.USAGE_PREFIX/RESULT_PREFIX),
every line flushed as it is produced. Typed SDK failures surface as
``ERROR: ...`` display lines plus an is_error RESULT record - the
adapter never sees an unexplained silent exit.

Module import is stdlib-only on purpose: the SDK is an optional
dependency, and a missing package must produce a clear error line, not
an ImportError traceback.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from ralph_py.agents.claude_sdk import RESULT_PREFIX, USAGE_PREFIX

# Runner exit codes (informational; the adapter keys on output lines).
_EXIT_OK = 0
_EXIT_BAD_CONFIG = 2
_EXIT_SDK_MISSING = 3
_EXIT_SDK_ERROR = 4

# File tools whose target path the workspace guard checks. Bash is
# deliberately absent: parsing shell to find write targets is
# false-negative-prone, and Bash writes are already bounded by worktree
# isolation + the R7.5 OS sandbox + mechanical diff-scope verification.
_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_PATH_KEYS = ("file_path", "notebook_path")


def _emit(line: str) -> None:
    print(line, flush=True)


def _emit_result(payload: dict[str, Any]) -> None:
    _emit(RESULT_PREFIX + json.dumps(payload))


def _read_config() -> dict[str, Any]:
    raw = sys.stdin.read()
    config = json.loads(raw)
    if not isinstance(config, dict) or "prompt" not in config:
        raise ValueError("runner config must be a JSON object with 'prompt'")
    return config


def _workspace_root(config: dict[str, Any]) -> Path:
    cwd = config.get("cwd")
    root = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())
    return Path(os.path.realpath(root))


def _path_escapes_workspace(raw_path: str, workspace: Path) -> bool:
    """True when the tool's target path resolves outside the workspace.

    ``realpath`` (not ``resolve(strict=False)`` alone) so a symlink
    inside the workspace pointing out of it cannot smuggle the write.
    """
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = Path(os.path.realpath(candidate))
    return not resolved.is_relative_to(workspace)


def _make_workspace_guard(workspace: Path) -> Any:
    """PreToolUse hook denying file tools that target paths outside
    ``workspace`` (the spike's measured win: prevention, not detection;
    the denial is recorded by the CLI in permission_denials)."""

    async def guard(
        hook_input: Any, _tool_use_id: str | None, _context: Any,
    ) -> dict[str, Any]:
        tool_name = str(hook_input.get("tool_name", ""))
        tool_input = hook_input.get("tool_input")
        if tool_name in _GUARDED_TOOLS and isinstance(tool_input, dict):
            for key in _PATH_KEYS:
                raw = tool_input.get(key)
                if isinstance(raw, str) and raw and _path_escapes_workspace(
                    raw, workspace,
                ):
                    _emit(
                        f"[workspace-guard] denied {tool_name} "
                        f"outside workspace: {raw}"
                    )
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"{key}={raw} is outside the run workspace "
                                f"{workspace}; write within the workspace."
                            ),
                        },
                    }
        return {}

    return guard


def _render_content_blocks(blocks: Any, sdk: Any) -> None:
    """Mirror the claude_code adapter's display conventions."""
    from ralph_py.agents.claude_code import _format_tool_use

    for block in blocks:
        if isinstance(block, sdk.TextBlock):
            text = block.text.strip()
            if text:
                _emit(text)
        elif isinstance(block, sdk.ToolUseBlock):
            tool_input = block.input if isinstance(block.input, dict) else {}
            for line in _format_tool_use(block.name, tool_input):
                _emit(line)
        elif isinstance(block, sdk.ToolResultBlock):
            content = block.content
            if isinstance(content, str) and content.strip():
                text = content[:200] + "..." if len(content) > 200 else content
                _emit(text)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = str(item.get("text", ""))
                        if len(text) > 200:
                            text = text[:200] + "..."
                        if text.strip():
                            _emit(text)


def _emit_result_message(message: Any) -> None:
    """Emit the typed ResultMessage as the two contract records."""
    usage = message.usage if isinstance(message.usage, dict) else {}
    _emit(USAGE_PREFIX + json.dumps({
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "cost_usd": message.total_cost_usd,
        "duration_ms": message.duration_ms,
    }))
    denials = message.permission_denials
    _emit_result({
        "subtype": message.subtype,
        "is_error": message.is_error,
        "errors": message.errors,
        "num_turns": message.num_turns,
        "result": message.result,
        "permission_denials": len(denials) if isinstance(denials, list) else 0,
    })


async def _drive(config: dict[str, Any], sdk: Any) -> int:
    """Stream one SDK query, rendering messages as display lines."""
    workspace = _workspace_root(config)
    options_kwargs: dict[str, Any] = {
        "cwd": config.get("cwd") or None,
        "model": config.get("model") or None,
        "effort": config.get("effort") or None,
        # SDK default system_prompt=None maps to an EMPTY system prompt
        # (measured: _build_command passes --system-prompt ""). Engineer
        # parity with the CLI adapter requires the claude_code preset.
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "settings": config.get("settings") or None,
        "max_budget_usd": config.get("max_budget_usd"),
        "cli_path": config.get("cli_path") or None,
    }
    # Mirror of the CLI adapter's R7.5 invocation shape: skip-permissions
    # unless the no-network sandbox requires the permission layer.
    if config.get("bypass_permissions", True):
        options_kwargs["permission_mode"] = "bypassPermissions"
    if config.get("workspace_guard", True):
        options_kwargs["hooks"] = {
            "PreToolUse": [sdk.HookMatcher(
                matcher="|".join(_GUARDED_TOOLS),
                hooks=[_make_workspace_guard(workspace)],
            )],
        }

    options = sdk.ClaudeAgentOptions(**options_kwargs)
    saw_result = False
    try:
        async for message in sdk.query(
            prompt=str(config["prompt"]), options=options,
        ):
            if isinstance(message, sdk.ResultMessage):
                saw_result = True
                _emit_result_message(message)
            else:
                _render_message(message, sdk)
    except Exception as exc:  # noqa: BLE001 - see below
        # The SDK raises a PLAIN Exception (not ClaudeSDKError) for
        # error results in the stream - measured 2026-07-20 on 0.2.123:
        # a max_budget_usd breach surfaces as
        # ``Exception: Claude Code returned an error result: Reached
        # maximum budget ($...)`` from query.receive_messages. The
        # usage/result records already emitted (if any) stand; this
        # converts the would-be traceback into the designed error
        # surface.
        _emit(f"ERROR: {type(exc).__name__}: {exc}")
        if not saw_result:
            _emit_result({
                "subtype": type(exc).__name__,
                "is_error": True,
                "errors": [str(exc)],
                "num_turns": 0,
                "result": None,
                "permission_denials": 0,
            })
        return _EXIT_SDK_ERROR
    if not saw_result:
        _emit("ERROR: SDK stream ended without a result message")
        _emit_result({
            "subtype": "missing_result",
            "is_error": True,
            "errors": ["stream ended without ResultMessage"],
            "num_turns": 0,
            "result": None,
            "permission_denials": 0,
        })
        return _EXIT_SDK_ERROR
    return _EXIT_OK


def _render_message(message: Any, sdk: Any) -> None:
    """Render one non-result SDK message as display lines."""
    if isinstance(message, (sdk.AssistantMessage, sdk.UserMessage)):
        _render_content_blocks(message.content, sdk)
    elif isinstance(message, sdk.RateLimitEvent):
        info = getattr(message, "rate_limit_info", None)
        _emit(f"[rate-limit] {info}")
    # SystemMessage / StreamEvent: init noise, skipped like the CLI
    # adapter skips non-assistant stream events.


def main() -> int:
    try:
        config = _read_config()
    except (json.JSONDecodeError, ValueError) as exc:
        _emit(f"ERROR: invalid sdk-runner config on stdin: {exc}")
        return _EXIT_BAD_CONFIG

    try:
        import claude_agent_sdk as sdk
    except ImportError:
        _emit(
            "ERROR: claude-agent-sdk is not installed; "
            "install the sdk extra (uv sync --extra sdk)"
        )
        _emit_result({
            "subtype": "sdk_not_installed",
            "is_error": True,
            "errors": ["claude-agent-sdk not installed"],
            "num_turns": 0,
            "result": None,
            "permission_denials": 0,
        })
        return _EXIT_SDK_MISSING

    try:
        return asyncio.run(_drive(config, sdk))
    except Exception as exc:  # noqa: BLE001
        # Failures outside _drive's own stream handling (options
        # construction, event-loop teardown). Typed SDK errors
        # (CLINotFoundError, CLIConnectionError, ProcessError,
        # CLIJSONDecodeError) land here too when raised pre-stream; the
        # broad catch exists because the SDK ALSO raises plain
        # Exception for in-stream error results (measured, 0.2.123) -
        # a traceback on stdout is never an acceptable surface.
        _emit(f"ERROR: {type(exc).__name__}: {exc}")
        _emit_result({
            "subtype": type(exc).__name__,
            "is_error": True,
            "errors": [str(exc)],
            "num_turns": 0,
            "result": None,
            "permission_denials": 0,
        })
        return _EXIT_SDK_ERROR


if __name__ == "__main__":
    sys.exit(main())
