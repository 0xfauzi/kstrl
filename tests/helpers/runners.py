"""A recording `ks factory` stand-in, shared by daemon-composition tests.

Moved out of ``tests/test_serve_seam.py`` (#231 D3) once
``tests/test_steering.py`` became a second consumer: a cross-module
import of another test module's private helper breaks COLLECTION of the
importer the moment the private is renamed, which is exactly what
``tests/test_helper_import_direction.py`` exists to catch. A helper two
test modules share has exactly one place to be.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from kstrl.serve import RunOutcome


def recording_runner(
    calls: list[dict[str, Any]],
    outcome: RunOutcome | None = None,
) -> Any:
    """A factory stand-in for the composition tests.

    These tests are about whether the daemon reaches the runner AT ALL
    with remotely-sourced work, so the runner records and returns; the
    stub-driven classification tests own everything past that point.
    """
    result = outcome or RunOutcome(0)

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome:
        # The spec is read at CALL time on purpose. The queue moves the
        # item out of running/ when the cycle finishes, so a path
        # captured here and read afterwards is already stale - which is
        # correct behaviour, and would otherwise read as a defect.
        calls.append(
            {
                "spec_path": spec_path,
                "spec_exists": spec_path.exists(),
                "spec_text": (spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""),
                "project_name": project_name,
                "pause_before_pr_merge": pause_before_pr_merge,
            }
        )
        return result

    return runner
