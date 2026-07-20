"""OS-level sandbox pass-through for agent subprocesses (R7.5).

Worktree isolation bounds WHERE an agent's git-tracked changes land;
it does not bound what the agent's shell commands may read, write, or
reach over the network. This module carries the operator's sandbox
intent from config into the agent CLIs that support OS-level
enforcement.

Every mapping below is backed by a probe run on 2026-07-19 (macOS,
seatbelt; probe transcript in the R7.5 PR). Measured findings:

- codex CLI 0.134.0: ``codex exec --sandbox
  {read-only|workspace-write|danger-full-access}`` selects the policy.
  ``workspace-write`` denies writes outside the workspace (measured:
  ``touch $HOME/x`` -> "Operation not permitted"). Network inside it is
  governed by ``sandbox_workspace_write.network_access``, which MUST be
  passed explicitly in BOTH directions: the operator's global
  ``~/.codex/config.toml`` can set ``network_access = true`` and would
  otherwise silently win (measured on this machine). With the explicit
  ``=false`` override, DNS resolution itself is denied (curl exit 6).

- claude CLI 2.1.215: has NO ``--sandbox`` flag (measured: "error:
  unknown option '--sandbox'"). Sandboxing rides the ``--settings``
  flag with a top-level ``sandbox`` settings object, accepted inline on
  headless ``--print`` runs (measured). Write scoping is OS-enforced
  (measured: ``touch $HOME/x`` -> "Operation not permitted"). Network
  scoping is an ALLOWLIST GATE AT THE PERMISSION LAYER, which changes
  the invocation shape:
    - with ``--dangerously-skip-permissions``, domain approvals are
      auto-granted and the network stays OPEN (measured: curl 200);
    - without it, sandboxed Bash still auto-runs and non-allowlisted
      domains are hard-denied at the sandbox proxy (measured: curl
      "CONNECT tunnel failed, response 403"), but the FILE tools become
      permission-gated (measured: Write prompt, no file), so the
      settings JSON must carry explicit ``permissions.allow`` rules for
      them (measured: Write then succeeds).
  ``allowUnsandboxedCommands`` defaults to true (an escape hatch that
  reruns a failed command unsandboxed); it is always set to false here.

- CustomAgent: an arbitrary operator-supplied shell command has no
  generic sandbox surface; the config is IGNORED for custom agents and
  the factory says so loudly at startup.

Default off: sandboxing changes agent behavior (blocked network calls,
denied writes), so the operator opts in per project.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# File-tool allow rules for the claude no-network mode: without
# --dangerously-skip-permissions these tools are permission-gated in
# headless mode (measured), and an engineer that cannot edit files is
# useless. Bash is deliberately absent - sandboxed Bash auto-runs
# (measured) and an explicit allow rule would also cover unsandboxed
# Bash requests. Network tools (WebFetch, WebSearch) are deliberately
# absent - this mode exists to deny network.
_CLAUDE_SANDBOXED_TOOL_ALLOW = [
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "LS", "TodoWrite",
]


@dataclass(frozen=True)
class SandboxConfig:
    """Operator sandbox intent, mapped per-CLI by the adapters.

    ``enabled`` turns OS-level sandboxing on (write scope = the agent's
    working tree by construction on both CLIs). ``allow_network``
    re-opens outbound network inside the sandbox; off by default
    because a scoped-writes-but-open-network sandbox still exfiltrates.
    """

    enabled: bool = False
    allow_network: bool = False

    @classmethod
    def from_env(cls) -> SandboxConfig:
        """Load sandbox config from environment variables only."""
        from ralph_py.config import _parse_bool

        return cls(
            enabled=_parse_bool(os.environ.get("RALPH_SANDBOX_ENABLED")),
            allow_network=_parse_bool(
                os.environ.get("RALPH_SANDBOX_ALLOW_NETWORK")
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> SandboxConfig:
        """Load sandbox config with precedence: env > toml > defaults.

        Reads the ``[sandbox]`` section from ``<root_dir>/ralph.toml``.
        """
        from ralph_py.config import _parse_bool, load_toml_section

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(root_dir / "ralph.toml", "sandbox")
        enabled = cls.enabled
        allow_network = cls.allow_network
        if "enabled" in section:
            enabled = bool(section["enabled"])
        if "allow_network" in section:
            allow_network = bool(section["allow_network"])
        if "RALPH_SANDBOX_ENABLED" in os.environ:
            enabled = _parse_bool(os.environ.get("RALPH_SANDBOX_ENABLED"))
        if "RALPH_SANDBOX_ALLOW_NETWORK" in os.environ:
            allow_network = _parse_bool(
                os.environ.get("RALPH_SANDBOX_ALLOW_NETWORK")
            )
        return cls(enabled=enabled, allow_network=allow_network)


def codex_sandbox_args(config: SandboxConfig | None) -> list[str]:
    """``codex exec`` argv fragment for the operator's sandbox intent.

    ``network_access`` is ALWAYS passed explicitly when the sandbox is
    enabled: the operator's global ``~/.codex/config.toml`` may carry
    its own value, and the CLI ``-c`` override is the only way Ralph's
    per-project intent reliably wins (measured - see module docstring).
    """
    if config is None or not config.enabled:
        return []
    network = "true" if config.allow_network else "false"
    return [
        "--sandbox", "workspace-write",
        "-c", f"sandbox_workspace_write.network_access={network}",
    ]


def claude_sandbox_settings(config: SandboxConfig | None) -> str | None:
    """Claude settings JSON payload for the operator's sandbox intent.

    The single source of the payload for BOTH invocation surfaces: the
    CLI adapter passes it via ``--settings`` (R7.5, measured) and the
    claude-sdk adapter passes the same string via
    ``ClaudeAgentOptions.settings`` (R7.6) - the SDK forwards it to the
    same CLI flag, so the two paths cannot drift.

    Always sets ``allowUnsandboxedCommands: false`` (the default true
    would let a failed command re-run OUTSIDE the sandbox). In the
    no-network mode the JSON additionally carries the file-tool
    permission allow rules the headless run needs once
    ``--dangerously-skip-permissions`` is dropped (see
    :func:`claude_sandbox_drops_skip_permissions`).
    """
    if config is None or not config.enabled:
        return None
    settings: dict[str, object] = {
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
        },
    }
    if not config.allow_network:
        settings["permissions"] = {
            "allow": list(_CLAUDE_SANDBOXED_TOOL_ALLOW),
        }
    return json.dumps(settings)


def claude_sandbox_args(config: SandboxConfig | None) -> list[str]:
    """``claude --print`` argv fragment for the operator's sandbox intent.

    Thin argv wrapper over :func:`claude_sandbox_settings`.
    """
    settings = claude_sandbox_settings(config)
    if settings is None:
        return []
    return ["--settings", settings]


def claude_sandbox_drops_skip_permissions(
    config: SandboxConfig | None,
) -> bool:
    """Whether the adapter must drop ``--dangerously-skip-permissions``.

    Claude's domain allowlist is enforced at the permission layer:
    with skip-permissions every domain is auto-approved and the network
    stays open (measured); without it, non-allowlisted domains are
    hard-denied at the sandbox proxy while sandboxed Bash still
    auto-runs (measured). So denying network REQUIRES dropping the
    flag; allowing network keeps it (the pre-R7.5 invocation shape).
    """
    return config is not None and config.enabled and not config.allow_network
