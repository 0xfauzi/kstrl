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
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl import git
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.review import (
    REVIEWER_PROMPT,
    ReviewMode,
    ReviewResult,
    build_review_prompt,
    parse_review_output,
    run_review,
)
from kstrl.security import (
    SECURITY_PROMPT,
    SecurityConfig,
    SecurityMode,
    _build_security_prompt,
    parse_security_output,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.conftest import ReviewRepo, make_review_repo

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

    def __init__(self, output: str, name: str = "recording-agent"):
        self._output = output
        self._name = name
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
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return None


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
        assert f"git diff {review_repo.base_branch}...HEAD" in agent.prompts[0]
        assert agent.cwds == [review_repo.path]


# ---------------------------------------------------------------------------
# 2. The anti-padding replacement: the diffstat attestation
# ---------------------------------------------------------------------------


class TestDiffstatParsing:
    def test_well_formed_stat_is_read(self) -> None:
        assert git.parse_observed_diffstat(
            {"files": 3, "insertions": 120, "deletions": 4}
        ) == git.DiffStat(files=3, insertions=120, deletions=4)

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "3 files",
            [],
            {},
            {"files": 1, "insertions": 2},
            {"files": 1, "insertions": 2, "deletions": "0"},
            {"files": 1, "insertions": 2, "deletions": -1},
            {"files": True, "insertions": 2, "deletions": 0},
        ],
        ids=[
            "missing",
            "string",
            "list",
            "empty",
            "short",
            "non-int",
            "negative",
            "bool",
        ],
    )
    def test_malformed_stat_reads_as_no_claim(self, value: object) -> None:
        """Every malformed shape means the same thing downstream - no
        usable claim about what was read - and coercing one into a
        number would manufacture a claim the reviewer never made. The
        bool case matters because ``True`` is an ``int`` in Python and
        would otherwise land as a 1-file change."""
        assert git.parse_observed_diffstat(value) is None

    def test_review_and_security_parsers_read_the_same_field(self) -> None:
        payload = {"observedDiffstat": {"files": 2, "insertions": 9, "deletions": 1}}
        expected = git.DiffStat(files=2, insertions=9, deletions=1)
        review = parse_review_output(json.dumps({**payload, "stories": [], "concerns": []}))
        security = parse_security_output(json.dumps({**payload, "findings": []}), "advisory")
        assert review.observed_diffstat == expected
        assert security.observed_diffstat == expected


class TestDiffstatDisagreement:
    def test_matching_stat_is_no_disagreement(self) -> None:
        stat = git.DiffStat(files=1, insertions=2, deletions=0)
        assert git.diffstat_disagreement(stat, stat) is None

    def test_absent_stat_reads_as_nothing_read(self) -> None:
        message = git.diffstat_disagreement(
            None,
            git.DiffStat(files=1, insertions=2, deletions=0),
        )
        assert message is not None
        assert "reported no diffstat" in message

    def test_wrong_stat_names_both_figures(self) -> None:
        message = git.diffstat_disagreement(
            git.DiffStat(files=1, insertions=2, deletions=0),
            git.DiffStat(files=4, insertions=900, deletions=12),
        )
        assert message is not None
        assert "1 files, +2/-0" in message
        assert "4 files, +900/-12" in message


class TestCoverageAttestation:
    def _run(
        self,
        repo: ReviewRepo,
        output: str,
        mode: ReviewMode,
    ) -> ReviewResult:
        return run_review(
            RecordingAgent(output),
            repo.prd_path,
            repo.path,
            repo.base_branch,
            _VERIFICATION,
            mode,
            UI,
        )

    def test_matching_diffstat_passes_clean(self, review_repo: ReviewRepo) -> None:
        result = self._run(review_repo, review_repo.review_json(), ReviewMode.HARD)
        assert result.passed is True
        assert result.diffstat_disagreement == ""
        assert result.concerns == []
        assert "UNVERIFIED COVERAGE" not in result.as_pr_body_section()

    def test_hard_mode_refuses_an_unverified_review(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """A reviewer answering with a diffstat that is not git's did not
        read this change, whatever its verdicts say. Hard mode must not
        merge on it. Infrastructure rather than a criterion failure: the
        verdicts are unattributable, not wrong, and charging the engineer
        a retry for a reviewer that cannot reach the repo would spend
        engineer iterations on a harness fault."""
        result = self._run(
            review_repo,
            review_repo.review_json(
                observedDiffstat={"files": 1, "insertions": 1, "deletions": 0},
            ),
            ReviewMode.HARD,
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "coverage unverified" in result.overall_notes

    def test_a_missing_diffstat_is_treated_as_unverified(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """The field is new, so the interesting case is a reviewer that
        simply does not emit it. Silence must not read as agreement."""
        payload = json.loads(review_repo.review_json())
        del payload["observedDiffstat"]
        result = self._run(review_repo, json.dumps(payload), ReviewMode.HARD)
        assert result.passed is False
        assert result.infrastructure_error is True

    def test_advisory_mode_records_it_and_continues(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """Advisory mode never blocks, but the pass must be visibly
        unverified: an advisory concern in the findings stream and an
        annotation in the PR body, the same shape the old partial-review
        marker used."""
        result = self._run(
            review_repo,
            review_repo.review_json(observedDiffstat={"files": 9, "insertions": 9, "deletions": 9}),
            ReviewMode.ADVISORY,
        )
        assert result.passed is True
        assert result.infrastructure_error is False
        markers = [c for c in result.concerns if "Unverified review coverage" in c.explanation]
        assert len(markers) == 1
        assert markers[0].severity == "advisory"
        assert "UNVERIFIED COVERAGE" in result.as_pr_body_section()

    def test_security_hard_mode_refuses_an_unverified_review(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        result = run_security_review(
            RecordingAgent(
                review_repo.security_json(
                    observedDiffstat={"files": 0, "insertions": 0, "deletions": 0},
                )
            ),
            review_repo.prd_path,
            review_repo.path,
            review_repo.base_branch,
            SecurityConfig(mode=SecurityMode.HARD.value),
            UI,
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "coverage unverified" in result.overall_notes

    def test_an_unverified_review_keeps_the_findings_it_did_return(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """A reviewer can report real findings AND fail to prove it read
        the whole change. Before #266 every infrastructure_error result
        was built empty, so ``as_findings`` was allowed to return only
        the synthetic infra finding; this is the first path that sets the
        flag on a populated result. Dropping the findings there would
        have deleted real evidence, and would have left the typed stream
        and the PR body - which renders them either way - disagreeing
        about the same review."""
        result = self._run(
            review_repo,
            review_repo.review_json(
                observedDiffstat={"files": 1, "insertions": 1, "deletions": 0},
                concerns=[
                    {
                        "category": "security_concern",
                        "severity": "fail",
                        "location": "src/mod.py:1",
                        "explanation": "hardcoded credential",
                        "suggestion": "move it to the environment",
                    }
                ],
            ),
            ReviewMode.HARD,
        )
        assert result.infrastructure_error is True
        findings = result.as_findings()
        assert findings[0].is_infrastructure_error
        assert any("hardcoded credential" in f.explanation for f in findings)
        # The PR body and the findings stream tell the same story.
        assert "hardcoded credential" in result.as_pr_body_section()

    def test_an_unverified_security_review_keeps_its_findings(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        result = run_security_review(
            RecordingAgent(
                review_repo.security_json(
                    observedDiffstat={"files": 0, "insertions": 0, "deletions": 0},
                    findings=[
                        {
                            "category": "hardcoded_secret",
                            "severity": "critical",
                            "location": "src/mod.py:1",
                            "explanation": "API key in source",
                            "suggestion": "move it to the environment",
                        }
                    ],
                )
            ),
            review_repo.prd_path,
            review_repo.path,
            review_repo.base_branch,
            SecurityConfig(mode=SecurityMode.HARD.value),
            UI,
        )
        assert result.infrastructure_error is True
        findings = result.as_findings()
        assert findings[0].is_infrastructure_error
        assert any("API key in source" in f.explanation for f in findings)

    def test_security_advisory_marker_never_trips_the_threshold(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        """The marker finding is severity "low" on purpose: in advisory
        mode it must be visible without becoming the thing that fails a
        hard-mode run somewhere else."""
        result = run_security_review(
            RecordingAgent(
                review_repo.security_json(
                    observedDiffstat={"files": 9, "insertions": 9, "deletions": 9},
                )
            ),
            review_repo.prd_path,
            review_repo.path,
            review_repo.base_branch,
            SecurityConfig(mode=SecurityMode.ADVISORY.value),
            UI,
        )
        assert result.passed is True
        markers = [f for f in result.findings if "Unverified security review" in f.explanation]
        assert len(markers) == 1
        assert markers[0].severity == "low"

    def test_an_unmeasurable_diff_is_infrastructure_not_a_zero(
        self,
        tmp_path: Path,
    ) -> None:
        """``get_diff_numstat``'s lenient contract returns [] on failure,
        which folds to "0 files, +0/-0" and would AGREE with a reviewer
        that read nothing. The strict path must raise instead, so a
        directory that is not a repository fails closed."""
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        result = run_review(
            RecordingAgent("{}"),
            _write_prd(not_a_repo / "prd.json", ["US-001"]),
            not_a_repo,
            "main",
            _VERIFICATION,
            ReviewMode.HARD,
            UI,
        )
        assert result.passed is False
        assert result.infrastructure_error is True

    def test_the_prompt_tells_the_reviewer_the_stat_is_checked(
        self,
        tmp_path: Path,
    ) -> None:
        """An attestation the reviewer does not know is checked is an
        invitation to guess the number. Both prompts have to declare the
        field AND say the harness runs the same command."""
        assert "observedDiffstat" in REVIEWER_PROMPT
        assert "observedDiffstat" in SECURITY_PROMPT
        rendered = [
            build_review_prompt(
                _write_prd(tmp_path / "prd.json", ["US-001"]),
                "main",
                _VERIFICATION,
            ),
            _build_security_prompt("prd", git.repo_change_source("main")),
        ]
        for prompt in rendered:
            assert "The harness runs the same command and" in prompt
            assert "never what you expect the answer to be" in prompt


# ---------------------------------------------------------------------------
# 3. The read-only sandbox
# ---------------------------------------------------------------------------


# The read-only reviewer's argv/settings assertions live in
# tests/test_sandbox.py::TestReadOnlyReviewerPassThrough, next to the
# adapter pass-through helpers they share. What belongs HERE is the
# wiring: that the two reviewer phases actually ask for it.


class TestReviewerPhasesAreReadOnly:
    def test_review_and_security_phases_ask_for_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end through the factory: whatever the operator's
        sandbox config says, both reviewer roles are constructed
        read-only. Measured, an unsandboxed reviewer left __pycache__/
        in the tree it was judging."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        repo = make_review_repo(tmp_path / "repo")
        root = _scaffold(repo.path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        agent = RecordingAgent(repo.review_json())
        seen: list[object] = []

        def fake_get_agent(*args: object, **kwargs: object) -> object:
            seen.append(kwargs.get("read_only"))
            return agent

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=ComponentResult("comp-a", success=True, iterations=1),
            ),
            patch("kstrl.agents.get_agent", side_effect=fake_get_agent),
        ):
            run_factory(
                manifest,
                _factory_config(
                    review_mode="advisory",
                    security_config=SecurityConfig(mode=SecurityMode.ADVISORY.value),
                ),
                _base_config(root),
                PlainUI(no_color=True),
                root,
            )
        assert seen == [True, True]


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

    def test_the_pasted_source_applies_no_cap(self) -> None:
        """The one remaining paste path is calibration, whose fixtures
        are hand-written diffs with no repository to read. It must not
        truncate: a silent cut there would restore the failure on the
        one path whose job is to measure the prompt honestly."""
        body = "y" * (git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT * 2)
        rendered = git.pasted_change_source(body, "TOKEN")
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
