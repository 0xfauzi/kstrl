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

Measured again on 2026-10-09 (macOS, claude 2.1.291, codex-cli 0.156.1),
with kstrl's own adapters in a kstrl worktree (``<root>/.kstrl/worktrees/<id>``,
whose ``.git`` file points into ``<root>/.git/worktrees/<id>``); the
transcript is on #700:

- codex ``workspace-write`` holds every ``.git`` read-only, in the
  workspace and outside it, so ``git commit`` failed in a kstrl worktree
  (``<root>/.git/worktrees/<id>/index.lock``) and in a plain checkout
  (``<root>/.git/index.lock``). With ``sandbox_workspace_write.writable_roots``
  naming the paths of :func:`kstrl.git.git_write_paths`, and not a git dir,
  the commit succeeded in both layouts, and a write to ``hooks``, ``config``
  or ``commondir`` stayed denied. A writable git dir lets a sandboxed
  command write a hook, or a ``commondir`` that points git at a config of
  its own, and git obeys either outside every sandbox (measured).
- claude commits in both layouts with no extra path, and denies writes to
  ``<common>/hooks``, ``<common>/config`` and ``commondir``. No git path is
  added for it.
- On both, a check that writes outside the worktree (a tool cache in
  the home directory) failed with "Operation not permitted". The confirmed
  ``[stack]``'s ``writable`` paths, which the test zone grants the same
  commands, are passed as codex ``writable_roots`` and as claude
  ``sandbox.filesystem.allowWrite``; with them the check passed, and a
  write to ``$HOME`` and outbound network stayed denied.

- CustomAgent: an arbitrary operator-supplied shell command has no
  generic sandbox surface. A run that starts one still runs, and
  :func:`warn_unconfined` names the role in a warning (owner decision
  2026-10-09, #700; before it, #701 refused the run).

Default on (owner decision 2026-10-09, #700): the engineer runs in the
sandbox of its own harness. An operator who sets ``enabled = false`` gets
the same warning as a custom command. Outbound network is open by default
(owner decision 2026-10-09, #700): only the ``[stack]`` setup downloads
packages, so an engineer that adds a package fails with the network
denied. ``allow_network = false`` denies it.
Measured on 2026-10-09 with the same versions and layout:

- ``allow_network = true``: codex (``network_access=true``) and claude
  (``--dangerously-skip-permissions``, no permission rules) reached
  ``https://example.com`` (HTTP 200), committed, and were denied a shell
  write to ``$HOME`` ("Operation not permitted"). claude also refused a
  Bash call with ``dangerouslyDisableSandbox``.
- ``allow_network = false``: codex could not resolve the host and claude
  got "CONNECT tunnel failed, response 403".
- In both modes the claude Write tool wrote a file in ``$HOME``: the
  claude sandbox confines Bash, not the file tools. codex refused the
  same write ("patch rejected: writing outside of the project").
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.config_numbers import check_numbers
from kstrl.git import git_write_paths
from kstrl.jsonread import read_json

if TYPE_CHECKING:
    from kstrl.stack import Stack

# File-tool allow rules for the claude no-network mode: without
# --dangerously-skip-permissions these tools are permission-gated in
# headless mode (measured), and an engineer that cannot edit files is
# useless. Bash is deliberately absent - sandboxed Bash auto-runs
# (measured) and an explicit allow rule would also cover unsandboxed
# Bash requests. Network tools (WebFetch, WebSearch) are deliberately
# absent - this mode exists to deny network.
_CLAUDE_SANDBOXED_TOOL_ALLOW = [
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "LS",
    "TodoWrite",
]


@dataclass(frozen=True)
class SandboxConfig:
    """Operator sandbox intent, mapped per-CLI by the adapters.

    ``enabled`` turns OS-level sandboxing on (a shell command may write
    the agent's working tree on both CLIs; the claude file tools are not
    confined, measured); on by default (#700).
    ``allow_network`` keeps outbound network open inside the sandbox; on
    by default (#700), because an engineer that adds a package needs it.
    False denies it. ``writable`` is not a kstrl.toml key: it is the paths
    the confirmed ``[stack]`` grants its commands, set by
    :func:`with_stack_writable` for the roles that run those commands.
    """

    enabled: bool = True
    allow_network: bool = True
    writable: tuple[str, ...] = field(default=(), metadata={"provenance": True})

    @classmethod
    def from_env(cls) -> SandboxConfig:
        """Load sandbox config from environment variables only."""
        from kstrl.config import _parse_bool

        enabled = os.environ.get("KSTRL_SANDBOX_ENABLED")
        network = os.environ.get("KSTRL_SANDBOX_ALLOW_NETWORK")
        return cls(
            enabled=cls.enabled if enabled is None else _parse_bool(enabled),
            allow_network=cls.allow_network if network is None else _parse_bool(network),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> SandboxConfig:
        """Load sandbox config with precedence: env > toml > defaults.

        Reads the ``[sandbox]`` section from ``<root_dir>/kstrl.toml``.
        """
        from kstrl.config import _parse_bool, load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "sandbox")
        enabled = cls.enabled
        allow_network = cls.allow_network
        if "enabled" in section:
            enabled = bool(section["enabled"])
        if "allow_network" in section:
            allow_network = bool(section["allow_network"])
        if "KSTRL_SANDBOX_ENABLED" in os.environ:
            enabled = _parse_bool(os.environ.get("KSTRL_SANDBOX_ENABLED"))
        if "KSTRL_SANDBOX_ALLOW_NETWORK" in os.environ:
            allow_network = _parse_bool(os.environ.get("KSTRL_SANDBOX_ALLOW_NETWORK"))
        return check_numbers(cls(enabled=enabled, allow_network=allow_network))


def warn_unconfined(
    warn: Callable[[str], None], config: SandboxConfig, commands: Mapping[str, str | None]
) -> None:
    """Warn once for each role that runs with no sandbox (#700).

    ``commands`` maps each role a run starts that writes the tree to its
    custom agent command, or to None when the role runs on a CLI adapter.
    With the sandbox off every such role is named; with it on, each role
    on a custom command, which is an arbitrary shell command with no
    sandbox surface. Nothing is refused (owner decision 2026-10-09). The
    command itself is not printed: it can carry a credential.
    """
    if not config.enabled:
        lines = [f"the {role} runs with no sandbox: [sandbox] enabled = false" for role in commands]
    else:
        lines = [
            f"the {role} is a custom agent command, which runs with no sandbox. Run it on "
            "the claude-code, claude-sdk or codex adapter to confine it."
            for role, command in commands.items()
            if command
        ]
    for line in lines:
        warn(f"  {line}")


def with_stack_writable(
    config: SandboxConfig, root_dir: Path, stack: Stack | None
) -> SandboxConfig:
    """``config`` with the paths the confirmed ``stack`` grants its commands.

    The engineer runs the stack's checks in its own sandbox, so it gets
    the same ``writable`` paths the test zone gets (measured: a check that
    writes a tool cache in the home directory fails without them). An
    unconfirmed stack grants nothing.
    """
    from kstrl.stack import stack_paths

    if stack is None or stack.unconfirmed or not stack.writable:
        return config
    return replace(config, writable=tuple(str(p) for p in stack_paths(root_dir, stack.writable)))


def codex_sandbox_args(config: SandboxConfig | None, cwd: Path | None = None) -> list[str]:
    """``codex exec`` argv fragment for the operator's sandbox intent.

    ``network_access`` is ALWAYS passed explicitly when the sandbox is
    enabled: the operator's global ``~/.codex/config.toml`` may carry
    its own value, and the CLI ``-c`` override is the only way kstrl's
    per-project intent reliably wins (measured - see module docstring).
    ``writable_roots`` names the git paths a commit in ``cwd`` writes and
    ``config.writable``, because codex denies both (measured).
    """
    if config is None or not config.enabled:
        return []
    network = "true" if config.allow_network else "false"
    args = [
        "--sandbox",
        "workspace-write",
        "-c",
        f"sandbox_workspace_write.network_access={network}",
    ]
    roots = [*(str(path) for path in git_write_paths(cwd)), *config.writable]
    if roots:
        args += ["-c", f"sandbox_workspace_write.writable_roots={json.dumps(roots)}"]
    return args


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
    sandbox: dict[str, object] = {"enabled": True, "allowUnsandboxedCommands": False}
    if config.writable:
        sandbox["filesystem"] = {"allowWrite": list(config.writable)}
    settings: dict[str, object] = {"sandbox": sandbox}
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


# ---------------------------------------------------------------------------
# Reviewer sandbox (#266)
# ---------------------------------------------------------------------------
#
# Not an operator knob and deliberately not a field of SandboxConfig:
# the reviewer reads the worktree it is judging, so "may it write?" has
# exactly one defensible answer and no configuration surface. A reviewer
# that mutates the tree under review has changed the evidence.
#
# Measured on this machine, 2026-08-31 (codex-cli 0.150.1, claude 2.1.251),
# in a scratch git repo with a two-file change on a feature branch:
#
# - ``codex exec --sandbox read-only``: ran ``git diff main...HEAD
#   --numstat`` and reported the totals correctly (7 insertions, 0
#   deletions, 2 files); the write attempt was refused by the CLI itself
#   ("patch rejected: writing is blocked by read-only sandbox") and
#   ``git status --porcelain`` stayed empty. This is OS-level: it is the
#   same enforcement the workspace-write mode uses, with the workspace
#   moved out of the writable set.
#
# - ``claude --print`` with the settings payload below and WITHOUT
#   ``--dangerously-skip-permissions``: same git command, same correct
#   totals, and the write was refused ("file write operations require
#   explicit approval"), including the shell-redirection fallback,
#   leaving the tree clean. This is PERMISSION-layer, not OS-level, and
#   the difference is load-bearing: a control probe with ``deny`` on the
#   file tools but a blanket ``Bash`` allow DID write the file by shell
#   redirection. Breadth of the Bash allowance is what decides it, so
#   the allowance is an explicit list of read-only git verbs rather than
#   a bare "Bash". Anything outside the list is not approved, and a
#   headless run cannot prompt, so it is refused.
#
# Also measured: "MultiEdit" is not a known tool name in claude 2.1.251
# (the CLI warns "matches no known tool"), so it is absent below.

#: Read-only git verbs the reviewer is allowed to shell out to. Scoped
#: to plumbing that cannot mutate a repository: no ``add``, ``commit``,
#: ``checkout``, ``clean``, ``stash``, or ``restore``.
CLAUDE_REVIEW_GIT_COMMANDS: tuple[str, ...] = (
    "git diff",
    "git log",
    "git show",
    "git status",
    "git rev-parse",
    "git ls-files",
    "git blame",
    "git cat-file",
    "git describe",
)

#: Tools the reviewer must never use. WebFetch/WebSearch are here
#: because a reviewer with the diff and an outbound channel is an
#: exfiltration path.
CLAUDE_REVIEW_DENY_TOOLS: tuple[str, ...] = (
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)

#: Non-Bash tools the reviewer needs to read the worktree.
CLAUDE_REVIEW_ALLOW_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")


def codex_review_sandbox_args() -> list[str]:
    """``codex exec`` argv fragment for a read-only reviewer.

    ``read-only`` is one of the CLI's three sandbox policies and denies
    both writes and network to model-generated shell commands. No
    ``sandbox_workspace_write.*`` override is passed: that key governs
    the workspace-write policy only, and setting it here would be a
    no-op that reads like a guarantee.
    """
    return ["--sandbox", "read-only"]


def claude_review_sandbox_settings(config: SandboxConfig | None = None) -> str:
    """Claude settings JSON for a read-only reviewer.

    The operator's SANDBOX OBJECT is kept and their PERMISSIONS are
    replaced, which is where this differs from the codex mapping. There,
    ``--sandbox read-only`` is strictly tighter than ``--sandbox
    workspace-write`` and simply supersedes the whole thing. Here the
    two payloads are disjoint: the operator's carries the OS-level
    ``sandbox`` object (and ``allowUnsandboxedCommands: false``, the
    escape hatch this module always closes), which the reviewer wants,
    while their permission rules exist to re-allow the file tools an
    ENGINEER needs, which the reviewer must not have. Emitting only the
    reviewer's payload would drop OS-level sandboxing for the one role
    with the least business writing anything; merging the permissions
    leaked a file tool (see below).

    Paired with :func:`claude_review_drops_skip_permissions`: the
    permission rules are permission-layer, so they only bite when the
    adapter is NOT passing ``--dangerously-skip-permissions``.
    """
    base = claude_sandbox_settings(config)
    settings: dict[str, object] = read_json(base) if base else {}
    # The operator's SANDBOX object is kept (that is the OS-level
    # enforcement, and the reviewer wants it); the operator's
    # PERMISSIONS are REPLACED, never merged.
    #
    # Merging was wrong and the #266 review caught it: the operator's
    # allow list exists to re-allow the file tools an ENGINEER needs
    # under the no-network mode, and it names MultiEdit, which is not in
    # the reviewer's deny list. Subtracting a deny list from a merged
    # allow list leaks every tool nobody thought to deny - the failure
    # mode points the wrong way. Replacing means the reviewer holds
    # exactly the rules named below, and a tool added to the engineer's
    # list in future is excluded here until somebody considers it.
    settings["permissions"] = {
        "deny": list(CLAUDE_REVIEW_DENY_TOOLS),
        "allow": [
            *CLAUDE_REVIEW_ALLOW_TOOLS,
            *(f"Bash({verb}:*)" for verb in CLAUDE_REVIEW_GIT_COMMANDS),
        ],
    }
    return json.dumps(settings)


def claude_review_sandbox_args(config: SandboxConfig | None = None) -> list[str]:
    """``claude --print`` argv fragment for a read-only reviewer."""
    return ["--settings", claude_review_sandbox_settings(config)]
