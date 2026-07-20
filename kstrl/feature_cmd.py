"""The feature flow: understand -> review gate -> implement -> repairs.

Mechanical extraction from cli.feature (TUI surface C2): the click
shell resolves configs/paths/validation and builds FeatureParams; this
module runs the flow and RETURNS the exit code (no sys.exit - the flow
must be hostable on a worker thread). Narration is byte-identical to
the pre-extraction command.

The review gate goes through the interaction seam: the terminal wires
UiInteractionChannel (unchanged semantics incl. the non-TTY
"Interactive review required" refusal); the embedded TUI (C3) passes
its queue channel so the gate opens as a modal.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.agents import get_agent
from kstrl.agents.logging import LoggingAgent
from kstrl.breaker import BreakerConfig
from kstrl.interaction import (
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.loop import run_loop
from kstrl.timeout import TimeoutConfig

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.config import KstrlConfig
    from kstrl.prd import PRD
    from kstrl.sandbox import SandboxConfig
    from kstrl.ui.base import UI


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class FeatureParams:
    """The feature command's resolved knobs (CLI/env/toml already
    collapsed by the shell; None = "not overridden")."""

    prd_path: Path
    prd_doc: PRD
    feature_name: str
    feature_dir: Path
    feature_understand: Path
    log_dir: Path
    understand_iterations: int
    understand_prompt_file: Path | None
    implementation_auto_run: bool
    repair_max_runs: int
    repair_iterations: int
    repair_agent_cmd: str | None
    branch_override: str | None
    allowed_paths_override: list[str] | None
    sandbox: SandboxConfig


def _log_path(params: FeatureParams, label: str, attempt: int | None = None) -> Path:
    stamp = _timestamp()
    if attempt is None:
        name = f"{label}_{stamp}.log"
    else:
        name = f"{label}_{attempt:02d}_{stamp}.log"
    return params.log_dir / name


def _build_repair_prd(
    params: FeatureParams, root_dir: Path, log_file: Path, attempt: int,
) -> Path:
    repair_dir = params.feature_dir / "repairs"
    repair_dir.mkdir(parents=True, exist_ok=True)
    repair_path = repair_dir / f"repair_{_timestamp()}.json"
    latest_path = repair_dir / "latest.json"

    verification: list[str] = []
    seen: set[str] = set()
    for story in params.prd_doc.user_stories:
        for item in story.acceptance_criteria:
            lower = item.lower()
            has_check = "typecheck" in lower or "tests" in lower or "lint" in lower
            if has_check and "pass" in lower:
                if item not in seen:
                    seen.add(item)
                    verification.append(item)

    try:
        rel_log = log_file.relative_to(root_dir)
        log_ref = rel_log.as_posix()
    except ValueError:
        log_ref = str(log_file)

    criteria = [f"Repair failures reported in {log_ref}"]
    criteria.extend(verification)

    repair_story = {
        "id": f"REPAIR-{attempt:02d}",
        "title": "Repair failures from last run",
        "acceptanceCriteria": criteria,
        "priority": 1,
        "passes": False,
        "notes": f"Original PRD: {params.prd_path}",
    }
    repair_doc = {
        "branchName": params.prd_doc.branch_name,
        "userStories": [repair_story],
    }
    with open(repair_path, "w") as handle:
        json.dump(repair_doc, handle, indent=2)
        handle.write("\n")
    with open(latest_path, "w") as handle:
        json.dump(repair_doc, handle, indent=2)
        handle.write("\n")

    return repair_path


def run_feature(
    params: FeatureParams,
    base_config: KstrlConfig,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    *,
    interaction: InteractionChannel | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """Understand -> review gate -> implement -> repair loop.

    Returns the flow's exit code. ``interaction`` defaults to the
    terminal channel; ``stop_check`` threads into every run_loop.
    """
    # Feature understanding phase
    understand_config = copy.deepcopy(base_config)
    understand_config.max_iterations = params.understand_iterations
    if params.understand_prompt_file is not None:
        understand_config.prompt_file = params.understand_prompt_file
    understand_config.prd_file = params.prd_path
    rel_feature_understand = (
        params.feature_understand.relative_to(root_dir).as_posix()
    )
    understand_config.allowed_paths = [rel_feature_understand]
    if params.branch_override is not None:
        understand_config.kstrl_branch = params.branch_override
        understand_config.kstrl_branch_explicit = True

    timeouts = TimeoutConfig.load(root_dir)
    breaker_config = BreakerConfig.load(root_dir)

    understand_log = _log_path(params, "understand")
    understand_agent = LoggingAgent(agent, understand_log)
    understand_result = run_loop(
        understand_config, ui, understand_agent, root_dir,
        timeouts=timeouts, breaker_config=breaker_config,
        interaction=interaction, stop_check=stop_check,
    )
    if not understand_result.completed:
        return understand_result.exit_code

    # Review gate
    ui.section("Feature understand review")
    ui.kv("Understand file", str(params.feature_understand))
    if params.implementation_auto_run:
        ui.info("IMPLEMENTATION_AUTO_RUN enabled: skipping review gate")
    else:
        channel = (
            interaction if interaction is not None
            else UiInteractionChannel(ui)
        )
        if not channel.can_prompt():
            ui.err(
                "Interactive review required. Re-run with --implementation-auto-run."
            )
            return 2

        response = channel.request(PromptRequest(
            kind=PromptKind.CONFIRM,
            header="Review the understand file and confirm implementation start:",
            options=("Start implementation", "Quit to amend"),
            default=0,
        ))
        if not response.answered or response.choice != 0:
            ui.info("Amend the understand file and re-run `ralph feature`.")
            return 0

    # Implementation phase
    run_config = copy.deepcopy(base_config)
    run_config.prd_file = params.prd_path
    run_config.max_iterations = len(params.prd_doc.user_stories)
    if run_config.max_iterations == 0:
        ui.warn("PRD has no user stories. Skipping implementation.")
        return 0
    run_config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
    if params.allowed_paths_override is not None:
        run_config.allowed_paths = params.allowed_paths_override
    if params.branch_override is not None:
        run_config.kstrl_branch = params.branch_override
        run_config.kstrl_branch_explicit = True

    run_log = _log_path(params, "run")
    run_agent = LoggingAgent(agent, run_log)
    result = run_loop(
        run_config, ui, run_agent, root_dir,
        timeouts=timeouts, breaker_config=breaker_config,
        interaction=interaction, stop_check=stop_check,
    )
    if (
        result.exit_code == 0
        or params.repair_max_runs == 0
        or result.iterations == 0
    ):
        return result.exit_code

    last_log = run_log
    repair_result = result
    for attempt in range(1, params.repair_max_runs + 1):
        repair_prd = _build_repair_prd(params, root_dir, last_log, attempt)
        repair_config = copy.deepcopy(base_config)
        repair_config.prd_file = repair_prd
        repair_config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
        repair_config.max_iterations = params.repair_iterations
        if params.allowed_paths_override is not None:
            repair_config.allowed_paths = params.allowed_paths_override
        repair_config.kstrl_branch = ""
        repair_config.kstrl_branch_explicit = True

        repair_log = _log_path(params, "repair", attempt)
        repair_agent_base = get_agent(
            params.repair_agent_cmd or base_config.agent_cmd,
            base_config.model,
            base_config.model_reasoning_effort,
            base_config.agent_type,
            sandbox=params.sandbox,
        )
        repair_agent = LoggingAgent(repair_agent_base, repair_log)
        repair_result = run_loop(
            repair_config, ui, repair_agent, root_dir,
            timeouts=timeouts, breaker_config=breaker_config,
            interaction=interaction, stop_check=stop_check,
        )
        if repair_result.exit_code == 0:
            return 0
        last_log = repair_log

    return repair_result.exit_code
