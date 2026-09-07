"""Fixture snapshots: recording what passed, and noticing when it stops.

Split out of ``kstrl/fixtures.py`` when that file crossed the 800-line
ratchet measured against ``origin/main`` (792 there, 838 on this branch).
The seam is the one the ratchet's own text asks for: running a fixture and
remembering what it produced last time are two jobs, and only the second
touches the snapshot directory. ``check_fixtures`` calls both functions and
is the only caller inside ``kstrl/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.atomicio import atomic_write_json

if TYPE_CHECKING:
    # Only for the annotations. `kstrl.fixtures` imports this module at run
    # time to call both functions, so a run-time import back would be a
    # cycle; neither function constructs either type, it only reads their
    # fields, and `from __future__ import annotations` keeps the names out
    # of the run-time namespace.
    from kstrl.fixtures import Fixture, FixtureResult


def save_snapshot(
    component_id: str,
    fixtures: list[Fixture],
    results: list[FixtureResult],
    snapshot_dir: Path,
) -> None:
    """Save successful fixture outputs as a JSON snapshot for regression detection.

    Only saves results for fixtures that passed. The snapshot captures the actual
    output so future runs can detect behavioral regressions. Written
    atomically through ``atomicio``, which owns that pattern (#291).
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{component_id}.json"

    snapshot_entries = []
    for fixture, result in zip(fixtures, results, strict=True):
        if result.passed:
            snapshot_entries.append(
                {
                    "description": fixture.description,
                    "fixture_type": fixture.fixture_type,
                    "actual": result.actual,
                }
            )

    snapshot_data = {
        "component_id": component_id,
        "fixture_count": len(snapshot_entries),
        "entries": snapshot_entries,
    }

    atomic_write_json(snapshot_path, snapshot_data)


def check_snapshot_regression(
    component_id: str,
    results: list[FixtureResult],
    snapshot_dir: Path,
) -> list[str]:
    """Compare current results against saved snapshots to detect regressions.

    Returns a list of regression descriptions. An empty list means no regressions.
    A regression is detected when a fixture that previously passed now fails,
    or when its actual output has changed.
    """
    snapshot_path = snapshot_dir / f"{component_id}.json"

    if not snapshot_path.exists():
        return []

    try:
        snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return ["Failed to read snapshot file - cannot check for regressions"]

    previous_entries = {entry["description"]: entry for entry in snapshot_data.get("entries", [])}

    regressions: list[str] = []

    for result in results:
        description = result.fixture.description
        previous = previous_entries.get(description)

        if previous is None:
            # New fixture - no regression possible
            continue

        # Previously passed but now fails
        if not result.passed:
            regressions.append(
                f"Regression in '{description}': previously passed, now fails - {result.message}"
            )
            continue

        # Output changed from previous snapshot
        if result.actual != previous.get("actual", ""):
            regressions.append(
                f"Output changed in '{description}': "
                f"previous={previous.get('actual', '')!r}, "
                f"current={result.actual!r}"
            )

    return regressions
