"""The learning-readiness numbers, as lines, for every surface that shows them.

``ks evolve`` printed these four lines and the TUI's evolve screen showed
none of them (#433 F12). One function builds the text so the two cannot
drift: the CLI prints each line, the screen shows the same lines.

The numbers gate the unbuilt phases of #217. Sections 5.2 and 7 of
docs/continuous-learning-design.md say the attribution thresholds must be
measured before they are chosen; these are reads of aggregates that
already existed. A zero here is a measurement, which is what the
instrument is for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.evolution import EvolutionConfig, EvolutionJournal, FailurePattern


def readiness_lines(
    journal: EvolutionJournal,
    evo_config: EvolutionConfig,
    patterns: list[FailurePattern],
    root_dir: Path,
) -> list[str]:
    """The four readiness lines, each indented two spaces as ``ks evolve`` prints them.

    ``patterns`` is passed in rather than re-read: the caller has already
    computed it from the same journal, and a second read is a second
    answer to one question.
    """
    from kstrl.distill_readiness import distill_parse_failure_line

    util = journal.get_fact_utilization(lookback_runs=evo_config.lookback_runs)
    concern = journal.get_concern_hit_rate(lookback_runs=evo_config.lookback_runs)
    superseded_only = sum(1 for pattern in patterns if pattern.superseded_only)
    by_category = ", ".join(
        f"{name} {count}" for name, count in sorted(concern["by_category"].items())
    )
    return [
        f"  recurring signatures (>= {evo_config.min_pattern_frequency} runs): "
        f"{len(patterns)}, of which {superseded_only} only on superseded attempts",
        f"  fact utilization: measured {util['measured']}, unmeasured "
        f"{util['unmeasured']}, referenced {util['referenced']}, "
        f"runs_with_referenced {util['runs_with_referenced']}",
        f"  concern hit rate: {concern['with_concern']} of "
        f"{concern['components']} components"
        + (f", by category: {by_category}" if by_category else ""),
        # #495: the one reader of DistillResult.parse_failed. From the
        # event stream, not the journal; that module's docstring says why.
        distill_parse_failure_line(root_dir, evo_config.lookback_runs),
    ]
