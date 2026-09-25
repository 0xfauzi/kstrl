"""#500: the integration review reads a reviewer's real output.

Two defects the paid E1/E2 replays found in how kstrl reads a real
reviewer. A finding cited by bare file name (``storage.py:614-635``) was
handed off with no location, because only the exact tracked spelling was
looked up. And IC5's criterion held a placeholder (``<component>``) a
reviewer echoing it could rewrite, which made the whole review red.

The end-to-end tests drive the real ``run_factory`` through
``tests/helpers/integration_harness.py``: a real repository, the real
Phase 3 check and temporary worktree, the real ``run_review`` parser, with
only the reviewer CLI stubbed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl import git
from kstrl.integration import integration_stories
from kstrl.pipeline import ComponentPipeline
from tests.helpers import integration_harness as h
from tests.helpers.gitrepo import git_in

BARE_CITATION = (
    "Every row read back is rebuilt through save (store.py:614-635), "
    "which re-runs the create-time validators."
)


def _state(root: Path) -> dict[str, object]:
    return json.loads(h.state_file(root).read_text(encoding="utf-8"))


def test_a_bare_file_name_resolves_to_the_one_tracked_file_it_names(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", BARE_CITATION)

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    findings = _state(root)["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    finding = findings[0]
    assert finding["status"] == "open"
    assert finding["locations"] == [h.STORE]
    assert finding["missingLocations"] == []
    assert "store.py:614-635" in finding["text"]


def test_a_bare_file_name_two_tracked_files_share_stays_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    h.commit_file(root, "lib/store.py", "def save(x: int) -> int:\n    return x\n")
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", BARE_CITATION)

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    findings = _state(root)["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    finding = findings[0]
    assert finding["status"] == "handoff"
    assert finding["locations"] == []
    assert finding["missingLocations"] == ["store.py"]


def test_a_cited_name_matches_only_on_whole_path_segments(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", "tore.py:3 re-applies request rules to stored rows")

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    findings = _state(root)["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    assert findings[0]["status"] == "handoff"
    assert findings[0]["missingLocations"] == ["tore.py"]


def test_no_integration_criterion_holds_placeholder_text() -> None:
    for story in integration_stories("a" * 40):
        assert re.search(r"<[^<>\s]+>", story.criterion) is None, (
            f"{story.story_id} holds placeholder text a reviewer echoing it can rewrite: "
            f"{story.criterion!r}"
        )


def test_tracked_files_at_lists_the_commit_not_the_working_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _base, head = h.merged_feature(root)
    (root / "src" / "untracked.py").write_text("x = 1\n", encoding="utf-8")

    tracked = git.tracked_files_at(head, root)

    assert h.STORE in tracked
    assert h.API in tracked
    assert "src/untracked.py" not in tracked
    assert git.tracked_files_at(h.rev(root, "main~1"), root) == frozenset({h.STORE})


def test_tracked_files_at_refuses_a_commit_that_does_not_exist(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root)

    with pytest.raises(git.GitDiffError, match="git ls-tree"):
        git.tracked_files_at("0" * 40, root)


def test_a_listing_failure_is_not_run_and_calls_no_reviewer(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    reviewer = h.FakeReviewer(json.dumps(h.review_payload(root, base)))

    with (
        patch("kstrl.git.tracked_files_at", side_effect=git.GitDiffError("git ls-tree boom")),
        patch.object(
            ComponentPipeline,
            "adversarial_budget_consume",
            autospec=True,
            side_effect=ComponentPipeline.adversarial_budget_consume,
        ) as consume,
    ):
        h.run_factory_over(root, reviewer)

    assert reviewer.calls == 0
    assert consume.call_count == 0
    stop = _state(root)["stops"][-1]
    assert stop["outcome"] == "not_run"
    assert "could not be listed: git ls-tree boom" in stop["reason"]


def test_a_cited_path_of_several_segments_resolves_by_its_whole_suffix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    h.commit_file(root, "pkg/a/store.py", "def save(x: int) -> int:\n    return x\n")
    h.commit_file(root, "pkg/b/store.py", "def save(x: int) -> int:\n    return x\n")
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", "a/store.py:3 re-applies request rules to stored rows")

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    findings = _state(root)["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    finding = findings[0]
    assert finding["status"] == "open"
    assert finding["locations"] == ["pkg/a/store.py"]
    assert finding["missingLocations"] == []


def test_the_listing_reads_the_reviewed_commit_not_the_checked_out_one(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", BARE_CITATION)
    git_in(root, "checkout", "-q", "--detach", base)

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    state = _state(root)
    evidence = json.loads(h.evidence_files(root)[-1].read_text(encoding="utf-8"))
    assert evidence["reviewedSha"] == head
    findings = state["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    assert findings[0]["status"] == "open"
    assert findings[0]["locations"] == [h.STORE]
