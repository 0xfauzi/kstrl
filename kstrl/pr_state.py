"""GitHub's view of a pull request: its state, its mergeability, and
the commit a merge produced.

Split out of :mod:`kstrl.pr` when that module approached the 800-line
pre-commit ratchet carrying the merge ref. Reading what GitHub thinks of
a PR is a different job from creating one and writing its body, and it
is the only job that produces the release ref.

``wait_for_merge`` deliberately stayed in :mod:`kstrl.pr`. Moving it
would leave ``kstrl.pr.wait_for_merge`` existing but unread by
``kstrl.pipeline``, and three tests patch that exact attribute with no
``raising=False``: the patch would succeed, the tests would poll a real
``gh``, and they would HANG rather than fail.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from kstrl.jsonread import read_json

# Explicit budget for every gh poll in this module (R0.2): a hung gh
# call previously blocked the factory scheduler forever.
GH_POLL_TIMEOUT = 30.0


@dataclass(frozen=True)
class PrView:
    """GitHub's view of one PR: its state and, when merged, the commit
    the merge produced.

    ``merge_sha`` is "" whenever GitHub has published none, so an
    unrecorded ref reads as unrecorded rather than as a ref.
    """

    state: str
    merge_sha: str = ""


def _pr_state(pr_number: int, cwd: Path) -> PrView | None:
    """Fetch a PR's state and merge commit via gh.

    Returns ``None`` when the state could not be determined (gh error,
    timeout, bad JSON). ``merge_sha`` is "" when GitHub has published no
    commit for the merge (e.g. an ``--auto`` merge that landed between
    polls but has not yet published the ref, or a PR that was never
    merged). Never falls back to another ref and never raises.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state,mergeCommit"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=GH_POLL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = read_json(result.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    state = str(data.get("state", ""))
    commit = data.get("mergeCommit")
    sha = str(commit.get("oid", "")) if isinstance(commit, dict) else ""
    return PrView(state=state, merge_sha=sha) if state else None


def _pr_mergeable(pr_number: int, cwd: Path) -> str | None:
    """Fetch GitHub's mergeability verdict for a PR: "MERGEABLE",
    "CONFLICTING", or "UNKNOWN" (still computing). None when it could
    not be determined (gh error, timeout, bad JSON) - callers must
    treat None and UNKNOWN as "not proven conflicting"."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "mergeable"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=GH_POLL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = read_json(result.stdout)
    except ValueError:
        return None
    mergeable = data.get("mergeable", "") if isinstance(data, dict) else ""
    return str(mergeable) or None
