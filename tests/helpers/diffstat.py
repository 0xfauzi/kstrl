"""The wire shape of a reviewer reply's ``observedDiffstat``."""

from __future__ import annotations

from kstrl.git_diff import DiffStat


def diffstat_payload(stat: DiffStat) -> dict[str, int]:
    """The three key names the prompt schema and ``parse_observed_diffstat`` share."""
    return {"files": stat.files, "insertions": stat.insertions, "deletions": stat.deletions}
