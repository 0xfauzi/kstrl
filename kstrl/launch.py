"""Launch specs + factory config assembly (TUI surface B1).

The home shell's launcher forms produce a LaunchSpec - the HEADLINE
options only; everything else resolves env > kstrl.toml > defaults,
i.e. "the CLI invoked with just these flags". The session layer (D6)
maps each spec onto its command core.

``assemble_factory_configs`` is the extracted mirror-of-`ks factory`-
with-no-flags block that cli.retry grew; retry and the launcher share
it so the two paths can never drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig
from kstrl.timeout import TimeoutConfig


@dataclass(frozen=True)
class FactoryLaunch:
    """`ks factory` from an existing manifest (or a spec to decompose
    first - the D6 session layer chains DecomposeLaunch for that)."""

    manifest_path: Path | None = None
    max_parallel: int | None = None
    review_mode: str | None = None


@dataclass(frozen=True)
class DecomposeLaunch:
    spec_path: Path = Path("scripts/kstrl/spec.md")
    project_name: str = ""
    base_branch: str = "main"
    single_pr: bool = False


@dataclass(frozen=True)
class FeatureLaunch:
    prd_path: Path = Path("scripts/kstrl/prd.json")
    iterations: int | None = None


@dataclass(frozen=True)
class LoopLaunch:
    """`ks run` / `ks understand` - the single-loop commands."""

    command: str = "run"  # run | understand
    iterations: int | None = None


LaunchSpec = FactoryLaunch | DecomposeLaunch | FeatureLaunch | LoopLaunch


def assemble_factory_configs(
    root_dir: Path,
    *,
    single_pr: bool | None = None,
    progress_log_path: Path | None = None,
    force_lock: bool = False,
    keep_worktrees_on_failure: bool = False,
) -> tuple[FactoryConfig, KstrlConfig]:
    """Config assembly mirroring `ks factory` with no flags: every
    phase config resolves env > kstrl.toml > defaults (R2.1 control
    plane). The base config is forced plain - factory narration goes
    through the caller's console/bridge, never a nested rich UI.
    """
    from kstrl.contract import ContractConfig
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.security import SecurityConfig
    from kstrl.verify import VerifyConfig

    base_config = KstrlConfig.load(root_dir)
    base_config.ui_mode = "plain"
    base_config.no_color = True
    if not base_config.prompt_file.exists():
        default_prompt = root_dir / "scripts" / "kstrl" / "prompt.md"
        if default_prompt.exists():
            base_config.prompt_file = default_prompt

    factory_config = FactoryConfig.load(root_dir)
    if single_pr is not None:
        factory_config.single_pr = single_pr
    factory_config.verify_config = VerifyConfig.load(root_dir)
    factory_config.security_config = SecurityConfig.load(root_dir)
    contract_resolved = ContractConfig.load(root_dir)
    factory_config.contract_config = (
        contract_resolved if contract_resolved.mode != "skip" else None
    )
    factory_config.feedforward_config = FeedforwardConfig.load(root_dir)
    factory_config.timeout_config = TimeoutConfig.load(root_dir)
    factory_config.progress_log_path = progress_log_path
    factory_config.force_lock = force_lock
    if keep_worktrees_on_failure:
        factory_config.keep_worktrees_on_failure = True
    return factory_config, base_config
