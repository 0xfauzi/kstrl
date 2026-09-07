"""A run resolves its config envelope once, and Phase 1 enforces that (#192).

``manifest.policy_hash`` is the audit record of what merge guardrails a
run was held to. Before this file it could disagree with what the run
actually enforced: the hash was taken once at run start while Phase 1
re-read ``[policy]``, ``[adequacy]`` and ``[autonomy]`` from
``kstrl.toml`` for every component. Measured at ``414d662`` on a
two-component run with the ladder off, which is the default: component B
was held to ``max_files_changed=500, deps_allow_new=true`` against a
manifest recording the hash of ``max_files_changed=5,
deps_allow_new=false``, the adequacy posture flipped from enabled to
disabled with nothing recording either posture, and a malformed mid-run
edit raised out of ``_phase_verify`` and aborted the run.

This is the PHASE half: what one component was held to, driven through
the real ``_phase_verify``, plus the unit tests for the envelope itself.
``tests/test_run_envelope.py`` is the RUN half, which drives
``run_factory``; ``tests/test_config_guard.py`` is the STATIC half.
Three files rather than one because together they are past the repo's
800-line limit.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kstrl.autonomy import AutonomyState
from kstrl.policy import PolicyConfig
from kstrl.runenvelope import RunEnvelope
from tests.helpers.run_config import AFTER, BEFORE, MALFORMED, edit
from tests.helpers.verify_phase import component, phase_verify_envelopes


class TestAMidRunEditDoesNotChangeWhatIsEnforced:
    @pytest.mark.parametrize("autonomy", ["false", "true"])
    def test_the_policy_envelope_is_the_recorded_one(self, tmp_path: Path, autonomy: str) -> None:
        """Measured at 414d662 with ``enabled = false``, which is the
        default: two distinct envelopes enforced in one run, and the
        second was not the one the hash records."""
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy=autonomy))
        envelope = RunEnvelope.load(tmp_path)
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=edit(tmp_path, AFTER.format(autonomy=autonomy)),
            run_envelope=envelope,
        )

        assert [r.component for r in readings] == ["comp-a", "comp-b"]
        assert {r.policy.envelope_hash() for r in readings} == {envelope.policy_hash()}
        assert {r.policy.max_files_changed for r in readings} == {5}
        assert {r.policy.deps_allow_new for r in readings} == {False}

    @pytest.mark.parametrize("autonomy", ["false", "true"])
    def test_the_adequacy_posture_is_the_one_the_run_started_with(
        self, tmp_path: Path, autonomy: str
    ) -> None:
        """AdequacyConfig has no envelope hash and is in no artifact, so
        at 414d662 the posture a component was judged under flipped with
        nothing anywhere recording that it had."""
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy=autonomy))
        envelope = RunEnvelope.load(tmp_path)
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=edit(tmp_path, AFTER.format(autonomy=autonomy)),
            run_envelope=envelope,
        )

        assert {r.adequacy.enabled for r in readings} == {True}

    def test_phase_one_is_handed_the_envelope_level_not_the_stored_one(
        self, tmp_path: Path
    ) -> None:
        """The level Phase 1 ENFORCES, recorded rather than inferred.

        ``test_phase_one_gets_the_clamped_autonomy_level`` asserts on the
        pipeline's envelope, which is one step short: a mutation putting
        ``AutonomyState.load(self.root_dir).level`` back inside
        ``_phase_verify`` leaves that envelope correct and was measured
        STILL GREEN against it. This records what the verifier was
        handed, with the stored level deliberately different.
        """
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="true"))
        AutonomyState(level=4).save(tmp_path)
        clamped = replace(RunEnvelope.load(tmp_path), autonomy_level=1)

        readings = phase_verify_envelopes(tmp_path, [component("comp-a")], run_envelope=clamped)

        assert AutonomyState.load(tmp_path).level == 4
        assert {r.autonomy_level for r in readings} == {1}

    def test_a_malformed_mid_run_edit_does_not_abort_phase_one(self, tmp_path: Path) -> None:
        """The failure mode is DELETED, not handled.

        At 414d662 an edit the entry preflight would have rejected
        reached a per-component load with nothing in front of it and
        raised ``ValueError`` out of ``_phase_verify``. There is no read
        left to raise from, so no handler is added.

        This drives ``_phase_verify`` directly, which is necessary and
        is NOT the claim that a RUN survives the edit:
        ``TestAMalformedMidRunEditDoesNotAbortTheRun`` drives the
        scheduler for that, because it is ``process_result`` running
        outside the ``try`` around ``future.result()`` that turned one
        bad edit into a dead run.
        """
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=edit(tmp_path, MALFORMED),
        )

        assert [r.component for r in readings] == ["comp-a", "comp-b"]
        assert {r.policy.max_files_changed for r in readings} == {5}


class TestTheParseCountDoesNotGrowWithComponents:
    """Acceptance 4 as a measurement rather than a claim.

    TWO numbers, because they are two different claims and an earlier
    draft of this test conflated them. ``load_toml_document`` CALLS is
    how many times something asked for the document; ``tomllib.loads``
    is how many times the bytes were actually lexed, since
    ``load_toml_document`` serves a repeat inside a
    ``toml_parse_scope`` from the scope. Counting the wrapper and
    calling the answer a parse count over-reports by exactly the number
    of reads the scope already collapsed.

    Measured at 414d662 by the same two methods: 5 calls and 5 parses
    for 1 component, 9 and 9 for 2, 17 and 17 for 4, 33 and 33 for 8.
    They agreed at the base because nothing on that path was inside a
    scope; Phase 1 added 4 of each per component.
    """

    @staticmethod
    def _counts(tmp_path: Path, count: int, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
        from kstrl import config as config_module

        original = config_module.load_toml_document
        original_loads = config_module.tomllib.loads
        calls = 0
        parses = 0

        def counting(path: Path) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return original(path)

        def counting_loads(text: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal parses
            parses += 1
            return original_loads(text, **kwargs)

        monkeypatch.setattr(config_module, "load_toml_document", counting)
        monkeypatch.setattr(config_module.tomllib, "loads", counting_loads)
        comps = [component(f"comp-{index}") for index in range(count)]
        phase_verify_envelopes(tmp_path, comps)
        return calls, parses

    def test_one_component_and_eight_cost_the_same(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        one = self._counts(tmp_path, 1, monkeypatch)
        eight = self._counts(tmp_path, 8, monkeypatch)
        assert one == eight, (
            f"1 component cost {one} (calls, parses) and 8 cost {eight}, "
            "so the config cost still grows with the component count."
        )
        # Stated rather than left as "equal". 7 calls, all seven inside
        # ``RunEnvelope.load``; 1 parse, because they share one
        # ``toml_parse_scope``, which is the whole point of the block.
        # It was (7, 2) while ``ComponentPipeline.__init__`` still
        # resolved four of them in a scope of its own.
        assert one == (7, 1)


class TestRunEnvelope:
    """The unit the two halves above share."""

    def test_it_reads_the_three_sections_the_hash_and_the_gates_use(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        envelope = RunEnvelope.load(tmp_path)
        assert envelope.policy.max_files_changed == 5
        assert envelope.adequacy.enabled is True
        assert envelope.autonomy.enabled is False
        assert envelope.autonomy_level == 0
        assert envelope.policy_hash() == envelope.policy.envelope_hash()

    def test_the_stored_level_is_read_only_when_the_ladder_is_on(self, tmp_path: Path) -> None:
        """0 when the ladder is off is not a default, it is the same
        expression ``_phase_verify`` used to evaluate per component.

        The name says READ, so the state field is asserted beside the
        level. Round 2 of #192 made the name false while the assertion
        stayed true: the level was zeroed and the file was read anyway,
        and a reader takes the name for the mechanism.
        ``tests/test_run_envelope.py`` counts the loads on a whole run.
        """
        AutonomyState(level=3).save(tmp_path)
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = false\n")
        off = RunEnvelope.load(tmp_path)
        assert off.autonomy_level == 0
        assert off.autonomy_state is None
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\nmax_level = 4\n")
        on = RunEnvelope.load(tmp_path)
        assert on.autonomy_level == 3
        assert on.autonomy_state is not None

    def test_the_policy_override_wins_over_the_file(self, tmp_path: Path) -> None:
        """``FactoryConfig.policy_config`` is the injection seam callers
        already use; the envelope keeps it rather than adding a second."""
        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        injected = PolicyConfig(enabled=True, max_files_changed=42)
        envelope = RunEnvelope.load(tmp_path, policy_override=injected)
        assert envelope.policy is injected
        assert envelope.adequacy.enabled is True

    def test_it_is_frozen(self, tmp_path: Path) -> None:
        """The factory clamps it with ``replace``; nothing edits one in
        place, so a holder cannot have it changed underneath."""
        import dataclasses

        (tmp_path / "kstrl.toml").write_text(BEFORE.format(autonomy="false"))
        envelope = RunEnvelope.load(tmp_path)
        with pytest.raises(dataclasses.FrozenInstanceError):
            envelope.autonomy_level = 4  # type: ignore[misc]
