"""The `ks serve` daemon.

Admission, the GitHub intake, the child runs, the settlement, the spend, the
signals and the CI state.
"""

from __future__ import annotations

from kstrl.serve.ci_state import CiState, poll_ci, read_ci_ledger, recorded_merges
from kstrl.serve.intake_gh import INTAKE_GITHUB_SECTION, GitHubIntakeConfig, IntakeError
from kstrl.serve.intake_github import sync
from kstrl.serve.serve import ServeLockedError
from kstrl.serve.serve import serve as run_serve
from kstrl.serve.serve_config import SERVE_SECTION, ServeConfig, ServeError
from kstrl.serve.serve_gates import (
    check_budget,
    check_cost_coverage,
    check_inbox_cap,
    check_parked_merges,
    check_poison_breaker,
    consecutive_poison_count,
    factory_lock_held,
)
from kstrl.serve.serve_github import check_open_pr_bound
from kstrl.serve.serve_launchd import (
    LAUNCHD_MODES,
    REQUIRE_TIMEOUT_ENV,
    launchd_log_dir,
    render_launchd_plist,
)
from kstrl.serve.serve_settle import settle_approval_run
from kstrl.serve.serve_spend import (
    ARCHITECT_RUN_KIND,
    ServeStateError,
    SpendLedger,
    owned_run_spend,
    run_dir_names,
)
from kstrl.serve.signals import SIGNALS_SECTION, SignalsConfig, SignalsError, poll, read_ledger

__all__ = [
    "ARCHITECT_RUN_KIND",
    "CiState",
    "GitHubIntakeConfig",
    "INTAKE_GITHUB_SECTION",
    "IntakeError",
    "LAUNCHD_MODES",
    "REQUIRE_TIMEOUT_ENV",
    "SERVE_SECTION",
    "SIGNALS_SECTION",
    "ServeConfig",
    "ServeError",
    "ServeLockedError",
    "ServeStateError",
    "SignalsConfig",
    "SignalsError",
    "SpendLedger",
    "check_budget",
    "check_cost_coverage",
    "check_inbox_cap",
    "check_open_pr_bound",
    "check_parked_merges",
    "check_poison_breaker",
    "consecutive_poison_count",
    "factory_lock_held",
    "launchd_log_dir",
    "owned_run_spend",
    "poll",
    "poll_ci",
    "read_ci_ledger",
    "read_ledger",
    "recorded_merges",
    "render_launchd_plist",
    "run_dir_names",
    "run_serve",
    "settle_approval_run",
    "sync",
]
