"""R8.4 (#151): factory health trending, advisory only.

``kstrl/factory.py`` has read a module named ``kstrl.health`` since #232
and nothing supplied it. ``health_breaches`` is the contract #232 reads
(``kstrl/factory.py:2888``). Advisory only, per the owner's 2026-08-25
comment on #151 and ``docs/control-loop-design.md`` 5.11: this module
never mutates or demotes anything; the existing seam decides what to do
with the list it returns, and demotes only when
``[autonomy] demote_on_health_breach`` is on (default false).

Three metrics, because three are what the recorded data carries
(measured, ``docs/dark-factory-roadmap.md`` R8.4): ``retry_rate`` (the
``experiments.tsv`` column), ``cost_per_merged_component``
(``total_cost_usd / completed``), ``infrastructure_error_rate`` (mean of
``findings_summary["infrastructure_errors"]`` per run, from the
journal). Calibration detection delta and human-edit rate are NOT
trended: the first has no per-run series (baselines are captured by the
opt-in calibration suite, not once per run, and already has its own
trigger, ``DemotionTrigger.CALIBRATION_REGRESSION``); the second is not
recorded anywhere in production
(``AutonomyState.record_merged_component(human_edited=...)`` has one
caller and it is a test).

All three metrics are one-sided: a RISE is the bad news, so the rules
compare the high side only. No ``direction`` parameter - always +1 is
dead generality, and the other tail has no metric yet.

Rules, in this fixed order, at most one breach per metric: one point
beyond 3 sigma; 2 of the last 3 monitored points beyond 2 sigma; then
EWMA(lambda=0.2) beyond 3 sigma. The EWMA rule earns its place on
measured grounds (roadmap R8.4): it costs 0.0007 of false-alarm
probability at n=8 and doubles detection of a six-run sustained shift at
n=30, which the three-point window misses.

The n floor is ``kstrl.autonomy.MIN_DECISIVE_RUNS`` (8), reused rather
than retyped. The textbook 1-in-370 false-alarm rate does NOT hold at
that floor: mu and sigma are estimated from the baseline rather than
known, so the measured rate is about 1 in 10 per metric per run at n=8
(200,000-trial Monte Carlo, roadmap R8.4). ``demote_on_health_breach``
must stay false until a project's history is long enough to bring that
down.

The baseline is the series minus the last 3 (``MONITORED_RUNS``) runs,
never the whole series and never a fixed constant. The population is
DECISIVE runs only (``autonomy_replay.RunRecord.decisive``), reused so
the health rules and the ladder replay agree about which runs count. A
shift running longer than the monitored window contaminates the
baseline and partly hides itself (measured power 0.13 against 0.36); the
EWMA rule is what partially covers that case. When the baseline has ZERO
variation (``sigma <= 0.0``) there are no limits and no breach,
whatever the monitored points do.

This module performs NO file I/O of its own: both reads go through the
existing tolerant readers, ``autonomy_replay.load_runs`` and
``observability.read_progress_events``, so it stays outside the
encoding-site and append-site guards. ``load_runs`` raises ``OSError``
and ``ValueError`` for an unreadable or unparseable file, and both are
ALLOWED OUT deliberately: an unreadable history is a REFUSAL, not an
empty read (CLAUDE.md, #260). The two callers handle it: ``ks health``
exits 2 naming the cause, and the factory seam's caller catches
``Exception`` and warns non-fatally. ``read_progress_events`` swallows
its own read errors and returns ``[]`` (the house tolerant JSONL
reader), unchanged here: an unreadable journal reads as ``n=0`` rather
than a clean bill of health, which is why ``ks health`` always prints
the per-metric ``n``.

No sparklines: non-ASCII bytes on an operator's terminal for no
information the numbers do not already carry, and CLAUDE.md is explicit
about kstrl not being a source of non-ASCII output.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.autonomy import MIN_DECISIVE_RUNS
from kstrl.autonomy_replay import RunRecord, load_runs
from kstrl.evolution import EvolutionConfig
from kstrl.observability import read_progress_events

LAMBDA = 0.2
MONITORED_RUNS = 3
RULE_ONE_POINT = "1 point beyond 3 sigma"
RULE_TWO_OF_THREE = "2 of 3 beyond 2 sigma"
RULE_EWMA = "EWMA(0.2) beyond 3 sigma"
METRIC_RETRY_RATE = "retry_rate"
METRIC_COST_PER_MERGED = "cost_per_merged_component"
METRIC_INFRA_ERROR_RATE = "infrastructure_error_rate"


@dataclass(frozen=True)
class HealthBreach:
    """The contract #232 is written against (``kstrl/factory.py:2926``).

    Five fields, frozen, no more: this is read structurally through a
    ``Protocol`` on the other side, so a sixth field here changes
    nothing the seam sees, and a field renamed here breaks it silently.
    """

    metric: str
    rule: str
    value: float
    limit: float
    window_runs: int


@dataclass(frozen=True)
class MetricReading:
    """One metric's full arithmetic, in one of three shapes.

    Below the floor: every statistic is ``None``. A baseline with zero
    variation: ``mean`` and ``sigma`` are set, every limit and ``ewma``
    is ``None`` (an EWMA level with no limit to compare it against is a
    number nobody can act on), and there is no breach. Otherwise:
    everything is set and ``breach`` may or may not be ``None``.
    """

    metric: str
    values: tuple[float, ...]
    mean: float | None
    sigma: float | None
    ewma: float | None
    limit_2sigma: float | None
    limit_3sigma: float | None
    limit_ewma: float | None
    breach: HealthBreach | None


def ewma(values: Sequence[float], *, start: float) -> float:
    """z_t = LAMBDA * x_t + (1 - LAMBDA) * z_{t-1}, seeded at ``start``."""
    z = start
    for x in values:
        z = LAMBDA * x + (1.0 - LAMBDA) * z
    return z


def _reading(metric: str, values: Sequence[float]) -> MetricReading:
    """The whole rule arithmetic. About 30 lines, and no more than that."""
    series = tuple(values)
    n = len(series)
    if n < MIN_DECISIVE_RUNS:
        return MetricReading(metric, series, None, None, None, None, None, None, None)

    baseline = series[:-MONITORED_RUNS]
    monitored = series[-MONITORED_RUNS:]
    mean = statistics.fmean(baseline)
    sigma = statistics.pstdev(baseline)
    if sigma <= 0.0:
        return MetricReading(metric, series, mean, sigma, None, None, None, None, None)

    limit_3sigma = mean + 3.0 * sigma
    limit_2sigma = mean + 2.0 * sigma
    # ** 0.5 rather than math.sqrt, so there is no `math` import for one use.
    sigma_ewma = sigma * (LAMBDA / (2.0 - LAMBDA)) ** 0.5
    limit_ewma = mean + 3.0 * sigma_ewma
    level = ewma(series, start=mean)
    worst = max(monitored)

    breach: HealthBreach | None
    if worst > limit_3sigma:
        breach = HealthBreach(metric, RULE_ONE_POINT, worst, limit_3sigma, n)
    elif sum(1 for x in monitored if x > limit_2sigma) >= 2:
        breach = HealthBreach(metric, RULE_TWO_OF_THREE, monitored[-1], limit_2sigma, n)
    elif level > limit_ewma:
        breach = HealthBreach(metric, RULE_EWMA, level, limit_ewma, n)
    else:
        breach = None

    return MetricReading(
        metric, series, mean, sigma, level, limit_2sigma, limit_3sigma, limit_ewma, breach
    )


def _run_series(
    runs: Sequence[RunRecord], infra_by_run: dict[str, float]
) -> dict[str, tuple[float, ...]]:
    """Three chronological series over DECISIVE runs only."""
    retry: list[float] = []
    cost: list[float] = []
    infra: list[float] = []
    for run in runs:
        if not run.decisive:
            continue
        retry.append(run.retry_rate)
        if run.total_cost_usd is not None and run.completed > 0:
            cost.append(run.total_cost_usd / run.completed)
        if run.run_id in infra_by_run:
            infra.append(infra_by_run[run.run_id])
    return {
        METRIC_RETRY_RATE: tuple(retry),
        METRIC_COST_PER_MERGED: tuple(cost),
        METRIC_INFRA_ERROR_RATE: tuple(infra),
    }


def _infra_rate_by_run(entries: Sequence[dict[str, Any]]) -> dict[str, float]:
    """Mean infrastructure findings per component, per run, from the journal.

    A run is measurable only if EVERY one of its ``component_result``
    entries carries a ``dict`` ``findings_summary`` whose
    ``infrastructure_errors`` is an ``int`` (and not a ``bool``:
    ``isinstance(True, int)`` is ``True``). Any entry that fails this
    drops the WHOLE run from the mapping rather than contributing a
    partial mean. A missing key is not a measured zero (CLAUDE.md's
    #191 rule); "component_result" is a one-off literal here, matching
    the existing unenrolled literal in ``kstrl/evolution.py``, not in
    ``ENROLLED_EVENT_CONSTANTS``.
    """
    counts_by_run: dict[str, list[int]] = {}
    dropped: set[str] = set()
    for entry in entries:
        if entry.get("event_type") != "component_result":
            continue
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if run_id in dropped:
            continue
        summary = entry.get("findings_summary")
        count: object = None
        if isinstance(summary, dict):
            count = summary.get("infrastructure_errors")
        if not isinstance(count, int) or isinstance(count, bool):
            dropped.add(run_id)
            counts_by_run.pop(run_id, None)
            continue
        counts_by_run.setdefault(run_id, []).append(count)
    return {run_id: sum(counts) / len(counts) for run_id, counts in counts_by_run.items()}


def _readings(root_dir: Path, experiments: Path | None = None) -> list[MetricReading]:
    config = EvolutionConfig.load(root_dir)
    runs = load_runs(experiments or config.experiments_path)
    infra = _infra_rate_by_run(read_progress_events(config.journal_path))
    series = _run_series(runs, infra)
    return [_reading(metric, values) for metric, values in series.items()]


def health_breaches(root_dir: Path, experiments: Path | None = None) -> list[HealthBreach]:
    """The contract #232 reads (``kstrl/factory.py:2888``).

    The seam calls this POSITIONALLY with one argument; ``experiments``
    exists only for ``ks autonomy replay --experiments``.
    """
    return [r.breach for r in _readings(root_dir, experiments) if r.breach is not None]


def breach_lines(breaches: Sequence[HealthBreach]) -> list[str]:
    """One operator-facing line per breach.

    ONE home for this prose: both ``ks health`` and ``ks autonomy
    replay`` render through this function.
    """
    return [
        f"  - {b.metric}: {b.rule} "
        f"(value {b.value:.4f} beyond limit {b.limit:.4f} over {b.window_runs} run(s))"
        for b in breaches
    ]


def _render(readings: Sequence[MetricReading]) -> str:
    lines = ["Factory health (R8.4, advisory)", "=" * len("Factory health (R8.4, advisory)"), ""]
    breaches: list[HealthBreach] = []
    for r in readings:
        name = r.metric.ljust(26)
        n = len(r.values)
        if r.mean is None:
            lines.append(f"{name}n={n}   insufficient data (need {MIN_DECISIVE_RUNS})")
        elif r.limit_3sigma is None:
            lines.append(
                f"{name}n={n}  mean {r.mean:.4f}  sigma {r.sigma:.4f}  no variation in the baseline"
            )
        else:
            lines.append(
                f"{name}n={n}  mean {r.mean:.4f}  sigma {r.sigma:.4f}  "
                f"2s {r.limit_2sigma:.4f}  3s {r.limit_3sigma:.4f}  "
                f"EWMA {r.ewma:.4f} (limit {r.limit_ewma:.4f})"
            )
            if r.breach is not None:
                breaches.append(r.breach)
    lines.append("")
    if breaches:
        lines.append("Breaches:")
        lines.extend(breach_lines(breaches))
    else:
        lines.append("No breaches.")
    lines.append("")
    lines.append(
        "Not trended, because nothing records them once per run:\n"
        "  calibration detection delta: captured by the opt-in calibration suite, not per\n"
        "    run. A regression there already demotes through CALIBRATION_REGRESSION.\n"
        "  human-edit rate: AutonomyState.record_merged_component(human_edited=...) has no\n"
        "    production caller, so every merge is recorded as clean.\n\n"
        "Advisory only. These rules never demote unless [autonomy]\n"
        "demote_on_health_breach is on (default false). Measured on stationary noise the\n"
        "rule set fires about once every 10 runs per metric at n=8 and about once every\n"
        "63 at n=30; see docs/dark-factory-roadmap.md, R8.4."
    )
    return "\n".join(lines)


def health_status(root_dir: Path) -> tuple[str, list[HealthBreach]]:
    """The plain-text ``ks health`` report and its breach list, from ONE read."""
    readings = _readings(root_dir)
    text = _render(readings)
    breaches = [r.breach for r in readings if r.breach is not None]
    return text, breaches
