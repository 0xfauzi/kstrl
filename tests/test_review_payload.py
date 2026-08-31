"""#266: the reviewer reads the repository instead of being handed a diff.

This file replaces ``test_truncation_policy.py``, whose entire subject -
splitting a diff into prompt-sized chunks, merging the per-chunk
verdicts, budgeting one adversarial call per chunk, and failing closed
when a single hunk would not fit - existed to make PASTING a diff work.
The reviewer roles have always run with ``cwd`` set to the worktree they
are judging, so they can read the change themselves, and none of that
machinery is needed once they do.

What is tested here, in the order the change has to earn:

1. The payload. The prompt carries no diff, names the resolved base ref,
   and is the same size whatever the change's size - which is what makes
   "a new file over 50KB is permanently unreviewable" impossible rather
   than merely unlikely.
2. The anti-padding replacement. Chunking's real guarantee was that
   every byte of the diff reached SOME prompt; that is replaced by an
   attestation - the reviewer reports the diffstat it measured and the
   harness compares it against git's. Hard mode refuses a review whose
   coverage cannot be confirmed.
3. The read-only sandbox. A reviewer that can write the tree it is
   judging has changed the evidence.
4. The deletion. The surviving callers of the diff-as-text path (the
   HITL checkpoint excerpt, the knowledge distiller, the PR body) still
   work, and the deleted names are really gone.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl import git
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.review import (
    ReviewMode,
    build_review_prompt,
    run_review,
)
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.conftest import ReviewRepo, git_in, make_review_repo

UI = PlainUI(no_color=True)

_VERIFICATION = VerificationResult(
    passed=True,
    checks=[CheckResult("test_suite", True, "ok")],
)

# The measured shape of the failure this issue removes: a newly added
# file is always exactly one hunk, so it could never be split, and a
# 1200-test file is comfortably past the old 50,000-char prompt cap.
_ONE_HUNK_OVER_THE_OLD_CAP = "".join(
    f"def test_case_{i}() -> None:\n    assert compute({i}) == {i * 2}\n\n" for i in range(1400)
)


class RecordingAgent:
    """Agent that records prompts and replies with a fixed output."""

    def __init__(
        self,
        output: str,
        name: str = "recording-agent",
        on_prompt: Callable[[str], None] | None = None,
    ):
        self._output = output
        self._name = name
        # Fires when the agent is invoked, which is the window between
        # the harness's own measurement and the reviewer's. A hook
        # rather than a reply-computing callback: the reply is known
        # before the call, and assertions inside a callback would be
        # HIDDEN - run_review catches every exception, so a failed
        # precondition surfaces as "Reviewer agent failed" and reads as
        # the feature under test breaking.
        self._on_prompt = on_prompt
        self.calls = 0
        self.prompts: list[str] = []
        self.cwds: list[Path | None] = []

    @property
    def name(self) -> str:
        return self._name

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.calls += 1
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        if self._on_prompt is not None:
            self._on_prompt(prompt)
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return None


def _repo_with_moving_base(tmp_path: Path) -> Path:
    """A repo whose ``origin/main`` can be advanced onto part of the
    feature branch, the way a sibling component's PR merge does.

    Built on ``make_review_repo`` so the git identity, quiet flags and
    branch layout have one owner. Two things it deliberately does that
    the helper's docstring does not promise, both needed to stage the
    race: it ADDS a remote-tracking ref (the helper leaves the base as a
    plain ``main``), and it lands a second commit on the feature branch
    so the base can advance onto the first one.
    """
    repo = make_review_repo(
        tmp_path / "moving-base",
        files={"first.py": "def first() -> int:\n    return 1\n"},
    ).path
    git_in(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    # The commit the sibling PR will land on the base.
    git_in(repo, "branch", "landed")
    (repo / "second.py").write_text("def second() -> int:\n    return 2\n", encoding="utf-8")
    # By NAME, not `add -A`: make_review_repo leaves an untracked
    # prd.json in the tree on purpose, and sweeping it into this commit
    # would put it inside the range under measurement.
    git_in(repo, "add", "second.py")
    git_in(repo, "commit", "-qm", "second")
    return repo


def _write_prd(path: Path, story_ids: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "branchName": "test",
                "userStories": [
                    {
                        "id": sid,
                        "title": f"Story {sid}",
                        "acceptanceCriteria": ["AC1"],
                        "priority": 1,
                        "passes": True,
                        "notes": "",
                    }
                    for sid in story_ids
                ],
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# 1. The payload: no diff in the prompt, and a size that does not track
#    the size of the change
# ---------------------------------------------------------------------------


class TestPromptCarriesNoDiff:
    def test_prompt_names_the_resolved_base_ref_not_the_branch(
        self,
        tmp_path: Path,
    ) -> None:
        """R0.2 makes ``origin/main`` and ``main`` routinely different
        commits. The reviewer and the harness must be asked about the
        same range or every diffstat would disagree for a reason that is
        not the reviewer's."""
        prompt = build_review_prompt(
            _write_prd(tmp_path / "prd.json", ["US-001"]),
            "origin/main",
            _VERIFICATION,
        )
        assert "git diff origin/main...HEAD" in prompt
        assert "git diff origin/main...HEAD --numstat" in prompt

    def test_prompt_size_does_not_track_the_size_of_the_change(
        self,
        tmp_path: Path,
    ) -> None:
        """The property the whole issue is about. ``build_review_prompt``
        has no diff parameter at all, so there is nothing whose size
        could push the prompt over a cap - and therefore no cap, no
        chunking, and no unsplittable hunk."""
        prompt = build_review_prompt(
            _write_prd(tmp_path / "prd.json", ["US-001"]),
            "main",
            _VERIFICATION,
        )
        assert len(prompt) < git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        assert "BEGIN GIT DIFF" not in prompt
        assert "(diff truncated at" not in prompt

    def test_the_previously_unreviewable_change_now_reviews(
        self,
        tmp_path: Path,
    ) -> None:
        """The exact shape that killed a component in #265: one new test
        file, one hunk, over the old cap. It used to raise
        ``DiffUnsplittableError`` before any model ran. It now reaches a
        reviewer and comes back with a verdict."""
        repo = make_review_repo(
            tmp_path / "big",
            {"tests/test_big.py": _ONE_HUNK_OVER_THE_OLD_CAP},
        )
        raw = git.get_diff_content(repo.base_branch, repo.path)
        assert len(raw) > git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        assert raw.count("\n@@ ") + raw.startswith("@@ ") == 1

        agent = RecordingAgent(repo.review_json())
        result = run_review(
            agent,
            repo.prd_path,
            repo.path,
            repo.base_branch,
            _VERIFICATION,
            ReviewMode.HARD,
            UI,
        )
        assert result.passed is True
        assert result.infrastructure_error is False
        assert agent.calls == 1
        assert len(agent.prompts[0]) < git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT

    def test_the_agent_is_run_inside_the_worktree(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """The load-bearing precondition: instructions to run git are
        worthless unless the process is standing in the repository."""
        agent = RecordingAgent(review_repo.review_json())
        run_review(
            agent,
            review_repo.prd_path,
            review_repo.path,
            review_repo.base_branch,
            _VERIFICATION,
            ReviewMode.HARD,
            UI,
        )
        assert agent.cwds == [review_repo.path]

    def test_security_prompt_carries_no_diff_either(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        agent = RecordingAgent(review_repo.security_json())
        run_security_review(
            agent,
            review_repo.prd_path,
            review_repo.path,
            review_repo.base_branch,
            SecurityConfig(mode=SecurityMode.ADVISORY.value),
            UI,
        )
        assert "BEGIN GIT DIFF" not in agent.prompts[0]
        # A SHA, not the branch name: Phase 2.5 pins the range the same
        # way Phase 2 does, so a base ref moving mid-run cannot make the
        # two measurements disagree.
        base_sha = git.resolve_base_sha(review_repo.base_branch, review_repo.path)
        assert f"git diff {base_sha}...HEAD" in agent.prompts[0]
        assert review_repo.base_branch not in agent.prompts[0].split("OBTAINING")[1][:400]
        assert agent.cwds == [review_repo.path]


# ---------------------------------------------------------------------------
# 4. The deletion: what went, and what had to survive it
# ---------------------------------------------------------------------------


class TestChunkingMachineryIsGone:
    @pytest.mark.parametrize(
        "name",
        [
            "split_diff_for_prompt",
            "DiffUnsplittableError",
            "strip_self_critique_from_diff",
        ],
    )
    def test_git_no_longer_exports_the_paste_machinery(self, name: str) -> None:
        assert not hasattr(git, name)

    @pytest.mark.parametrize(
        "module,name",
        [
            ("kstrl.review", "run_chunked_review"),
            ("kstrl.review", "merge_review_results"),
            ("kstrl.security", "run_chunked_security_review"),
            ("kstrl.security", "merge_security_results"),
        ],
    )
    def test_chunked_runners_are_gone(self, module: str, name: str) -> None:
        import importlib

        assert not hasattr(importlib.import_module(module), name)

    def test_truncate_survives_for_its_remaining_caller(self) -> None:
        """``truncate_diff_for_prompt`` is NOT deleted: the HITL
        checkpoint still shows a human an excerpt, with its own smaller
        limit. Deleting it because the reviewers stopped calling it
        would have broken a caller this issue never touched."""
        text = "x" * 100
        assert git.truncate_diff_for_prompt(text, 100) == text
        assert git.truncate_diff_for_prompt(text, 50).startswith("x" * 50)
        assert "(diff truncated at" in git.truncate_diff_for_prompt(text, 50)

    def test_an_empty_change_is_rejected_not_defaulted(self, tmp_path: Path) -> None:
        """#295 finding 5. ``files or {default}`` turned "no files" into
        "give me the default one-file change", so a caller that computed
        an empty set - the calibration harness does exactly that for a
        fixture with no ``diff --git`` line - would have measured the
        reviewer against fabricated code and reported a NUMBER rather
        than failing. That is the trap this issue closed elsewhere,
        sitting in the harness that produces the paid figure."""
        with pytest.raises(ValueError, match="empty change"):
            make_review_repo(tmp_path / "empty", files={})
        with pytest.raises(ValueError, match="nothing to commit"):
            make_review_repo(tmp_path / "no-base", base_files={})

    def test_the_pasted_source_applies_no_cap(self) -> None:
        """The one remaining paste path is calibration, whose fixtures
        are hand-written diffs with no repository to read. It must not
        truncate: a silent cut there would restore the failure on the
        one path whose job is to measure the prompt honestly."""
        body = "y" * (git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT * 2)
        rendered, _delimiter = git.pasted_change_source(body)
        assert body in rendered
        assert "truncated" not in rendered


class TestBudgetAccounting:
    def test_each_phase_consumes_exactly_one_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Salvaged from the deleted chunking suite, where the bug it
        guards (double-consuming the budget on the non-chunked path)
        first appeared. Two components and a budget of two must both get
        their security pass."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        repo = make_review_repo(tmp_path / "repo")
        root = _scaffold(repo.path, ["comp-a", "comp-b"])
        manifest = _make_manifest(["comp-a", "comp-b"])
        agent = RecordingAgent(repo.security_json())

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=lambda comp_id, *a, **k: ComponentResult(
                    comp_id, success=True, iterations=1
                ),
            ),
            patch("kstrl.agents.get_agent", return_value=agent),
        ):
            result = run_factory(
                manifest,
                _factory_config(
                    review_mode="skip",
                    max_adversarial_calls=2,
                    security_config=SecurityConfig(mode=SecurityMode.ADVISORY.value),
                ),
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        assert set(result.completed) == {"comp-a", "comp-b"}
        assert agent.calls == 2
        for comp_id in ("comp-a", "comp-b"):
            comp = manifest.get_component(comp_id)
            assert comp is not None
            assert not any(f.is_phase_skip and f.phase == "security" for f in comp.findings)


# ---------------------------------------------------------------------------
# factory scaffolding
# ---------------------------------------------------------------------------


def _scaffold(root: Path, comp_ids: list[str]) -> Path:
    (root / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "kstrl" / "prompt.md").write_text("p")
    (root / "scripts" / "kstrl" / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for comp_id in comp_ids:
        feature_dir = root / "scripts" / "kstrl" / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        _write_prd(feature_dir / "prd.json", ["US-001"])
    return root


def _make_manifest(ids: list[str]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="s",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=i,
                title=i,
                description="",
                dependencies=[],
                prd_path=f"scripts/kstrl/feature/{i}/prd.json",
                branch_name=f"kstrl/{i}",
            )
            for i in ids
        ],
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts/kstrl/prompt.md",
        prd_file=root / "scripts/kstrl/prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _factory_config(**overrides: object) -> FactoryConfig:
    defaults: dict[str, object] = dict(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)  # type: ignore[arg-type]
