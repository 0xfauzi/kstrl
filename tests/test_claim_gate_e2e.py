"""The claim gate spends no engineer retry that cannot change its answer.

A user run had a component pass every acceptance check on four attempts
while the claim gate failed it each time: once because the reviewer left a
criterion unjudged, and then on the same stories every attempt. The
reviewer's own gap now gets the reviewer one more reading, and the same
stories unconfirmed after an engineer retry halt to the inbox, where an
approval merges over the disagreement on that commit.

End to end: the real ``ks factory`` as a subprocess on a real git
repository after the real ``ks init``, with a confirmed ``[stack]``, a stub
engineer that commits, and a stub reviewer that reports the diff it
measured and counts its calls (the ``tests/test_stack_e2e.py`` harness).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import PRD_PATH, _factory, _repo, _stack

ENGINEER = "date +%s%N > made.txt && git add -A && git commit -q -m made >/dev/null 2>&1"


def _reviewer(tmp_path: Path, replies: list[list[dict[str, str]]]) -> Path:
    """A reviewer whose Nth call judges US-001's criteria as ``replies[N-1]``
    (the last again after they run out), and logs one line per call."""
    calls = tmp_path / "reviewer.calls"
    return write_executable(
        tmp_path / "reviewer.py",
        f"""#!{sys.executable}
import json, subprocess, sys
sys.stdin.read()
with open({str(calls)!r}, "a") as log:
    log.write("call\\n")
n = len(open({str(calls)!r}).read().splitlines())
replies = {json.dumps(replies)}
criteria = replies[min(n, len(replies)) - 1]
out = subprocess.run(["git", "diff", "--numstat", "main...HEAD"], capture_output=True, text=True)
rows = [[int(a), int(b), p] for a, b, p in (line.split("\\t") for line in out.stdout.splitlines())]
print(json.dumps({{
    "observedDiffstat": {{"rows": rows}},
    "stories": [{{"storyId": "US-001", "criteria": criteria}}],
    "concerns": [],
}}))
""",
    )


def _calls(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def _two_criteria(root: Path) -> None:
    """US-001 gets a second criterion, so a reviewer can leave one unjudged."""
    path = root / PRD_PATH.format(comp="greeter")
    prd = json.loads(path.read_text(encoding="utf-8"))
    prd["userStories"][0]["acceptanceCriteria"] = ["prints hello", "exits 0"]
    path.write_text(json.dumps(prd), encoding="utf-8")
    git_in(root, "commit", "-q", "-am", "two criteria")


def _verdict(criterion: str, verdict: str) -> dict[str, str]:
    return {"criterion": criterion, "verdict": verdict, "explanation": f"{verdict} by the stub"}


@runs_a_stack
def test_a_criterion_the_reviewer_left_unjudged_gets_the_reviewer_again_not_the_engineer(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, _stack({"tests": "true"}))
    _two_criteria(root)
    reviewer = _reviewer(
        tmp_path,
        [
            [_verdict("prints hello", "pass")],
            [_verdict("prints hello", "pass"), _verdict("exits 0", "pass")],
        ],
    )

    run = _factory(
        tmp_path,
        root,
        *("--review-mode", "hard", "--review-agent-cmd", str(reviewer)),
        *("--max-retries", "2"),
        engineer=ENGINEER,
    )

    assert "Review left criteria of US-001 unjudged; reading it once more" in run.out, run.out
    assert run.calls == 1, run.out
    assert _calls(tmp_path / "reviewer.calls") == 2, run.out
    assert run.code == 0, run.out


@runs_a_stack
def test_the_same_stories_unconfirmed_after_an_engineer_retry_halt_to_the_inbox(
    tmp_path: Path,
) -> None:
    """The reviewer leaves the second criterion unjudged on every reading,
    so a passed review leaves the story unconfirmed each attempt."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    _two_criteria(root)
    reviewer = _reviewer(tmp_path, [[_verdict("prints hello", "pass")]])

    run = _factory(
        tmp_path,
        root,
        *("--review-mode", "hard", "--review-agent-cmd", str(reviewer)),
        *("--max-retries", "3"),
        engineer=ENGINEER,
    )

    assert run.code == 1, run.out
    assert run.calls == 2, run.out
    assert "the same stories as the attempt before, so no engineer retries it" in run.out
    assert "pass on only 1 of 2 acceptance criteria" in run.out, run.out


@runs_a_stack
def test_an_advisory_verdict_confirms_the_story(tmp_path: Path) -> None:
    """Owner decision of 2026-10-25: an advisory verdict says the criterion
    appears implemented, with concerns the review records as non-blocking,
    so it confirms the claim. Under the old rule a user run spent three
    engineer retries on advisories with every acceptance check passing."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    reviewer = _reviewer(tmp_path, [[_verdict("prints hello", "advisory")]])

    run = _factory(
        tmp_path,
        root,
        *("--review-mode", "hard", "--review-agent-cmd", str(reviewer)),
        *("--max-retries", "3"),
        engineer=ENGINEER,
    )

    assert "claim disagreement" not in run.out, run.out
    assert (run.code, run.calls) == (0, 1), run.out
