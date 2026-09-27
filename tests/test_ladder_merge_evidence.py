"""The autonomy ladder counts only merges GitHub confirmed, and only clean ones as clean (#601).

The defect: ``_record_autonomy_outcome`` called ``record_merged_component()``
once per COMPLETED part with the default ``human_edited=False``. So a part
completed with no PR counted as a merge, a merge a human edited counted as
clean, and nothing ever fired ``HUMAN_REJECTED_AUTO_MERGE``.

The fix: a merge counts only when ``ComponentPipeline._record_merge`` ran for
it in this run, and it is clean only when the PR head GitHub merged
(``headRefOid``) is the commit the diff phase judged. A human rejecting a
candidate in a run operating at L3 or above demotes.

Every test is a real ``run_factory`` over a real git repository with a real
bare origin and a real engineer subprocess that commits one file. Two things
are stubbed: ``gh``, an executable on PATH, and the code reviewer
(``kstrl.factory.run_review``), because the ladder forces hard review and no
LLM may run. A human at the merge gate is a scripted interaction channel or
the real ``Inbox`` API. The replay test drives the real ``ks autonomy
replay`` CLI in a subprocess.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from kstrl.autonomy import AutonomyLevel, AutonomyState
from kstrl.factory import run_factory
from kstrl.inbox import Inbox, InboxConfig
from kstrl.interaction import PromptKind, PromptRequest, PromptResponse
from kstrl.manifest import Manifest
from kstrl.review import CriterionReview, ReviewResult
from kstrl.ui.plain import PlainUI
from tests.helpers.executables import write_executable
from tests.helpers.replay import run_record, write_runs
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    git,
    init_kstrl_repo,
    make_manifest,
)

HTTP = "http"
BAD = "bad"
APPROVE, REJECT = 0, 1

#: `pr create` remembers the head branch. `pr merge` plays GitHub: with
#: $GH_HUMAN_EDIT set, a human first pushes a commit to the PR branch (from
#: a detached worktree of the repository, so it carries the identity
#: `tests.helpers.gitrepo.set_identity` gave the repository); then
#: the PR head origin holds is recorded as the head GitHub merged. `pr view`
#: reports $GH_VIEW_STATE (MERGED by default) and, when the caller asked for
#: the headRefOid field, that head; no headRefOid with $GH_OMIT_HEAD set.
#: With $GH_MERGE_FAIL set, `pr merge` exits 1 before anything merges.
FAKE_GH = """#!/bin/sh
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  for a in "$@"; do case "$a" in --head=*) head="${a#--head=}";; esac; done
  printf '%s' "$head" > "$GH_HEAD"
  echo "https://github.com/o/r/pull/41"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  if [ -n "$GH_MERGE_FAIL" ]; then echo "merge refused" >&2; exit 1; fi
  head=$(cat "$GH_HEAD")
  if [ -n "$GH_HUMAN_EDIT" ]; then
    git worktree add -q --detach "$HUMAN_TREE" "refs/heads/$head" || exit 1
    (cd "$HUMAN_TREE" && echo fix > human-fix.txt && git add -A &&
      git commit -q -m "human fix" && git push -q origin "HEAD:refs/heads/$head") || exit 1
    git worktree remove --force "$HUMAN_TREE" || exit 1
  fi
  git ls-remote origin "refs/heads/$head" | cut -f1 > "$GH_MERGED_HEAD"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  state="${GH_VIEW_STATE:-MERGED}"
  case "$*" in *headRefOid*) asked=1;; *) asked="";; esac
  if [ -z "$asked" ] || [ -n "$GH_OMIT_HEAD" ] || [ ! -s "$GH_MERGED_HEAD" ]; then
    printf '{"state": "%s", "mergeCommit": null}\\n' "$state"
  else
    printf '{"state": "%s", "mergeCommit": null, "headRefOid": "%s"}\\n' \\
      "$state" "$(cat "$GH_MERGED_HEAD")"
  fi
  exit 0
fi
echo "[]"
exit 0
"""

#: The engineer: commits one file named after its component and completes.
#: The component `bad` writes a .pem file, which the policy envelope denies.
ENGINEER = (
    'c=$(basename "$(pwd)"); '
    'if [ "$c" = bad ]; then echo k > k.pem; else echo work > "work-$c.txt"; fi; '
    "git add -A && git commit -q -m work && echo '<promise>COMPLETE</promise>'"
)

#: What the stubbed code reviewer returns: a pass that confirms the story.
PASS = ReviewResult(
    passed=True,
    mode="hard",
    criteria=[
        CriterionReview(
            criterion="AC1", verdict="pass", explanation="ok", suggestion="", story_id="US-001"
        )
    ],
)


@pytest.fixture(autouse=True)
def _stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "gh", FAKE_GH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_HEAD", str(tmp_path / "gh.head"))
    monkeypatch.setenv("GH_MERGED_HEAD", str(tmp_path / "gh.merged_head"))
    monkeypatch.setenv("HUMAN_TREE", str(tmp_path / "human"))
    for var in ("GH_HUMAN_EDIT", "GH_OMIT_HEAD", "GH_VIEW_STATE", "GH_MERGE_FAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
    monkeypatch.setattr("kstrl.factory.run_review", lambda *a, **k: PASS)


def _project(
    tmp_path: Path,
    level: AutonomyLevel,
    *,
    toml: str = "",
    clean: int = 0,
    decisive: int = 0,
    comps: tuple[str, ...] = (HTTP,),
) -> Path:
    """A repository with a bare origin, `[inbox]` and `[autonomy]` on, and a seeded ladder."""
    root = tmp_path / "repo"
    init_kstrl_repo(root, comps, with_origin=True)
    (root / "kstrl.toml").write_text(
        "[inbox]\nenabled = true\n[autonomy]\nenabled = true\n" + toml, encoding="utf-8"
    )
    git("add", "kstrl.toml", cwd=root)
    git("commit", "-q", "-m", "config", cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    seeded = AutonomyState(
        level=int(level), clean_merges_at_level=clean, decisive_runs_at_level=decisive
    )
    assert seeded.save(root) is None
    return root


class _Human:
    """A merge-gate channel that answers each checkpoint as scripted.

    ``before`` runs while the prompt is open, with the request, so a test
    can act as a human who edits the branch before answering.
    """

    def __init__(
        self, answers: dict[str, int], before: Callable[[PromptRequest], None] | None = None
    ) -> None:
        self.answers = answers
        self.before = before

    def can_prompt(self) -> bool:
        return True

    def request(self, req: PromptRequest) -> PromptResponse:
        assert req.kind is PromptKind.CHECKPOINT, req
        if self.before is not None:
            self.before(req)
        return PromptResponse(
            request_id=req.request_id, choice=self.answers[req.component_id], answered=True
        )


class _NoOne:
    """A channel with nobody behind it, so the merge gate parks even under `pytest -s`."""

    def can_prompt(self) -> bool:
        return False

    def request(self, req: PromptRequest) -> PromptResponse:
        return PromptResponse(request_id=req.request_id, choice=req.default, answered=False)


def _run(
    root: Path,
    *,
    human: _Human | None = None,
    create_prs: bool = True,
    explicit_pause: bool = False,
    comps: tuple[str, ...] = (HTTP,),
) -> str:
    """One `run_factory` over the saved manifest (a fresh one on the first run); its output."""
    path = root / "scripts" / "kstrl" / "manifest.json"
    manifest = (
        Manifest.load(path) if path.exists() else make_manifest([component(c) for c in comps])
    )
    config = factory_config(create_prs=create_prs)
    if explicit_pause:
        config.pause_before_pr_merge = True
        config.explicit_fields = frozenset({"pause_before_pr_merge"})
    out = io.StringIO()
    run_factory(
        manifest,
        config,
        base_config(root, agent_cmd=ENGINEER),
        PlainUI(no_color=True, file=out),
        root,
        interaction=human if human is not None else _NoOne(),
    )
    return out.getvalue()


def _evidence_line(out: str) -> str:
    lines = [line for line in out.splitlines() if "Autonomy evidence" in line]
    assert len(lines) == 1, out
    return lines[0]


def _parked_then_approved(root: Path) -> str:
    """Run 1 parks at the gate (nobody to ask); the item is approved; run 2 merges."""
    _run(root)
    box = Inbox(root, InboxConfig.load(root))
    items = [item for item in box.open_items() if str(item.kind) == "merge_gate"]
    assert len(items) == 1, [str(i.kind) for i in box.open_items()]
    box.approve(items[0].id, actor="operator")
    return _run(root)


class TestTheCleanStreak:
    def test_a_parked_merge_of_exactly_the_judged_commit_extends_the_clean_streak(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L2_GATED_MERGE, clean=14, decisive=8)
        out = _parked_then_approved(root)
        state = AutonomyState.load(root)
        assert (state.clean_merges_at_level, state.components_merged_at_level) == (15, 1)
        assert state.promotion_blockers() == []
        assert "clean: http" in _evidence_line(out)

    def test_a_merge_whose_pr_head_moved_after_kstrl_pushed_breaks_the_clean_streak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L2_GATED_MERGE, clean=14, decisive=8)
        monkeypatch.setenv("GH_HUMAN_EDIT", "1")
        out = _parked_then_approved(root)
        state = AutonomyState.load(root)
        assert (state.clean_merges_at_level, state.components_merged_at_level) == (0, 1)
        assert "0/15 consecutive merges approved without edits" in state.promotion_blockers()
        assert "edited: http" in _evidence_line(out)

    def test_a_commit_made_while_the_checkpoint_was_open_breaks_the_clean_streak(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L2_GATED_MERGE, clean=14, decisive=8)

        def commit_in_the_worktree(req: PromptRequest) -> None:
            listing = git("worktree", "list", "--porcelain", cwd=root).split("\n\n")
            (block,) = [b for b in listing if f"branch refs/heads/kstrl/factory/{HTTP}" in b]
            worktree = Path(block.splitlines()[0].removeprefix("worktree "))
            (worktree / "human-fix.txt").write_text("fix\n", encoding="utf-8")
            git("add", "-A", cwd=worktree)
            git("commit", "-q", "-m", "human fix", cwd=worktree)

        out = _run(root, human=_Human({HTTP: APPROVE}, before=commit_in_the_worktree))
        state = AutonomyState.load(root)
        assert (state.clean_merges_at_level, state.components_merged_at_level) == (0, 1)
        assert "edited: http" in _evidence_line(out)

    def test_a_merge_whose_head_github_did_not_report_is_not_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L2_GATED_MERGE, clean=14, decisive=8)
        monkeypatch.setenv("GH_OMIT_HEAD", "1")
        out = _run(root, human=_Human({HTTP: APPROVE}))
        state = AutonomyState.load(root)
        assert (state.clean_merges_at_level, state.components_merged_at_level) == (0, 1)
        assert "head unknown: http" in _evidence_line(out)

    def test_a_merge_the_next_run_confirms_by_repolling_extends_the_clean_streak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L2_GATED_MERGE, clean=14, decisive=8)
        monkeypatch.setenv("GH_VIEW_STATE", "OPEN")
        _run(root, human=_Human({HTTP: APPROVE}))
        comp = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").get_component(HTTP)
        assert comp is not None and comp.status == "merge_pending", comp
        monkeypatch.delenv("GH_VIEW_STATE")
        out = _run(root)
        state = AutonomyState.load(root)
        assert (state.clean_merges_at_level, state.components_merged_at_level) == (15, 1)
        assert "clean: http" in _evidence_line(out)

    def test_a_part_completed_without_a_pr_is_not_a_merge(self, tmp_path: Path) -> None:
        root = _project(tmp_path, AutonomyLevel.L1_SUPERVISED)
        out = _run(root, create_prs=False)
        state = AutonomyState.load(root)
        assert state.decisive_runs_at_level == 1
        assert (state.components_merged_at_level, state.clean_merges_at_level) == (0, 0)
        assert "completed without a merge: http" in _evidence_line(out)
        assert not (tmp_path / "gh.head").exists()


POLICY = "[policy]\nenabled = true\nlicense_use_network = false\n"


class TestAHumanRejectionAtL3Demotes:
    def test_a_rejection_at_the_merge_gate_at_l3_demotes_with_the_human_rejected_trigger(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO, toml=POLICY)
        _run(root, human=_Human({HTTP: REJECT}), explicit_pause=True)
        state = AutonomyState.load(root)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert [t.trigger for t in state.history] == ["human_rejected_auto_merge"]
        assert state.history[-1].evidence["components"] == [HTTP]
        notices = [
            i
            for i in Inbox(root, InboxConfig.load(root)).open_items()
            if str(i.kind) == "demotion_notice"
        ]
        assert len(notices) == 1

    def test_a_pr_closed_while_its_merge_waited_at_l3_demotes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO, toml=POLICY)
        monkeypatch.setenv("GH_VIEW_STATE", "CLOSED")
        _run(root)
        comp = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").get_component(HTTP)
        assert comp is not None and comp.failed_check == "pr_closed"
        state = AutonomyState.load(root)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert [t.trigger for t in state.history] == ["human_rejected_auto_merge"]

    def test_a_rejection_in_a_run_clamped_to_l2_does_not_demote(self, tmp_path: Path) -> None:
        root = _project(
            tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO, toml="[policy]\nenabled = false\n"
        )
        _run(root, human=_Human({HTTP: REJECT}))
        state = AutonomyState.load(root)
        assert state.level == int(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert state.history == []

    def test_a_pr_flow_failure_at_l3_is_not_a_human_rejection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO, toml=POLICY)
        monkeypatch.setenv("GH_MERGE_FAIL", "1")
        monkeypatch.setenv("GH_VIEW_STATE", "OPEN")
        _run(root)
        comp = Manifest.load(root / "scripts" / "kstrl" / "manifest.json").get_component(HTTP)
        assert comp is not None and comp.failed_check == "pr_flow", comp
        state = AutonomyState.load(root)
        assert state.level == int(AutonomyLevel.L3_ENVELOPED_AUTO)
        assert state.history == []

    def test_a_policy_violation_and_a_rejection_in_one_run_demote_once(
        self, tmp_path: Path
    ) -> None:
        comps = (HTTP, BAD)
        root = _project(tmp_path, AutonomyLevel.L3_ENVELOPED_AUTO, toml=POLICY, comps=comps)
        _run(root, human=_Human({HTTP: REJECT}), explicit_pause=True, comps=comps)
        state = AutonomyState.load(root)
        assert state.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert [t.trigger for t in state.history] == ["policy_violation"]
        assert state.history[-1].evidence["components"] == [BAD]
        assert state.history[-1].evidence["human_rejected"] == [HTTP]


def test_the_replay_does_not_predict_l3_from_rows_without_edit_evidence(tmp_path: Path) -> None:
    """25 clean one-merge runs: the replay may predict L2 but never L3."""
    root = tmp_path / "repo"
    root.mkdir()
    write_runs(
        root,
        [run_record(run_id=f"r{i}", timestamp=f"2026-09-{i:02d}T00:00:00Z") for i in range(1, 26)],
    )
    result = subprocess.run(
        [sys.executable, "-m", "kstrl", "autonomy", "replay", "--root", str(root)],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "Final level after replay: L2" in out, out
    assert "L3 is not predictable from this file" in out, out
    assert "L2 -> L3" not in out, out
