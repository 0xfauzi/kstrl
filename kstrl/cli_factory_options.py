"""The 41 options of ``ks factory``, applied as one decorator."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from kstrl.cli_preflight import _AgentTypeChoice, _reject_blank_project_name
from kstrl.cli_seam import _PROGRESS_LOG_HELP
from kstrl.config import LIMIT_FLOAT, LIMIT_INT
from kstrl.config_report import UI_MODES
from kstrl.contract import ContractMode
from kstrl.factory_config import VALID_REVIEW_MODES
from kstrl.security import SEVERITY_ORDER, SecurityMode

_FACTORY_OPTIONS: tuple[Callable[[Callable[..., Any]], Callable[..., Any]], ...] = (
    click.option(
        "--spec",
        type=click.Path(exists=True, path_type=Path),
        help=("Markdown spec file or SpecKit artifact directory (runs decompose first)"),
    ),
    click.option(
        "--manifest",
        "manifest_path",
        type=click.Path(exists=True, path_type=Path),
        help="Existing manifest file to build from instead of decomposing a spec "
        "(no default: pass this or --spec)",
    ),
    click.option(
        "--root",
        type=click.Path(path_type=Path),
        help="Project root path (defaults to current directory)",
    ),
    click.option(
        "--project-name",
        callback=_reject_blank_project_name,
        help="Name for this factory project (required with --spec)",
    ),
    click.option(
        "--base-branch",
        default=None,
        help="Base git branch (default: auto-detected from the repository)",
    ),
    click.option(
        "--single-pr",
        is_flag=True,
        help="Use a single branch/PR for all components",
    ),
    click.option(
        "--max-parallel",
        type=LIMIT_INT,
        default=None,
        help="Maximum parallel components (default: 4)",
    ),
    click.option(
        "--max-retries",
        type=LIMIT_INT,
        default=None,
        help="Maximum retries per component (default: 3)",
    ),
    click.option(
        "--create-prs/--no-prs",
        default=None,
        help="Create PRs for completed components (default: on)",
    ),
    click.option(
        "--no-verify",
        is_flag=True,
        help="Skip Phase 1 mechanical verification",
    ),
    click.option(
        "--accept-red-base",
        metavar="SHA",
        default="",
        help="Run on a base branch whose gates fail, for this run only: at least 12 "
        "characters of the base commit's sha. A base that moved since refuses again; "
        "`ks retry` replays it",
    ),
    click.option(
        "--acceptance",
        metavar="DIR",
        default="",
        help="A directory outside the repository holding plan.json: acceptance checks run "
        "on the base and on each component's head inside the isolation rung. A failed "
        "held-out check halts the component; any other failed check is retried. Needs a "
        "[stack]; `ks retry` replays it",
    ),
    click.option(
        "--design-acceptance",
        is_flag=True,
        default=False,
        help="Ask the verification designer (the [review] agent) for each component's "
        "acceptance checks before the plan gate, then run them as --acceptance does: they "
        "gate the run as an operator's checks do. Needs a [stack]; `ks retry` replays it",
    ),
    click.option(
        "--bug-report",
        is_flag=True,
        default=False,
        help="The run fixes a reported bug (`ks serve` passes it for an issue with the bug "
        "label). When no acceptance check that the base keeps fails on the base, the run is "
        "refused before any engineer and an inbox item names the checks; approving it runs "
        "the bug without a reproduction. Needs --acceptance or --design-acceptance; "
        "`ks retry` replays it",
    ),
    click.option(
        "--review-mode",
        type=click.Choice(VALID_REVIEW_MODES),
        default=None,
        help="Phase 2 review: hard (block), advisory (warn), skip (default: hard)",
    ),
    click.option(
        "--review-agent-cmd",
        help="Custom agent for reviewer (default: same as implementation agent)",
    ),
    click.option(
        "--review-model",
        help="Model for reviewer agent",
    ),
    click.option(
        "--security-mode",
        type=click.Choice([mode.value for mode in SecurityMode]),
        default=None,
        help="Phase 2.5 security review: hard (block on findings at or above "
        "--security-fail-threshold), "
        "advisory (warn only, default), skip (turn the review off)",
    ),
    click.option(
        "--security-agent-cmd",
        help="Custom agent for security reviewer (default: same as implementation agent)",
    ),
    click.option(
        "--security-model",
        help="Model for security reviewer agent (default: same as implementation agent)",
    ),
    click.option(
        "--security-fail-threshold",
        type=click.Choice(list(SEVERITY_ORDER)),
        default=None,
        help="In hard mode, findings at or above this severity block "
        "(default: high - critical+high fail)",
    ),
    click.option(
        "--contract-check",
        type=click.Choice([mode.value for mode in ContractMode]),
        default=None,
        help="Phase 3 contract testing: tier (per-tier), final (end-only), skip (default: tier)",
    ),
    click.option(
        "--agent-timeout",
        type=LIMIT_FLOAT,
        default=None,
        help="Timeout per agent iteration in seconds; 0 disables "
        "(default: no limit, or KSTRL_TIMEOUT_AGENT_ITERATION / "
        "[timeout].agent_iteration in kstrl.toml)",
    ),
    click.option(
        "--component-timeout",
        type=LIMIT_FLOAT,
        default=None,
        help="Timeout per component total in seconds; 0 disables "
        "(default: no limit, or KSTRL_TIMEOUT_COMPONENT / "
        "[timeout].component_total in kstrl.toml)",
    ),
    click.option(
        "--max-adversarial-calls",
        type=LIMIT_INT,
        default=None,
        help="Hard cap on adversarial LLM calls (review + security + "
        "distill) per run; 0 = unbounded (default: 0, or "
        "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS / "
        "[factory].max_adversarial_calls in kstrl.toml)",
    ),
    click.option(
        "--max-total-tokens",
        type=LIMIT_INT,
        default=None,
        help="Run-level token budget across ALL phases (engineer + review "
        "+ security + distill); 0 = unbounded. Counts CACHE READS at "
        "par, so it is a poor proxy for spend - prefer --max-cost-usd. "
        "On breach the current component fails with a synthetic budget "
        "finding and pending components halt (default: 0, or "
        "KSTRL_FACTORY_MAX_TOTAL_TOKENS "
        "/ [factory].max_total_tokens in kstrl.toml)",
    ),
    click.option(
        "--max-cost-usd",
        type=LIMIT_FLOAT,
        default=None,
        help="Run-level USD budget across ALL phases; 0 = unbounded. "
        "Checked between engineer iterations and at phase boundaries, "
        "so it is NOT a hard cap: the iteration already in flight is "
        "unbounded. Distinct from [agent] budget_usd, which is "
        "adapter-internal (claude-sdk only). Default: 0, or "
        "KSTRL_FACTORY_MAX_COST_USD / [factory].max_cost_usd in "
        "kstrl.toml",
    ),
    click.option(
        "--pause-before-pr-merge/--no-pause-before-pr-merge",
        default=None,
        help="Pause for human approval before each component's PR "
        "push+merge (default: off, or "
        "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE / "
        "[factory].pause_before_pr_merge in kstrl.toml)",
    ),
    click.option(
        "--progress-log",
        type=click.Path(path_type=Path),
        help=_PROGRESS_LOG_HELP,
    ),
    click.option(
        "--no-worktrees",
        is_flag=True,
        help="Disable git worktrees (forces sequential execution)",
    ),
    click.option(
        "--keep-worktrees-on-failure",
        is_flag=True,
        help="Keep a failed component's worktree for post-mortem instead of "
        "removing it at cleanup; the failure summary points at it "
        "(default: off, or KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
        "[factory].keep_worktrees_on_failure in kstrl.toml)",
    ),
    click.option(
        "--force-lock",
        is_flag=True,
        help="Proceed even if another kstrl invocation holds "
        ".kstrl/factory.lock (may corrupt the other run's state)",
    ),
    click.option(
        "--yes",
        "-y",
        is_flag=True,
        help="Skip confirmation prompt",
    ),
    click.option(
        "--agent-cmd",
        help="Custom agent command (prompt piped to stdin)",
    ),
    click.option(
        "--model",
        "-m",
        help="Model for the agent",
    ),
    click.option(
        "--reasoning",
        help="Reasoning effort (low, medium, high, max)",
    ),
    click.option(
        "--agent-type",
        type=_AgentTypeChoice(),
        default="auto",
        help="Agent type",
    ),
    click.option(
        "--sleep",
        "-s",
        type=LIMIT_FLOAT,
        default=2.0,
        help="Sleep seconds between iterations",
    ),
    click.option(
        "--ui",
        type=click.Choice(UI_MODES),
        default="auto",
        help="UI mode",
    ),
    click.option(
        "--no-color",
        is_flag=True,
        help="Disable colors",
    ),
)


def factory_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Apply the options of ``ks factory`` in their declared order."""
    for option in reversed(_FACTORY_OPTIONS):
        func = option(func)
    return func
