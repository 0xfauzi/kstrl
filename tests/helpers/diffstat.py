"""The wire shape of a reviewer reply's ``observedDiffstat``."""

from __future__ import annotations

from kstrl.git_diff import DiffStat

#: House budget for diff content that a prompt carries (#266 removed its last
#: enforcing caller; #863 A2 moved it here from ``kstrl/git_diff.py``, where
#: only these tests read it). The size a prompt must stay under.
DEFAULT_PROMPT_DIFF_CHAR_LIMIT = 50_000


def diffstat_payload(stat: DiffStat) -> dict[str, int]:
    """The three key names the prompt schema and ``parse_observed_diffstat`` share."""
    return {"files": stat.files, "insertions": stat.insertions, "deletions": stat.deletions}
