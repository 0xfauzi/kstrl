"""#266: the guarantees that replaced what the pasted diff took with it.

Split from ``test_review_payload.py``, which covers the payload change
itself. Two things had to be re-established when the reviewers stopped
being handed a diff and started reading the worktree, and both live
here:

1. **The anti-padding property.** Chunking's real guarantee was that
   every byte of the diff reached SOME prompt. Its replacement is an
   attestation: the reviewer reports the diffstat it measured and the
   harness compares it against git's, refusing a hard-mode review whose
   figure is not git's.
2. **The read-only sandbox.** A reviewer that can write the tree it is
   judging has changed the evidence. The argv and settings assertions
   live in ``test_sandbox.py`` next to the adapter helpers they share;
   what is checked here is that the two reviewer phases ask for it.
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
from tests.conftest import ReviewRepo, git_in, make_review_repo

UI = PlainUI(no_color=True)

_VERIFICATION = VerificationResult(
    passed=True,
    checks=[CheckResult("test_suite", True, "ok")],
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

    def test_the_check_survives_a_base_ref_moving_mid_run(
        self,
        tmp_path: Path,
    ) -> None:
        """#295 finding 4. ``origin/<base>`` is a MOVING ref, and
        ``fetch_base_branch`` runs mid-run every time a sibling
        component's PR merges. The harness measures before the reviewer
        starts and the reviewer measures minutes later, so a name pins
        nothing across that gap: the merge-base moves under both and
        hard mode discards a review that read every byte.

        Staged exactly that way. The feature branch has two commits; the
        remote's base advances onto the FIRST of them while the reviewer
        is running, which is what a sibling merge does. The reviewer
        answers with the stat of the range it was given.
        """
        repo = _repo_with_moving_base(tmp_path)
        pinned_sha = git.resolve_base_sha("main", repo)
        pinned = git.get_diff_stat(pinned_sha, repo, resolved=True)
        reply = json.dumps(
            {
                "observedDiffstat": pinned.as_payload(),
                "stories": [
                    {
                        "storyId": "US-001",
                        "storyTitle": "Story US-001",
                        "criteria": [
                            {
                                "criterion": "AC1",
                                "verdict": "pass",
                                "explanation": "checked",
                                "suggestion": "",
                            }
                        ],
                    }
                ],
                "concerns": [],
                "exhaustively_searched": True,
                "overallNotes": "",
            }
        )
        agent = RecordingAgent(
            reply,
            on_prompt=lambda _prompt: git_in(
                repo, "update-ref", "refs/remotes/origin/main", "refs/heads/landed"
            ),
        )
        result = run_review(
            agent,
            _write_prd(repo / "prd.json", ["US-001"]),
            repo,
            "main",
            _VERIFICATION,
            ReviewMode.HARD,
            UI,
        )
        # Preconditions asserted OUT here, where a failure names itself
        # rather than surfacing as "Reviewer agent failed".
        assert git.resolve_base_sha("main", repo) != pinned_sha, "the fetch did not move"
        assert git.get_diff_stat("main", repo) != pinned, "the move did not change the range"
        # The prompt named the COMMIT, so it still points at the range
        # the harness measured; the NAME now lands somewhere else.
        assert f"git diff {pinned_sha}...HEAD" in agent.prompts[0]

        assert result.diffstat_disagreement == ""
        assert result.infrastructure_error is False
        assert result.passed is True

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
