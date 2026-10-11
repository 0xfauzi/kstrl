"""The daemon's shared vocabulary: the [serve] config, its error, the observer port, the
Admission verdict.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kstrl.config_numbers import check_numbers


class ServeError(RuntimeError):
    """The daemon cannot run."""


@dataclass(frozen=True)
class ServeConfig:
    """``[serve]`` config: the daemon's knobs.

    ``daily_budget_usd`` lives here rather than in ``[queue]`` because
    only the daemon spends; it is nonetheless queue-LEVEL in the sense
    R8.6 means - it spans runs and it pauses the queue, as opposed to
    the per-run ``max_cost_usd`` ceiling.
    """

    poll_interval_seconds: float = 60.0
    #: 0 disables the budget entirely. Any positive value is a HARD stop.
    daily_budget_usd: float = 0.0
    #: Consecutive poisoned items that pause the whole queue. The
    #: cross-item signal per-item bounds cannot see.
    max_consecutive_poison: int = 3
    #: Hold ``caffeinate -i`` for the duration of each run so the machine
    #: does not sleep mid-factory, and sleeps freely between runs.
    caffeinate: bool = True
    #: 0 disables the per-run timeout.
    factory_timeout_seconds: float = 0.0
    #: Run unattended even when a configured budget cannot be enforced
    #: because no adapter reports cost. Explicit opt-out of the guard.
    allow_uncovered_cost: bool = False
    #: Scheduled admission stops while this many kstrl-authored PRs are
    #: open. 0 disables the bound. Manual `ks factory` / `ks run` are
    #: unaffected: a human typing the command is the authorisation.
    max_open_prs: int = 1

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ServeError(
                f"serve.poll_interval_seconds must be > 0, got {self.poll_interval_seconds}"
            )
        if self.daily_budget_usd < 0:
            raise ServeError(f"serve.daily_budget_usd must be >= 0, got {self.daily_budget_usd}")
        if self.max_consecutive_poison < 1:
            raise ServeError(
                f"serve.max_consecutive_poison must be >= 1, got {self.max_consecutive_poison}"
            )
        if self.factory_timeout_seconds < 0:
            raise ServeError(
                f"serve.factory_timeout_seconds must be >= 0, got {self.factory_timeout_seconds}"
            )
        if self.max_open_prs < 0:
            raise ServeError(f"serve.max_open_prs must be >= 0, got {self.max_open_prs}")

    @classmethod
    def from_env(cls) -> ServeConfig:
        defaults = cls()
        poll = os.environ.get("KSTRL_SERVE_POLL_INTERVAL")
        budget = os.environ.get("KSTRL_SERVE_DAILY_BUDGET_USD")
        poison = os.environ.get("KSTRL_SERVE_MAX_CONSECUTIVE_POISON")
        caffeinate = os.environ.get("KSTRL_SERVE_CAFFEINATE")
        timeout = os.environ.get("KSTRL_SERVE_FACTORY_TIMEOUT")
        uncovered = os.environ.get("KSTRL_SERVE_ALLOW_UNCOVERED_COST")
        open_prs = os.environ.get("KSTRL_SERVE_MAX_OPEN_PRS")
        return cls(
            poll_interval_seconds=(defaults.poll_interval_seconds if poll is None else float(poll)),
            daily_budget_usd=(defaults.daily_budget_usd if budget is None else float(budget)),
            max_consecutive_poison=(
                defaults.max_consecutive_poison if poison is None else int(poison)
            ),
            caffeinate=(defaults.caffeinate if caffeinate is None else caffeinate == "1"),
            factory_timeout_seconds=(
                defaults.factory_timeout_seconds if timeout is None else float(timeout)
            ),
            allow_uncovered_cost=(
                defaults.allow_uncovered_cost if uncovered is None else uncovered == "1"
            ),
            max_open_prs=(defaults.max_open_prs if open_prs is None else int(open_prs)),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> ServeConfig:
        """Precedence: env > toml > defaults; reads ``[serve]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "serve")
        defaults = cls()

        def _float(key: str, fallback: float) -> float:
            return float(section[key]) if key in section else fallback

        def _int(key: str, fallback: int) -> int:
            return int(section[key]) if key in section else fallback

        def _bool(key: str, fallback: bool) -> bool:
            return bool(section[key]) if key in section else fallback

        poll = _float("poll_interval_seconds", defaults.poll_interval_seconds)
        budget = _float("daily_budget_usd", defaults.daily_budget_usd)
        poison = _int("max_consecutive_poison", defaults.max_consecutive_poison)
        caffeinate = _bool("caffeinate", defaults.caffeinate)
        timeout = _float(
            "factory_timeout_seconds",
            defaults.factory_timeout_seconds,
        )
        uncovered = _bool("allow_uncovered_cost", defaults.allow_uncovered_cost)
        open_prs = _int("max_open_prs", defaults.max_open_prs)

        if "KSTRL_SERVE_POLL_INTERVAL" in os.environ:
            poll = float(os.environ["KSTRL_SERVE_POLL_INTERVAL"])
        if "KSTRL_SERVE_DAILY_BUDGET_USD" in os.environ:
            budget = float(os.environ["KSTRL_SERVE_DAILY_BUDGET_USD"])
        if "KSTRL_SERVE_MAX_CONSECUTIVE_POISON" in os.environ:
            poison = int(os.environ["KSTRL_SERVE_MAX_CONSECUTIVE_POISON"])
        if "KSTRL_SERVE_CAFFEINATE" in os.environ:
            caffeinate = os.environ["KSTRL_SERVE_CAFFEINATE"] == "1"
        if "KSTRL_SERVE_FACTORY_TIMEOUT" in os.environ:
            timeout = float(os.environ["KSTRL_SERVE_FACTORY_TIMEOUT"])
        if "KSTRL_SERVE_ALLOW_UNCOVERED_COST" in os.environ:
            uncovered = os.environ["KSTRL_SERVE_ALLOW_UNCOVERED_COST"] == "1"
        # Not the `in os.environ` branch its six siblings use: a seventh
        # would push this method past the cyclomatic ratchet. Same
        # semantics: int() of an int is that int, of a string parses it.
        open_prs = int(os.environ.get("KSTRL_SERVE_MAX_OPEN_PRS", open_prs))

        return check_numbers(
            cls(
                poll_interval_seconds=poll,
                daily_budget_usd=budget,
                max_consecutive_poison=poison,
                caffeinate=caffeinate,
                factory_timeout_seconds=timeout,
                allow_uncovered_cost=uncovered,
                max_open_prs=open_prs,
            )
        )


class ServeObserver(Protocol):
    """Where the daemon narrates. Keeps the loop free of UI concerns."""

    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def err(self, message: str) -> None: ...


@dataclass(frozen=True)
class Admission:
    """Whether the daemon may start another item right now."""

    allowed: bool
    reason: str = ""
    #: Set when the refusal should PAUSE the queue rather than just wait.
    pause_reason: str = ""
    resume_after: str = ""
