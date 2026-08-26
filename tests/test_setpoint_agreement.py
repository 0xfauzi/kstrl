"""R10.3 set-point agreement: a story is done only when both sensors say so.

The engineer agent is the only writer of the PRD's ``passes`` flag: the
thing that does the work also files the report on the work. The reviewer
is a second, independent reading of the same question, and it has been
available and ignored since R1.1. These tests pin the rule that the two
must agree, and the two modes it ships in - record only, or revert the
flag and retry.

The pipeline-level tests import the harness from tests.test_pipeline
rather than rebuilding it, which is also where the reviewer stub seam
lives (``_recording_hooks``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kstrl.evolution import signatures_from_findings
from kstrl.factory import FactoryConfig
from kstrl.findings import SETPOINT_DISAGREEMENT_CATEGORY, Finding, finding_model
from kstrl.manifest import Component
from kstrl.pipeline import Transition
from kstrl.pr import _generate_pr_body
from kstrl.prd import PRD, UserStory
from kstrl.review import (
    CriterionReview,
    ReviewResult,
    parse_review_output,
    revert_unconfirmed_stories,
    setpoint_blocks,
    setpoint_disagreements,
)
from tests.test_pipeline import (
    _component,
    _factory_config,
    _make_pipeline,
    _success,
)

# --------------------------------------------------------------------
# builders
# --------------------------------------------------------------------


def _story(
    story_id: str, *, passes: bool, criteria: list[str] | None = None,
    notes: str = "",
) -> UserStory:
    return UserStory(
        id=story_id,
        title=f"Story {story_id}",
        acceptance_criteria=criteria or [f"{story_id} works"],
        priority=1,
        passes=passes,
        notes=notes,
    )


def _prd(*stories: UserStory) -> PRD:
    return PRD(branch_name="kstrl/factory/comp-a", user_stories=list(stories))


def _criterion(
    story_id: str, verdict: str, criterion: str = "does the thing",
) -> CriterionReview:
    return CriterionReview(
        criterion=criterion,
        verdict=verdict,
        explanation=f"{verdict} because",
        suggestion="" if verdict == "pass" else "fix it",
        story_id=story_id,
    )


def _review(
    *criteria: CriterionReview, passed: bool = True, infra: bool = False,
    model: str = "",
) -> ReviewResult:
    return ReviewResult(
        passed=passed, mode="advisory", criteria=list(criteria),
        infrastructure_error=infra, reviewer_model=model,
    )


def _write_prd(root: Path, comp: Component, prd: PRD) -> Path:
    path = root / comp.prd_path
    path.parent.mkdir(parents=True, exist_ok=True)
    prd.save(path)
    return path


# --------------------------------------------------------------------
# 1. the parser keeps the story id
# --------------------------------------------------------------------


class TestParserKeepsStoryId:
    def test_parse_review_output_keeps_story_id(self) -> None:
        raw = json.dumps({
            "stories": [
                {
                    "storyId": "US-001",
                    "criteria": [
                        {"criterion": "a", "verdict": "pass",
                         "explanation": "ok", "suggestion": ""},
                    ],
                },
                {
                    "storyId": "US-002",
                    "criteria": [
                        {"criterion": "b", "verdict": "fail",
                         "explanation": "no", "suggestion": "do b"},
                    ],
                },
            ],
            "concerns": [],
            "overallNotes": "",
        })
        result = parse_review_output(raw)
        assert not result.infrastructure_error, result.overall_notes
        assert [c.story_id for c in result.criteria] == ["US-001", "US-002"]

    def test_story_id_is_stored_stripped_but_not_lowercased(self) -> None:
        """The raw value stays inspectable; normalising happens at
        lookup, which is what lets a reviewer's case drift still match."""
        raw = json.dumps({
            "stories": [{
                "storyId": "  us-001  ",
                "criteria": [{"criterion": "a", "verdict": "pass",
                              "explanation": "ok", "suggestion": ""}],
            }],
            "concerns": [], "overallNotes": "",
        })
        result = parse_review_output(raw)
        assert result.criteria[0].story_id == "us-001"


# --------------------------------------------------------------------
# 2. rolling criterion verdicts up into a story verdict
# --------------------------------------------------------------------


class TestStoryVerdicts:
    def test_story_verdicts_fail_dominates(self) -> None:
        review = _review(
            _criterion("A", "pass"), _criterion("A", "fail"),
            _criterion("A", "advisory"),
        )
        assert review.story_verdicts() == {"a": "fail"}

    def test_story_verdicts_advisory_when_no_fail(self) -> None:
        review = _review(_criterion("A", "pass"), _criterion("A", "advisory"))
        assert review.story_verdicts() == {"a": "advisory"}

    def test_story_verdicts_pass_only_when_all_pass(self) -> None:
        review = _review(_criterion("A", "pass"), _criterion("A", "pass"))
        assert review.story_verdicts() == {"a": "pass"}

    def test_story_verdicts_ignores_empty_story_id(self) -> None:
        review = _review(_criterion("", "fail"), _criterion("A", "pass"))
        assert review.story_verdicts() == {"a": "pass"}

    def test_story_verdicts_ignores_an_unrecognised_verdict(self) -> None:
        """The parser cannot emit one, so this only guards a
        hand-built result. Skipping is the safe direction: the story
        ends with no reading and therefore reads as uncovered, not as
        confirmed."""
        review = _review(_criterion("A", "Blocked"))
        assert review.story_verdicts() == {}

    def test_uncovered_story_is_absent_not_defaulted(self) -> None:
        assert _review(_criterion("A", "pass")).story_verdicts().get("b") is None

    def test_non_pass_criteria_filters_by_story(self) -> None:
        review = _review(
            _criterion("A", "fail", "a-crit"),
            _criterion("B", "fail", "b-crit"),
            _criterion("A", "pass", "a-ok"),
        )
        assert [c.criterion for c in review.non_pass_criteria("A")] == ["a-crit"]


# --------------------------------------------------------------------
# 3. the disagreement check
# --------------------------------------------------------------------


class TestSetpointDisagreements:
    def test_disagreement_on_advisory_verdict(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=True))
        review = _review(
            _criterion("A", "pass"), _criterion("B", "advisory", "b-crit"),
        )
        found = setpoint_disagreements(prd, review, severity="advisory")
        assert len(found) == 1
        assert found[0].location == "B"
        assert found[0].category == SETPOINT_DISAGREEMENT_CATEGORY
        assert found[0].severity == "advisory"
        assert found[0].phase == "review"
        assert "advisory" in found[0].explanation
        assert found[0].suggestion == "b-crit"

    def test_disagreement_on_uncovered_story(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=True))
        review = _review(_criterion("A", "pass"))
        found = setpoint_disagreements(prd, review, severity="advisory")
        assert [f.location for f in found] == ["B"]
        assert "not covered" in found[0].explanation
        assert found[0].suggestion == ""

    def test_no_disagreement_for_unclaimed_story(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=False))
        review = _review(_criterion("A", "pass"), _criterion("B", "fail"))
        assert setpoint_disagreements(prd, review, severity="advisory") == []

    def test_no_disagreement_on_infra_error(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(passed=False, infra=True)
        assert setpoint_disagreements(prd, review, severity="advisory") == []

    def test_story_id_matching_is_case_insensitive(self) -> None:
        """The reviewer writing "us-001" for a PRD's "US-001" is
        agreement, not an uncovered story."""
        prd = _prd(_story("US-001", passes=True))
        review = _review(_criterion("us-001", "pass"))
        assert setpoint_disagreements(prd, review, severity="advisory") == []

    def test_severity_is_the_callers_choice(self) -> None:
        prd = _prd(_story("A", passes=True))
        found = setpoint_disagreements(prd, _review(), severity="fail")
        assert [f.severity for f in found] == ["fail"]

    def test_findings_carry_the_reviewing_model(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(model="codex (gpt-5)")
        found = setpoint_disagreements(prd, review, severity="advisory")
        assert finding_model(found[0]) == "codex (gpt-5)"


class TestSetpointBlocks:
    @pytest.mark.parametrize(("mode", "level", "expected"), [
        ("advisory", 0, False),
        ("block", 0, True),
        ("advisory", 1, True),
        ("advisory", 2, True),
    ])
    def test_setpoint_blocks_rules(
        self, mode: str, level: int, expected: bool,
    ) -> None:
        config = FactoryConfig(setpoint_agreement=mode)
        assert setpoint_blocks(config, level) is expected

    def test_matches_the_published_lesson_table(self) -> None:
        """docs/lessons/verify/pr-221/setpoint_agreement.py publishes
        ``blocks(mode, level) = mode == "block" or level >= 1`` as a
        claim about this code. If they ever disagree the lesson page is
        wrong, so the agreement is asserted rather than assumed."""
        for mode in ("advisory", "block"):
            for level in range(5):
                config = FactoryConfig(setpoint_agreement=mode)
                assert setpoint_blocks(config, level) is (
                    mode == "block" or level >= 1
                )


# --------------------------------------------------------------------
# 4. reverting the flag
# --------------------------------------------------------------------


class TestRevert:
    def test_revert_resets_the_flag_and_notes_why(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=True))
        review = _review(
            _criterion("A", "pass"), _criterion("B", "advisory", "b-crit"),
        )
        found = setpoint_disagreements(prd, review, severity="fail")
        assert revert_unconfirmed_stories(
            prd, review, found, attempt=2,
        ) == ["B"]
        by_id = {s.id: s for s in prd.user_stories}
        assert by_id["A"].passes is True
        assert by_id["B"].passes is False
        assert by_id["B"].notes == (
            "reverted by reviewer (attempt 2): b-crit"
        )

    def test_revert_of_an_uncovered_story_says_so(self) -> None:
        prd = _prd(_story("A", passes=True, notes="earlier note"))
        found = setpoint_disagreements(prd, _review(), severity="fail")
        revert_unconfirmed_stories(prd, _review(), found, attempt=1)
        assert prd.user_stories[0].notes == (
            "earlier note\n"
            "reverted by reviewer (attempt 1): story not covered by review"
        )


class TestPrdSave:
    def test_save_of_an_unchanged_prd_is_byte_identical(
        self, tmp_path: Path,
    ) -> None:
        """The factory writes PRDs with the same two-space indent and
        trailing newline PRD.save emits (decompose's atomic JSON
        writer), so a load-save cycle must not reformat the file. The
        revert has to change one flag and nothing else.
        """
        path = tmp_path / "prd.json"
        _prd(_story("A", passes=True), _story("B", passes=True)).save(path)
        original = path.read_bytes()

        PRD.load(path).save(path)
        assert path.read_bytes() == original

    def test_revert_changes_only_the_reverted_story(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "prd.json"
        _prd(_story("A", passes=True), _story("B", passes=True)).save(path)
        before = json.loads(path.read_text())

        prd = PRD.load(path)
        review = _review(_criterion("A", "pass"), _criterion("B", "fail"))
        found = setpoint_disagreements(prd, review, severity="fail")
        revert_unconfirmed_stories(prd, review, found, attempt=1)
        prd.save(path)

        after = json.loads(path.read_text())
        assert after["userStories"][0] == before["userStories"][0]
        assert after["userStories"][1]["passes"] is False
        assert after["branchName"] == before["branchName"]


# --------------------------------------------------------------------
# 5. the journal signature and the pull-request body
# --------------------------------------------------------------------


class TestFindingReachesTheRecord:
    def test_blocking_finding_produces_the_journal_signature(self) -> None:
        prd = _prd(_story("A", passes=True))
        found = setpoint_disagreements(prd, _review(), severity="fail")
        assert signatures_from_findings("review", found) == [
            "review:setpoint_disagreement",
        ]

    def test_advisory_finding_produces_no_signature(self) -> None:
        """signatures_from_findings only emits for fail/critical/high,
        so in advisory mode the journal carries the finding itself (via
        Component.findings) but no failure signature. Stated here so the
        asymmetry is a decision on the record rather than a surprise."""
        prd = _prd(_story("A", passes=True))
        found = setpoint_disagreements(prd, _review(), severity="advisory")
        assert signatures_from_findings("review", found) == []

    def test_finding_renders_in_the_pr_body(self, tmp_path: Path) -> None:
        """An advisory-only gate whose output never reaches the pull
        request is decoration. The review_findings string cannot carry
        it: that renders criteria and concerns, and this is neither."""
        from kstrl.manifest import Manifest

        comp = _component("comp-a")
        prd = _prd(_story("US-002", passes=True))
        comp.findings = setpoint_disagreements(
            prd, _review(_criterion("US-002", "advisory", "handles empties")),
            severity="advisory",
        )
        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="test",
            base_branch="main", single_pr=False, components=[comp],
        )
        body = _generate_pr_body(comp, manifest)
        assert SETPOINT_DISAGREEMENT_CATEGORY in body
        assert "US-002" in body


# --------------------------------------------------------------------
# 6. through the pipeline
# --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pipeline_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same seams tests.test_pipeline stubs, plus the ladder.

    An autouse fixture only applies inside the module that defines it,
    so importing the harness does not bring the stubs with it. Blocking
    must depend on the config alone here, so the autonomy ladder is
    explicitly off rather than incidentally absent.
    """
    monkeypatch.delenv("KSTRL_AUTONOMY_ENABLED", raising=False)
    monkeypatch.setattr(
        "kstrl.git.get_diff_content", lambda *a, **k: "diff --git a b\n",
    )
    monkeypatch.setattr(
        "kstrl.agents.get_agent", lambda *a, **k: object(),
    )


def _review_hook(result: ReviewResult) -> dict[str, Any]:
    return {"run_review": lambda *a, **k: result}


def _drive(
    tmp_path: Path, *, review: ReviewResult, prd: PRD,
    before_run: Callable[[], None] | None = None, **config: Any,
) -> tuple[Any, Component, Transition | None, Path]:
    """``before_run`` runs after the PRD is on disk and before the phase
    chain does, which is the only window in which a test can break the
    write the pipeline is about to attempt without breaking its own
    setup.
    """
    comp = _component("comp-a")
    pipeline, manifest, _, _ = _make_pipeline(
        tmp_path,
        components=[comp],
        config=_factory_config(**config),
        hooks_overrides=_review_hook(review),
    )
    prd_path = _write_prd(tmp_path, comp, prd)
    live = manifest.get_component("comp-a")
    assert live is not None
    if before_run is not None:
        before_run()
    pipeline.begin_attempt(live)
    outcome = pipeline.process_result("comp-a", _success("comp-a"))
    return pipeline, live, (outcome.transition if outcome else None), prd_path


def _setpoint_findings(comp: Component) -> list[Finding]:
    return [
        f for f in comp.findings
        if f.category == SETPOINT_DISAGREEMENT_CATEGORY
    ]


class TestPipelineWiring:
    def test_advisory_mode_records_and_proceeds(self, tmp_path: Path) -> None:
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(
                _criterion("A", "pass"), _criterion("B", "advisory", "b-crit"),
            ),
            prd=_prd(_story("A", passes=True), _story("B", passes=True)),
            review_mode="advisory", setpoint_agreement="advisory",
        )
        assert transition != Transition.RETRYING
        found = _setpoint_findings(comp)
        assert [f.location for f in found] == ["B"]
        assert found[0].severity == "advisory"
        # The PRD is not touched in advisory mode.
        on_disk = PRD.load(prd_path)
        assert all(s.passes for s in on_disk.user_stories)

    def test_block_mode_reverts_and_retries(self, tmp_path: Path) -> None:
        pipeline, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(
                _criterion("A", "pass"), _criterion("B", "advisory", "b-crit"),
            ),
            prd=_prd(_story("A", passes=True), _story("B", passes=True)),
            review_mode="advisory", setpoint_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert comp.failed_phase == "review"
        assert comp.failed_check == "setpoint"
        assert [f.severity for f in _setpoint_findings(comp)] == ["fail"]

        by_id = {s.id: s for s in PRD.load(prd_path).user_stories}
        assert by_id["A"].passes is True
        assert by_id["B"].passes is False
        assert "reverted by reviewer" in by_id["B"].notes

        ctx = pipeline.component_contexts["comp-a"]
        assert "Set-point disagreement" in ctx
        assert "b-crit" in ctx

    def test_block_mode_leaves_an_agreeing_run_alone(
        self, tmp_path: Path,
    ) -> None:
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(_criterion("A", "pass")),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory", setpoint_agreement="block",
        )
        assert transition != Transition.RETRYING
        assert _setpoint_findings(comp) == []
        assert PRD.load(prd_path).user_stories[0].passes is True

    def test_hard_mode_criterion_fail_still_wins(self, tmp_path: Path) -> None:
        """Both readings land in the findings stream, and the existing
        hard-mode failure path is the one that returns."""
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(_criterion("A", "fail", "a-crit"), passed=False),
            prd=_prd(_story("A", passes=True)),
            review_mode="hard", setpoint_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert comp.failed_check == "criteria"
        assert [f.location for f in _setpoint_findings(comp)] == ["A"]
        # The revert belongs to the set-point path, which did not run.
        assert PRD.load(prd_path).user_stories[0].passes is True

    def test_review_skip_mode_emits_nothing(self, tmp_path: Path) -> None:
        _, comp, _, _ = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=True)),
            review_mode="skip", setpoint_agreement="block",
        )
        assert _setpoint_findings(comp) == []

    def test_unwritable_prd_fails_the_component_without_aborting_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """factory.py calls process_result without a try, so an
        exception escaping the phase chain would take the whole run
        down, not just this component. A save that cannot be written
        degrades to an infrastructure finding, the component still
        fails, and the retry text stops claiming a revert that did not
        happen."""
        def _boom(self: PRD, path: Path) -> None:
            raise OSError("read-only file system")

        pipeline, comp, transition, _ = _drive(
            tmp_path,
            review=_review(_criterion("B", "fail", "b-crit")),
            prd=_prd(_story("B", passes=True)),
            before_run=lambda: monkeypatch.setattr(PRD, "save", _boom),
            review_mode="advisory", setpoint_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert comp.failed_check == "setpoint"
        assert any(
            f.is_infrastructure_error
            and "Set-point revert could not be written" in f.explanation
            for f in comp.findings
        )
        ctx = pipeline.component_contexts["comp-a"]
        assert "could NOT be reset automatically" in ctx

    def test_unreadable_prd_records_but_does_not_block(
        self, tmp_path: Path,
    ) -> None:
        """An unreadable PRD holds no claim to disagree with, and both
        check_prd_stories and run_review already fail on it. Recorded so
        len(findings) == 0 still means every sensor ran."""
        comp = _component("comp-a")
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            components=[comp],
            config=_factory_config(
                review_mode="advisory", setpoint_agreement="block",
            ),
            hooks_overrides=_review_hook(_review()),
        )
        path = tmp_path / comp.prd_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")
        live = manifest.get_component("comp-a")
        assert live is not None
        pipeline.begin_attempt(live)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))

        assert outcome is not None
        assert outcome.transition != Transition.RETRYING
        infra = [
            f for f in live.findings
            if f.is_infrastructure_error
            and "Set-point agreement not measured" in f.explanation
        ]
        assert len(infra) == 1


# --------------------------------------------------------------------
# 7. config
# --------------------------------------------------------------------


class TestConfig:
    def test_default_is_advisory(self) -> None:
        assert FactoryConfig().setpoint_agreement == "advisory"

    def test_config_loads_setpoint_agreement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_SETPOINT_AGREEMENT", raising=False)
        (tmp_path / "kstrl.toml").write_text(
            '[factory]\nsetpoint_agreement = "block"\n'
        )
        assert FactoryConfig.load(tmp_path).setpoint_agreement == "block"

    def test_env_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[factory]\nsetpoint_agreement = "block"\n'
        )
        monkeypatch.setenv("KSTRL_FACTORY_SETPOINT_AGREEMENT", "advisory")
        assert FactoryConfig.load(tmp_path).setpoint_agreement == "advisory"

    def test_invalid_toml_value_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_SETPOINT_AGREEMENT", raising=False)
        (tmp_path / "kstrl.toml").write_text(
            '[factory]\nsetpoint_agreement = "warn"\n'
        )
        with pytest.raises(ValueError, match="setpoint_agreement"):
            FactoryConfig.load(tmp_path)

    def test_invalid_env_value_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_SETPOINT_AGREEMENT", "warn")
        with pytest.raises(ValueError, match="(?i)setpoint_agreement"):
            FactoryConfig.load(tmp_path)

    def test_invalid_constructor_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="setpoint_agreement"):
            FactoryConfig(setpoint_agreement="warn")
