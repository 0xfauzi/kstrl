"""The single-agent engineer loop.

The iterations, the budget and the breaker.
"""

from __future__ import annotations

from kstrl.loop.breaker import BREAKER_SECTION, BreakerConfig
from kstrl.loop.loop import STOP_EXIT_CODE, LoopResult, determine_branch, run_loop
from kstrl.loop.loop_budget import UNENFORCEABLE_CALLS, LoopBudget

__all__ = [
    "BREAKER_SECTION",
    "BreakerConfig",
    "LoopBudget",
    "LoopResult",
    "STOP_EXIT_CODE",
    "UNENFORCEABLE_CALLS",
    "determine_branch",
    "run_loop",
]
