"""One builder for the ``RunRecord``s the ladder tests replay.

``tests/helpers/journal.py`` already states the rule this exists to
follow: the record builders live here when more than one file needs the
same records. #339 review measured five hand-rolled builders across two
files, one of them byte-for-byte identical to another, so a tenth field
on ``RunRecord`` would have been a five-place edit.

Kwargs-override rather than one function per shape, because the shapes
differ in one or two fields and naming each combination is how five
builders happened in the first place. The defaults describe a run that
merged one component and failed nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from kstrl.autonomy_replay import RunRecord
from kstrl.evolution import EXPERIMENTS_HEADER, EvolutionConfig

#: A run_id containing an invalid utf-8 byte: the reachable "unreadable
#: history" fixture (the mode-0200 half needs a permission the superuser
#: ignores). #151's simplify pass hoisted this out of byte-identical
#: copies in tests/test_autonomy_ladder.py and tests/test_health_trending.py.
UNDECODABLE_TSV = b"run_id\ttimestamp\nrun-1\xff\t2026-01-01\n"


def run_record(**overrides: object) -> RunRecord:
    """A ``RunRecord`` with the given fields, defaults for the rest."""
    fields: dict[str, object] = {
        "run_id": "r1",
        "timestamp": "2026-07-20T00:00:00Z",
        "project": "demo",
        "components_total": 1,
        "completed": 1,
        "failed": 0,
        "skipped": 0,
        "retry_rate": 0.0,
        "common_failure": "",
    }
    fields.update(overrides)
    return RunRecord(**fields)  # type: ignore[arg-type]


def write_runs(root: Path, records: Sequence[RunRecord]) -> None:
    """Write ``records`` to this project's CONFIGURED experiments.tsv.

    Resolves the path the same way the module under test does
    (``EvolutionConfig.load``), so a test that moves
    ``[evolution] experiments_path`` writes to the file the code will
    actually read rather than a hardcoded default. The four columns
    ``RunRecord`` does not track (avg_iterations, avg_duration_s,
    total_tokens, unreported_calls) get a fixed placeholder: nothing
    reading through ``RunRecord`` sees them, so no test asserts on their
    value.
    """
    path = EvolutionConfig.load(root).experiments_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [EXPERIMENTS_HEADER]
    for r in records:
        cost = "" if r.total_cost_usd is None else str(r.total_cost_usd)
        lines.append(
            "\t".join(
                (
                    r.run_id,
                    r.timestamp,
                    r.project,
                    str(r.components_total),
                    str(r.completed),
                    str(r.failed),
                    str(r.skipped),
                    "1.00",
                    "100.0",
                    f"{r.retry_rate:.4f}",
                    r.common_failure,
                    "1000",
                    cost,
                    "0",
                )
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clean_run(index: int) -> RunRecord:
    """One run that merged a component and failed nothing.

    Distinct timestamps because a replay reports the run a promotion
    would have fired on, and a report that names the same instant twelve
    times is unreadable.
    """
    return run_record(
        run_id=f"r{index}",
        timestamp=f"2026-07-{(index % 28) + 1:02d}T00:00:00Z",
        project="p",
    )


def failing_run(signature: str) -> RunRecord:
    """One run that merged nothing and failed on ``signature``."""
    return run_record(
        run_id="bad",
        timestamp="2026-07-28T00:00:00Z",
        project="p",
        completed=0,
        failed=1,
        retry_rate=1.0,
        common_failure=signature,
    )
