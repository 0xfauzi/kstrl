"""R10.3 claim agreement, part 2: the pipeline wiring.

Split from ``tests/test_claim_agreement.py`` by #395, which is 1108 lines
and fails the file-length ratchet as ``ADDED`` under any new name (a
moved file has no counterpart at HEAD, so every function in it reads as
new). Split at the section banner that already separated the two halves:
parser/verdicts/disagreements/criterion-coverage/blocks/revert/retry
context/PRD save/finding-reaches-the-record stayed in
``tests/test_claim_agreement.py``; ``_pipeline_seams``, ``_review_hook``,
``_drive``, ``_claim_findings``, ``TestPipelineWiring``, ``TestConfig``
and ``TestUnreachableGate`` moved here.

The five shared fixtures (``_criterion``, ``_prd``, ``_review``,
``_story``, ``_write_prd``) are imported from the sibling module rather
than copied, so the two files cannot drift on what a story or a review
looks like.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kstrl.factory import FactoryConfig, claim_gate_unreachable_warning
from kstrl.findings import CLAIM_DISAGREEMENT_CATEGORY, Finding
from kstrl.manifest import Component
from kstrl.pipeline import Transition
from kstrl.prd import PRD
from kstrl.review import ReviewResult
from tests.helpers.replay import failing_run
from tests.test_claim_agreement import _criterion, _prd, _review, _story, _write_prd
from tests.test_pipeline import (
    _component,
    _factory_config,
    _make_pipeline,
    _success,
)

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
        "kstrl.git.get_diff_content",
        lambda *a, **k: "diff --git a b\n",
    )
    monkeypatch.setattr(
        "kstrl.agents.get_agent",
        lambda *a, **k: object(),
    )


def _review_hook(result: ReviewResult) -> dict[str, Any]:
    return {"run_review": lambda *a, **k: result}


def _drive(
    tmp_path: Path,
    *,
    review: ReviewResult,
    prd: PRD,
    before_run: Callable[[Any], None] | None = None,
    **config: Any,
) -> tuple[Any, Component, Transition | None, Path]:
    """``before_run`` receives the pipeline after the PRD is on disk and
    before the phase chain runs. That is the only window in which a test
    can break the write the pipeline is about to attempt without
    breaking its own setup, or spend the adversarial budget the way a
    real run spends it, through the counter rather than the config.
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
        before_run(pipeline)
    pipeline.begin_attempt(live)
    outcome = pipeline.process_result("comp-a", _success("comp-a"))
    return pipeline, live, (outcome.transition if outcome else None), prd_path


def _claim_findings(comp: Component) -> list[Finding]:
    return [f for f in comp.findings if f.category == CLAIM_DISAGREEMENT_CATEGORY]


class TestPipelineWiring:
    def test_advisory_mode_records_and_proceeds(self, tmp_path: Path) -> None:
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(
                _criterion("A", "pass"),
                _criterion("B", "advisory", "b-crit"),
            ),
            prd=_prd(_story("A", passes=True), _story("B", passes=True)),
            review_mode="advisory",
            claim_agreement="advisory",
        )
        assert transition != Transition.RETRYING
        found = _claim_findings(comp)
        assert [f.location for f in found] == ["B"]
        assert found[0].severity == "advisory"
        # The PRD is not touched in advisory mode.
        on_disk = PRD.load(prd_path)
        assert all(s.passes for s in on_disk.user_stories)

    def test_block_mode_reverts_and_retries(self, tmp_path: Path) -> None:
        pipeline, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(
                _criterion("A", "pass"),
                _criterion("B", "advisory", "b-crit"),
            ),
            prd=_prd(_story("A", passes=True), _story("B", passes=True)),
            review_mode="advisory",
            claim_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert comp.failed_phase == "review"
        assert comp.failed_check == "claim"
        assert [f.severity for f in _claim_findings(comp)] == ["fail"]

        by_id = {s.id: s for s in PRD.load(prd_path).user_stories}
        assert by_id["A"].passes is True
        assert by_id["B"].passes is False
        assert "reverted by reviewer" in by_id["B"].notes

        ctx = pipeline.component_contexts["comp-a"]
        assert "Set-point disagreement" in ctx
        assert "b-crit" in ctx
        # The reviewer's reasoning reaches the agent by no other route
        # on this path, so it must be in the context.
        assert "advisory because" in ctx

    def test_block_mode_leaves_an_agreeing_run_alone(
        self,
        tmp_path: Path,
    ) -> None:
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(_criterion("A", "pass")),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory",
            claim_agreement="block",
        )
        assert transition != Transition.RETRYING
        assert _claim_findings(comp) == []
        assert PRD.load(prd_path).user_stories[0].passes is True

    def test_hard_mode_criterion_fail_still_wins(self, tmp_path: Path) -> None:
        """Both readings land in the findings stream, and the existing
        hard-mode failure path is the one that returns."""
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(_criterion("A", "fail", "a-crit"), passed=False),
            prd=_prd(_story("A", passes=True)),
            review_mode="hard",
            claim_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert comp.failed_check == "criteria"
        assert [f.location for f in _claim_findings(comp)] == ["A"]
        # The revert belongs to the claim path, which did not run.
        assert PRD.load(prd_path).user_stories[0].passes is True

    def test_review_skip_mode_emits_nothing(self, tmp_path: Path) -> None:
        _, comp, _, _ = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=True)),
            review_mode="skip",
            claim_agreement="block",
        )
        assert _claim_findings(comp) == []

    def test_unwritable_prd_fails_the_component_without_aborting_the_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
            before_run=lambda _p: monkeypatch.setattr(PRD, "save", _boom),
            review_mode="advisory",
            claim_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert comp.failed_check == "claim"
        assert any(
            f.is_infrastructure_error and "Claim revert could not be written" in f.explanation
            for f in comp.findings
        )
        ctx = pipeline.component_contexts["comp-a"]
        assert "could NOT be reset automatically" in ctx

    def test_blocking_mode_fails_when_the_reviewer_never_reported(
        self,
        tmp_path: Path,
    ) -> None:
        """Round-1 review, P2. In advisory review mode a crashed reviewer
        yields passed=True with infrastructure_error=True, so the review
        failure path does not fire. claim_disagreements correctly
        returns nothing, but in blocking mode "the second check never
        reported" must not be spent as "the second check confirmed"."""
        pipeline, comp, transition, prd_path = _drive(
            tmp_path,
            review=ReviewResult(
                passed=True,
                mode="advisory",
                infrastructure_error=True,
                overall_notes="Review agent crashed: boom",
            ),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory",
            claim_agreement="block",
        )
        assert transition == Transition.RETRYING
        assert _claim_findings(comp) == []
        # Nothing points at a particular story, so nothing is reverted.
        assert PRD.load(prd_path).user_stories[0].passes is True
        # An outage is not a measurement. R10.2 only lets a MEASURED
        # entry retire its own phase, so recording this as one would let
        # a crashed reviewer silently drop a real earlier finding, and
        # journalling it as a disagreement would claim a reviewer
        # disagreed when none reported.
        assert comp.failed_check == "infrastructure"
        ctx = json.loads(pipeline.component_contexts["comp-a"])
        entries = [e for e in ctx["entries"] if e["phase"] == "review"]
        assert entries and all(e["infrastructure"] for e in entries)

    def test_advisory_mode_does_not_fail_on_a_silent_reviewer(
        self,
        tmp_path: Path,
    ) -> None:
        _, comp, transition, _ = _drive(
            tmp_path,
            review=ReviewResult(
                passed=True,
                mode="advisory",
                infrastructure_error=True,
            ),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory",
            claim_agreement="advisory",
        )
        assert transition != Transition.RETRYING

    def test_no_claimed_story_means_nothing_to_confirm(
        self,
        tmp_path: Path,
    ) -> None:
        """A silent reviewer only blocks when a claim is outstanding."""
        _, comp, transition, _ = _drive(
            tmp_path,
            review=ReviewResult(
                passed=True,
                mode="advisory",
                infrastructure_error=True,
            ),
            prd=_prd(_story("A", passes=False)),
            review_mode="advisory",
            claim_agreement="block",
        )
        assert transition != Transition.RETRYING

    def test_budget_exhaustion_fails_closed_in_block_mode(
        self,
        tmp_path: Path,
    ) -> None:
        """Round-2 review, P1. An exhausted adversarial budget downgrades
        review to SKIP and returns before the claim gate, so the
        component would complete with a story claiming done and only a
        phase_skipped finding. That is the gate failing open at exactly
        the moment the budget ran out."""
        _, comp, transition, prd_path = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory",
            claim_agreement="block",
            max_adversarial_calls=1,
            before_run=lambda p: p.adversarial_budget_consume(),
        )
        assert transition == Transition.FAILED
        assert comp.failed_phase == "review"
        assert comp.failed_check == "claim"
        # Retrying cannot recover budget, so it must not retry.
        assert comp.retries == 0
        assert PRD.load(prd_path).user_stories[0].passes is True

    def test_the_claim_refusal_is_a_disclosed_divergence(
        self,
        tmp_path: Path,
    ) -> None:
        """#226 round 2. The seventh row of the census in
        ``kstrl/evolution.py`` above ``_CATEGORY_BY_CHECK``, asserted
        here so the disclosure fails if it stops being true.

        The repository answers "did this run say anything about the
        factory's judgement" twice. ``factory._infra_casualty`` asks the
        FINDING question and ``autonomy_replay.RunRecord`` asks the
        SIGNATURE one. For this component they disagree: the reviewer
        never ran, so the only finding is the ``phase_skipped`` trace and
        the live side counts a judged failure, while the signature says
        infrastructure and the replay excludes the run.

        Both halves are asserted, because a divergence is only
        defensible while it is on purpose. It is not on purpose here so
        much as newly VISIBLE: before this sweep the signature was
        ``review:claim-budget-exhausted`` and the two consumers agreed
        by both calling a reviewer that never ran a verdict. Enrolling
        ``adversarial_budget`` fixed the replay half and left the live
        half, which belongs to factory.py (#332).
        """
        pipeline, comp, transition, _ = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory",
            claim_agreement="block",
            max_adversarial_calls=1,
            before_run=lambda p: p.adversarial_budget_consume(),
        )
        assert transition == Transition.FAILED
        # The signature, read where the journal reads it. The check name
        # is the part that carries: everything before the first colon is
        # what evolution._CATEGORY_BY_CHECK is asked about.
        assert pipeline.component_failure_signatures["comp-a"] == ["adversarial_budget:claim"]
        # The replay half.
        assert failing_run("adversarial_budget:claim").infra_aborted
        # The live half: no infrastructure_error finding, so
        # factory._infra_casualty returns False and the same run counts
        # as a judged failure there.
        assert not any(f.is_infrastructure_error for f in comp.findings)
        assert [f.phase for f in comp.findings if f.is_phase_skip] == ["review"]

    def test_budget_exhaustion_with_no_claim_completes(
        self,
        tmp_path: Path,
    ) -> None:
        _, _, transition, _ = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=False)),
            review_mode="advisory",
            claim_agreement="block",
            max_adversarial_calls=1,
            before_run=lambda p: p.adversarial_budget_consume(),
        )
        assert transition != Transition.FAILED

    def test_budget_exhaustion_in_advisory_mode_completes(
        self,
        tmp_path: Path,
    ) -> None:
        _, _, transition, _ = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=True)),
            review_mode="advisory",
            claim_agreement="advisory",
            max_adversarial_calls=1,
            before_run=lambda p: p.adversarial_budget_consume(),
        )
        assert transition != Transition.FAILED

    def test_explicit_skip_is_the_operators_choice_not_a_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """review_mode="skip" turns the reviewer off deliberately, and
        run_factory warns at startup that the gate cannot fire. Failing
        every component instead would make the warning a lie."""
        _, comp, transition, _ = _drive(
            tmp_path,
            review=_review(),
            prd=_prd(_story("A", passes=True)),
            review_mode="skip",
            claim_agreement="block",
        )
        assert transition != Transition.FAILED
        assert _claim_findings(comp) == []

    def test_unreadable_prd_records_but_does_not_block(
        self,
        tmp_path: Path,
    ) -> None:
        """An unreadable PRD holds no claim to disagree with, and both
        check_prd_stories and run_review already fail on it. Recorded so
        len(findings) == 0 still means every check ran."""
        comp = _component("comp-a")
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            components=[comp],
            config=_factory_config(
                review_mode="advisory",
                claim_agreement="block",
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
            f
            for f in live.findings
            if f.is_infrastructure_error and "Claim agreement not measured" in f.explanation
        ]
        assert len(infra) == 1


# --------------------------------------------------------------------
# 7. config
# --------------------------------------------------------------------


class TestConfig:
    def test_default_is_advisory(self) -> None:
        assert FactoryConfig().claim_agreement == "advisory"

    def test_config_loads_claim_agreement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_CLAIM_AGREEMENT", raising=False)
        (tmp_path / "kstrl.toml").write_text('[factory]\nclaim_agreement = "block"\n')
        assert FactoryConfig.load(tmp_path).claim_agreement == "block"

    def test_env_beats_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[factory]\nclaim_agreement = "block"\n')
        monkeypatch.setenv("KSTRL_FACTORY_CLAIM_AGREEMENT", "advisory")
        assert FactoryConfig.load(tmp_path).claim_agreement == "advisory"

    def test_invalid_toml_value_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_CLAIM_AGREEMENT", raising=False)
        (tmp_path / "kstrl.toml").write_text('[factory]\nclaim_agreement = "warn"\n')
        with pytest.raises(ValueError, match="claim_agreement"):
            FactoryConfig.load(tmp_path)

    def test_invalid_env_value_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_CLAIM_AGREEMENT", "warn")
        with pytest.raises(ValueError, match="(?i)claim_agreement"):
            FactoryConfig.load(tmp_path)

    def test_invalid_constructor_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="claim_agreement"):
            FactoryConfig(claim_agreement="warn")

    def test_from_env_reads_the_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-1 review, P3. Unlike review_mode next door, this key has
        an env var, so from_env must read it."""
        monkeypatch.setenv("KSTRL_FACTORY_CLAIM_AGREEMENT", "block")
        assert FactoryConfig.from_env().claim_agreement == "block"

    def test_from_env_rejects_an_invalid_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_CLAIM_AGREEMENT", "warn")
        with pytest.raises(ValueError, match="(?i)claim_agreement"):
            FactoryConfig.from_env()

    def test_env_value_is_not_reported_as_coming_from_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`ks factory` diffs load() against from_env() to tell the
        operator where a setting came from. A field missing from
        from_env is announced as "from kstrl.toml" when it came from the
        environment, in the one place they look to find out."""
        from kstrl.cli import _collect_toml_notes

        monkeypatch.setenv("KSTRL_FACTORY_CLAIM_AGREEMENT", "block")
        notes: list[str] = []
        _collect_toml_notes(
            notes,
            "factory",
            FactoryConfig.load(tmp_path),
            FactoryConfig.from_env(),
            set(),
        )
        assert not [n for n in notes if "claim_agreement" in n]


class TestUnreachableGate:
    """A governance control that cannot fire must say so at startup.

    Same shape and same reason as ``merge_gate_unreachable_warning``:
    believing a gate is on is worse than knowing it is off.
    """

    def test_block_with_review_skipped_warns(self) -> None:
        warning = claim_gate_unreachable_warning(
            FactoryConfig(claim_agreement="block", review_mode="skip"),
        )
        assert warning is not None
        assert "never fires" in warning

    def test_block_with_a_reviewer_running_is_silent(self) -> None:
        for mode in ("hard", "advisory"):
            assert (
                claim_gate_unreachable_warning(
                    FactoryConfig(claim_agreement="block", review_mode=mode),
                )
                is None
            )

    def test_advisory_mode_never_warns(self) -> None:
        assert (
            claim_gate_unreachable_warning(
                FactoryConfig(review_mode="skip"),
            )
            is None
        )
