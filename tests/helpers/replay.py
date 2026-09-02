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

from kstrl.autonomy_replay import RunRecord


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
