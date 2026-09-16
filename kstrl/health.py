"""R8.4 (#151): factory health trending, advisory only.

``health_breaches`` is the contract ``kstrl/factory.py:2888`` reads
through ``importlib`` (#232). Advisory only, per the owner's 2026-08-25
comment on #151 and ``docs/control-loop-design.md`` 5.11: this module
never mutates or demotes anything, and the seam demotes only when
``[autonomy] demote_on_health_breach`` is on (default false).

Three metrics, three one-sided control-chart rules (one point beyond 3
sigma; 2 of the last 3 monitored points beyond 2 sigma; EWMA(0.2) beyond
3 sigma) over the DECISIVE-run baseline. The rule set, the false-alarm
rate it was chosen against, and the metrics deliberately not trended are
measured in ``docs/dark-factory-roadmap.md``, R8.4 - not duplicated
here: two copies of arithmetic nothing keeps in step is how one goes
wrong.

This module performs NO file I/O of its own: it reads through
``autonomy_replay.load_runs`` and ``observability.read_progress_events``.
An unreadable history is a REFUSAL (``OSError``/``ValueError``), never
an empty read (CLAUDE.md, #260).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
    """One metric's rule arithmetic: below the floor, zero-variance, or scored."""

    metric: str
    values: tuple[float, ...]
    mean: float | None = None
    sigma: float | None = None
    ewma: float | None = None
    breach: HealthBreach | None = None

    @property
    def limit_2sigma(self) -> float | None:
        """2-sigma control limit, or None below the floor or with zero variance."""
        if self.mean is None or self.sigma is None or self.sigma <= 0.0:
            return None
        return self.mean + 2.0 * self.sigma

    @property
    def limit_3sigma(self) -> float | None:
        """3-sigma control limit, or None below the floor or with zero variance."""
        if self.mean is None or self.sigma is None or self.sigma <= 0.0:
            return None
        return self.mean + 3.0 * self.sigma

    @property
    def limit_ewma(self) -> float | None:
        """3-sigma EWMA control limit, or None below the floor or with zero variance."""
        if self.mean is None or self.sigma is None or self.sigma <= 0.0:
            return None
        sigma_ewma: float = self.sigma * (LAMBDA / (2.0 - LAMBDA)) ** 0.5
        return self.mean + 3.0 * sigma_ewma


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
        return MetricReading(metric, series)

    baseline = series[:-MONITORED_RUNS]
    monitored = series[-MONITORED_RUNS:]
    mean = statistics.fmean(baseline)
    sigma = statistics.pstdev(baseline)
    if sigma <= 0.0:
        return MetricReading(metric, series, mean, sigma)

    level = ewma(series, start=mean)
    reading = MetricReading(metric, series, mean, sigma, level)
    limit_2sigma = reading.limit_2sigma
    limit_3sigma = reading.limit_3sigma
    limit_ewma = reading.limit_ewma
    assert limit_2sigma is not None
    assert limit_3sigma is not None
    assert limit_ewma is not None
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

    return replace(reading, breach=breach)


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
        "retry_rate": tuple(retry),
        "cost_per_merged_component": tuple(cost),
        "infrastructure_error_rate": tuple(infra),
    }


def _entry_infra_count(entry: dict[str, Any]) -> tuple[str, int | None] | None:
    """This entry's ``(run_id, count)``, or ``None`` if it is not a countable one.

    ``count`` is ``None`` for a malformed entry (no ``dict``
    ``findings_summary``, or an ``infrastructure_errors`` that is not an
    ``int`` - ``isinstance(True, int)`` is ``True``, so a ``bool`` is
    excluded too): a missing key is not a measured zero (CLAUDE.md's
    #191 rule). ``"component_result"`` is a one-off literal here,
    matching the existing unenrolled literal in ``kstrl/evolution.py``,
    not in ``ENROLLED_EVENT_CONSTANTS``.
    """
    if entry.get("event_type") != "component_result":
        return None
    run_id = entry.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    summary = entry.get("findings_summary")
    raw = summary.get("infrastructure_errors") if isinstance(summary, dict) else None
    count = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    return run_id, count


def _infra_rate_by_run(entries: Sequence[dict[str, Any]]) -> dict[str, float]:
    """Mean infrastructure findings per component, per run, from the journal.

    Counts are grouped per run first (:func:`_entry_infra_count`); a run
    keeps its mean only when every one of its counts is not ``None``, so
    one malformed entry drops the WHOLE run rather than contributing a
    partial mean.
    """
    counts_by_run: dict[str, list[int | None]] = {}
    for entry in entries:
        parsed = _entry_infra_count(entry)
        if parsed is None:
            continue
        run_id, count = parsed
        counts_by_run.setdefault(run_id, []).append(count)

    result: dict[str, float] = {}
    for run_id, counts in counts_by_run.items():
        valid = [c for c in counts if c is not None]
        if len(valid) == len(counts):
            result[run_id] = sum(valid) / len(valid)
    return result


def readings_from(runs: Sequence[RunRecord], journal_path: Path) -> list[MetricReading]:
    """The rule arithmetic over an ALREADY LOADED run population.

    The one entry point every caller funnels through: ``health_breaches``
    and ``health_status`` load their own runs and then call this;
    ``ks autonomy replay`` loads runs once for its own replay report and
    hands the same population here, rather than reading the file a
    second time (#151's simplify pass).
    """
    infra = _infra_rate_by_run(read_progress_events(journal_path))
    series = _run_series(runs, infra)
    return [_reading(metric, values) for metric, values in series.items()]


def _breaches(readings: Sequence[MetricReading]) -> list[HealthBreach]:
    return [r.breach for r in readings if r.breach is not None]


def health_breaches(root_dir: Path, experiments: Path | None = None) -> list[HealthBreach]:
    """The contract #232 reads (``kstrl/factory.py:2888``).

    The seam calls this POSITIONALLY with one argument; ``experiments``
    exists only for ``ks autonomy replay --experiments``.
    """
    config = EvolutionConfig.load(root_dir)
    runs = load_runs(experiments or config.experiments_path)
    return _breaches(readings_from(runs, config.journal_path))


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


def _render(readings: Sequence[MetricReading], breaches: Sequence[HealthBreach]) -> str:
    lines = ["Factory health (R8.4, advisory)", "=" * len("Factory health (R8.4, advisory)"), ""]
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
    lines.append("")
    if breaches:
        lines.append("Breaches:")
        lines.extend(breach_lines(breaches))
    else:
        lines.append("No breaches.")
    lines.append("")
    lines.append("Advisory only. See docs/dark-factory-roadmap.md, R8.4.")
    return "\n".join(lines)


def health_status(root_dir: Path) -> tuple[str, list[HealthBreach]]:
    """The plain-text ``ks health`` report and its breach list, from ONE read."""
    config = EvolutionConfig.load(root_dir)
    runs = load_runs(config.experiments_path)
    readings = readings_from(runs, config.journal_path)
    breaches = _breaches(readings)
    return _render(readings, breaches), breaches
