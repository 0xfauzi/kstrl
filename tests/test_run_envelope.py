"""What ``run_factory`` resolves before it starts, and what it refuses (#192).

The RUN half. ``tests/test_config_read_once.py`` drives Phase 1 and
answers "what was this component held to"; this file drives
``run_factory`` and answers "what did the run resolve, what did it hand
the pipeline, and what does it do with a section it cannot read".

The refusal is round 2's subject. Every section the envelope resolves is
one more that can reject in front of a run, and round 1 had a malformed
``[inbox]`` leave ``run_factory`` as an unhandled ``ValueError`` - above
the line that records the architect's spend, so the money was gone and
nothing recorded it (#257).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyState
from kstrl.factory import ComponentResult, FactoryConfig
from kstrl.manifest import Component
from kstrl.policy import PolicyConfig
from kstrl.verify import VerificationResult
from tests.helpers.run_config import BEFORE, MALFORMED, drive_run, empty_run
from tests.helpers.verify_phase import component


class TestAMalformedMidRunEditDoesNotAbortTheRun:
    """The run, not one phase call.

    Round 1 of #192 claimed this with a test that never entered
    ``run_factory``: it called ``_phase_verify`` in a ``for`` loop, so
    ``process_result``, the scheduler and the ``try`` around
    ``future.result()`` were never reached, and the thing being asserted
    was "``_phase_verify`` does not raise". The mechanism is that
    ``process_result`` runs OUTSIDE that ``try``, so a ``ValueError``
    from it takes the whole run down rather than one component.

    The engineer is stubbed and the edit is made by the verification
    hook for the first component, which is the operator saving
    kstrl.toml while the run is between components.
    """

    @staticmethod
    def _components() -> list[Component]:
        first = component("comp-a")
        second = component("comp-b")
        return [first, second]

    def test_both_components_are_verified_and_the_run_returns(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        comps = self._components()
        verified: list[str] = []

        def verify(*_args: Any, **kwargs: Any) -> VerificationResult:
            verified.append(str(kwargs["component_id"]))
            if len(verified) == 1:
                (tmp_path / "kstrl.toml").write_text(MALFORMED, encoding="utf-8")
            return VerificationResult(passed=True, checks=[])

        with (
            patch("kstrl.factory.run_mechanical_verification", side_effect=verify),
            patch(
                "kstrl.factory._run_component",
                # First positional is the component id: the worker takes
                # primitives, not the Component, because it runs in
                # another process.
                side_effect=lambda comp_id, *a, **k: ComponentResult(
                    comp_id, success=True, iterations=1
                ),
            ),
        ):
            outcome = drive_run(
                tmp_path,
                FactoryConfig(
                    use_worktrees=False,
                    create_prs=False,
                    max_parallel=1,
                    max_retries=0,
                    retry_delay=0,
                    review_mode="skip",
                ),
                components=comps,
            )

        assert verified == ["comp-a", "comp-b"], (
            "the run did not reach both components after a malformed "
            "mid-run edit to kstrl.toml. At 414d662 the second "
            "_phase_verify re-read [policy], raised ValueError, and "
            "process_result took the run down with it."
        )
        assert outcome.result.exit_code == 0
        assert outcome.pipelines[0].run_envelope.policy.max_files_changed == 5


class TestAMalformedSectionIsARefusalAndNotATraceback:
    """Blocker 1 of the round-1 review, as a test.

    Every section the envelope resolves is one more that can reject in
    front of a run. The entry preflight resolves all of them before the
    command body, but on ``ks factory --spec`` the architect runs for
    119 to 210 seconds between that check and this resolution, so an
    operator's edit inside the window arrives here. Measured on the
    round-1 branch: `[inbox] open_item_cap = "many"` left ``run_factory``
    as an unhandled ValueError, and it did so above the line that records
    the architect's spend.
    """

    @pytest.mark.parametrize(
        ("section", "body", "key"),
        [
            ("[inbox]", '[inbox]\nopen_item_cap = "many"\n', "open_item_cap"),
            ("[policy]", '[policy]\nmax_files_changed = "two"\n', "max_files_changed"),
        ],
    )
    def test_it_refuses_with_exit_2_and_names_the_key(
        self, tmp_path: Path, section: str, body: str, key: str
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(body, encoding="utf-8")

        outcome = drive_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )

        assert outcome.result.exit_code == 2, (
            "a section the run cannot resolve must refuse the way every "
            "other pre-spend check refuses, not leave run_factory as a "
            "traceback."
        )
        assert section in outcome.narration
        assert key in outcome.narration

    def test_it_refuses_before_the_pipeline_is_built(self, tmp_path: Path) -> None:
        """Which is what keeps #257's invariant intact.

        ``record_architect_usage`` is the first thing done to the
        pipeline and nothing between the run directory's sinks and it may
        return early. This refusal happens above the sinks, so no run
        directory exists to report $0 for and no early exit was inserted
        into that window.
        """
        (tmp_path / "kstrl.toml").write_text('[inbox]\nopen_item_cap = "many"\n')

        outcome = drive_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )

        assert outcome.pipelines == []
        assert not (tmp_path / ".kstrl" / "runs").exists()


class TestTheFactorySideParseCountIsPinned:
    """The half ``TestTheParseCountDoesNotGrowWithComponents`` cannot see.

    That class drives the pipeline harness, so it counts nothing
    ``_run_factory_locked`` does before the pipeline exists. The
    duplicate that lived there was ``[autonomy]``, read once bare at run
    start and once inside ``RunEnvelope.load`` nine lines later: two
    parses of one file, and a nested ``toml_parse_scope`` does NOT
    collapse them, because the inner scope replaces the outer cache
    rather than inheriting it (measured: 3 loads across nested scopes
    still cost 2 parses). The envelope carries the ``AutonomyConfig``
    instead, so the section is resolved once and read off it.
    """

    def test_a_run_resolves_each_section_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kstrl import config as config_module

        original_loads = config_module.tomllib.loads
        counts = {"calls": 0, "parses": 0}
        original_doc = config_module.load_toml_document

        def counting_doc(path: Path) -> dict[str, Any]:
            counts["calls"] += 1
            return original_doc(path)

        def counting_loads(text: str, **kwargs: Any) -> dict[str, Any]:
            counts["parses"] += 1
            return original_loads(text, **kwargs)

        monkeypatch.setattr(config_module, "load_toml_document", counting_doc)
        monkeypatch.setattr(config_module.tomllib, "loads", counting_loads)
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))

        empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )

        assert (counts["calls"], counts["parses"]) == (13, 7), (
            "the cost of a run's config resolution moved. This is a "
            "census pin, not a performance budget: a number that grew "
            "means a section is being resolved twice, and the fix is to "
            "read it off something already resolved rather than to "
            "raise the pin. It fell from (15, 10) to (14, 9) when the "
            "second [autonomy] read went, and to (13, 7) when the four "
            "the pipeline used to resolve joined the envelope's single "
            "scope and the second [sandbox] read went with them (#192)."
        )


class TestTheLadderStateIsReadOnce:
    """``AutonomyState`` is JSON, so no parse count covers it.

    ``AutonomyState.load`` costs a measured 17.2 ms, most of it
    ``ensure_control_state``, against 0.02 ms for a section of
    kstrl.toml. It ran twice per ladder-on run and the first result was
    discarded; it also left a window in which a concurrent `ks autonomy
    promote` made the two reads disagree. Counted rather than asserted
    by structure, because the second read was nine lines from the first
    and reading the code is how it survived a review.
    """

    def test_a_ladder_on_run_loads_the_state_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kstrl import autonomy as autonomy_module

        original = autonomy_module.AutonomyState.load
        calls = 0

        def counting(root_dir: Path) -> Any:
            nonlocal calls
            calls += 1
            return original(root_dir)

        monkeypatch.setattr(autonomy_module.AutonomyState, "load", counting)
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\n[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        at_construction: list[int] = []

        empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
            on_pipeline=lambda: at_construction.append(calls),
        )

        assert at_construction == [1], (
            "the run read .kstrl/autonomy.json more than once before the "
            "pipeline existed. The envelope carries the state so the "
            f"ladder reads it off there. Got {at_construction}"
        )
        # The epilogue's read is a different question and is not this
        # one: `_record_autonomy_outcome` re-reads AFTER the run, because
        # apply_demotion may have written the file while components ran.
        assert calls == 2


class TestTheEnvelopeDoesNotOutliveItsRun:
    """Blocker 2 of the round-1 review, as a test.

    ``factory_config.policy_config`` is an INPUT: the injection seam a
    caller uses to hand the factory a policy it did not read. Round 1
    also wrote the clamped envelope back to it, unconditionally, which
    made it an output as well, so a second ``run_factory`` on the same
    ``FactoryConfig`` resolved nothing from kstrl.toml and recorded run
    one's hash as if it had. Measured: with the edit below, run 2
    enforced 5 where ``origin/main`` enforced 777.

    Latent in production today - every caller assembles a fresh
    ``FactoryConfig`` - which is exactly why it needs a test rather than
    an argument.
    """

    def test_a_reused_factory_config_still_reads_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        config = FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip")

        _, first = empty_run(tmp_path, config)
        (tmp_path / "kstrl.toml").write_text(
            BEFORE.format(autonomy="false").replace(
                "max_files_changed = 5", "max_files_changed = 777"
            )
        )
        manifest, second = empty_run(tmp_path, config)

        assert first.run_envelope.policy.max_files_changed == 5
        assert second.run_envelope.policy.max_files_changed == 777, (
            "the second run inherited the first run's envelope instead of "
            "reading kstrl.toml, which is #192's own defect one level up."
        )
        assert manifest.policy_hash == second.run_envelope.policy_hash()
        assert config.policy_config is None, (
            "run_factory wrote a resolved policy back onto the caller's "
            "FactoryConfig, which makes the injection seam an output too."
        )


class TestTheInjectionSeamReachesTheRun:
    """``FactoryConfig.policy_config`` and ``.fixtures_config`` are the
    seams a caller uses to hand the factory config it did not read.

    Measured by the round-1 review: deleting the ``policy_override``
    argument was STILL GREEN, so a caller injecting a ``PolicyConfig``
    would have had it silently ignored and nothing would have failed.
    That is a semantic substitution with no report, which is the failure
    CLAUDE.md names directly.
    """

    def test_an_injected_policy_is_what_the_run_enforces_and_records(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        injected = PolicyConfig(enabled=True, max_files_changed=42)

        manifest, pipeline = empty_run(
            tmp_path,
            FactoryConfig(
                use_worktrees=False,
                create_prs=False,
                review_mode="skip",
                policy_config=injected,
            ),
        )

        assert pipeline.run_envelope.policy is injected
        assert manifest.policy_hash == injected.envelope_hash()

    def test_an_injected_fixtures_config_reaches_phase_one(self, tmp_path: Path) -> None:
        from kstrl.fixtures import FixturesConfig

        (tmp_path / "kstrl.toml").write_text(
            BEFORE.format(autonomy="false") + "\n[fixtures]\nenabled = false\n"
        )
        injected = FixturesConfig(enabled=True)

        _, pipeline = empty_run(
            tmp_path,
            FactoryConfig(
                use_worktrees=False,
                create_prs=False,
                review_mode="skip",
                fixtures_config=injected,
            ),
        )

        assert pipeline.fixtures_config is injected


class TestTheFactoryHandsThePipelineWhatItRecords:
    def test_the_recorded_hash_is_the_pipeline_envelope(self, tmp_path: Path) -> None:
        """With ``[autonomy] enabled = false``, the default. At 414d662
        the assignment that gave the pipeline the resolved policy sat
        inside ``if autonomy_active:``, so on this path the pipeline was
        handed nothing and fell through to disk per component."""
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        manifest, pipeline = empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        assert manifest.policy_hash == pipeline.run_envelope.policy_hash()
        assert pipeline.run_envelope.policy.max_files_changed == 5

    def test_the_ladder_clamp_reaches_the_pipeline_and_the_hash(self, tmp_path: Path) -> None:
        """The ladder withholds ``deps_allow_new`` below L3. The clamped
        envelope must be the one the pipeline holds AND the one the hash
        covers, which is what ``manifest.policy_hash`` promises."""
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\ndeps_allow_new = true\n"
            "[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        manifest, pipeline = empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        assert pipeline.run_envelope.policy.deps_allow_new is False
        assert manifest.policy_hash == pipeline.run_envelope.policy_hash()
        assert manifest.policy_hash != PolicyConfig.load(tmp_path).envelope_hash()

    def test_phase_one_gets_the_clamped_autonomy_level(self, tmp_path: Path) -> None:
        """Finding (c), and it needs no file edit at all.

        ``resolve_runtime_level`` clamps by ``[autonomy] max_level``, by
        the policy envelope ceiling and by control-state location.
        Phase 1 and the set-point gate both read the RAW stored level.
        Measured at 414d662: stored L4, factory resolved L1, Phase 1
        used L4. No verdict changed at either level today, because both
        consumers test only ``>= 1``; it goes live the moment either
        threshold becomes level-graded.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\n[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        _, pipeline = empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        assert AutonomyState.load(tmp_path).level == 4
        assert pipeline.run_envelope.autonomy_level == 1

    def test_the_setpoint_gate_gets_the_same_clamped_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the expression, measured separately.

        ``_phase_verify`` and ``_setpoint_blocking`` held two copies of
        the level expression, and mutating both at once is caught by
        the parse count through the ``_phase_verify`` copy alone. Split
        into two mutations, the set-point copy was measured STILL GREEN
        against ``test_setpoint_agreement.py`` and ``test_review.py``:
        nothing drove it with a stored level that differed from the
        run's. The verdict cannot tell them apart either, because
        ``setpoint_blocks`` tests only ``>= 1``, so this records the
        level the gate is HANDED rather than what it decided.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[policy]\nenabled = true\n[autonomy]\nenabled = true\nmax_level = 1\n"
        )
        AutonomyState(level=4).save(tmp_path)
        _, pipeline = empty_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )
        import kstrl.pipeline as pipeline_module

        seen: list[int] = []

        def recording(config: FactoryConfig, level: int) -> bool:
            seen.append(level)
            return False

        monkeypatch.setattr(pipeline_module, "setpoint_blocks", recording)
        pipeline._setpoint_blocking()

        assert seen == [1], (
            "the set-point gate was handed the raw stored level rather "
            f"than the clamped one the run operates at. Got {seen}, "
            "expected [1] with kstrl.toml clamping L4 to L1."
        )
