"""A run resolves its config envelope once, and Phase 1 enforces that (#192).

``manifest.policy_hash`` is the audit record of what merge guardrails a
run was held to. Before this file it could disagree with what the run
actually enforced: the hash was taken once at run start while Phase 1
re-read ``[policy]``, ``[adequacy]`` and ``[autonomy]`` from
``kstrl.toml`` for every component. Measured at ``414d662`` on a
two-component run with the ladder off, which is the default: component B
was held to ``max_files_changed=500, deps_allow_new=true`` against a
manifest recording the hash of ``5`` and ``false``, the adequacy posture
flipped from enabled to disabled with nothing recording either posture,
and a malformed mid-run edit raised out of ``_phase_verify`` and aborted
the run.

A section the run cannot resolve is a refusal rather than a traceback,
and that half is here too: every section the envelope resolves is one
more that can reject in front of a run, and round 1 of this change had a
malformed ``[inbox]`` leave ``run_factory`` as an unhandled
``ValueError`` above the line that records the architect's spend.

``tests/test_config_guard.py`` is the static half - who may read
``kstrl.toml`` and from which scope. The two were one file until they
crossed the repo's 800-line limit.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyState
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, FactoryResult, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.policy import PolicyConfig
from kstrl.runenvelope import RunEnvelope
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerificationResult
from tests.helpers.component_prd import PASSING_STORY, write_component_prd
from tests.helpers.verify_phase import component, phase_verify_envelopes

# --- the behaviour the guard is about ------------------------------------

_BEFORE = """\
[policy]
enabled = true
max_files_changed = 5
max_lines_changed = 100
deps_allow_new = false

[adequacy]
enabled = true
min_coverage_pct = 90

[autonomy]
enabled = {autonomy}
"""

_AFTER = """\
[policy]
enabled = true
max_files_changed = 500
max_lines_changed = 100000
deps_allow_new = true

[adequacy]
enabled = false
min_coverage_pct = 0

[autonomy]
enabled = {autonomy}
"""

_MALFORMED = """\
[policy]
enabled = true
max_files_changed = "two"

[autonomy]
enabled = false
"""


def _edit(root: Path, body: str) -> Callable[[], None]:
    """The operator editing kstrl.toml while the run is in flight."""

    def write() -> None:
        (root / "kstrl.toml").write_text(body, encoding="utf-8")

    return write


class TestAMidRunEditDoesNotChangeWhatIsEnforced:
    @pytest.mark.parametrize("autonomy", ["false", "true"])
    def test_the_policy_envelope_is_the_recorded_one(self, tmp_path: Path, autonomy: str) -> None:
        """Measured at 414d662 with ``enabled = false``, which is the
        default: two distinct envelopes enforced in one run, and the
        second was not the one the hash records."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy=autonomy))
        envelope = RunEnvelope.load(tmp_path)
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=_edit(tmp_path, _AFTER.format(autonomy=autonomy)),
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
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy=autonomy))
        envelope = RunEnvelope.load(tmp_path)
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=_edit(tmp_path, _AFTER.format(autonomy=autonomy)),
            run_envelope=envelope,
        )

        assert {r.adequacy.enabled for r in readings} == {True}

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
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        comps = [component("comp-a"), component("comp-b")]

        readings = phase_verify_envelopes(
            tmp_path,
            comps,
            between=_edit(tmp_path, _MALFORMED),
        )

        assert [r.component for r in readings] == ["comp-a", "comp-b"]
        assert {r.policy.max_files_changed for r in readings} == {5}


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
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        comps = self._components()
        verified: list[str] = []

        def verify(*_args: Any, **kwargs: Any) -> VerificationResult:
            verified.append(str(kwargs["component_id"]))
            if len(verified) == 1:
                (tmp_path / "kstrl.toml").write_text(_MALFORMED, encoding="utf-8")
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
            outcome = _drive_run(
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

        outcome = _drive_run(
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

        outcome = _drive_run(
            tmp_path,
            FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip"),
        )

        assert outcome.pipelines == []
        assert not (tmp_path / ".kstrl" / "runs").exists()


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
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
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


# --- the factory half: the hash is taken off what the pipeline enforces ---


def _init_git_repo(root: Path) -> None:
    """A repo the diff phase can read."""

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "tester")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "base")


@dataclass(frozen=True)
class RunOutcome:
    """Everything one ``run_factory`` left behind that these tests read."""

    manifest: Manifest
    result: FactoryResult
    pipelines: list[ComponentPipeline]
    narration: str


def _drive_run(
    tmp_path: Path,
    config: FactoryConfig,
    *,
    components: Sequence[Component] = (),
    on_pipeline: Callable[[], None] | None = None,
) -> RunOutcome:
    """Drive the REAL ``run_factory`` and hand back what it produced.

    With no components the subject is everything ``run_factory`` does
    BEFORE scheduling: resolve the envelope, refuse a section it cannot
    read, clamp with the autonomy ladder, hand the result to the
    pipeline and record its hash.

    With components the scheduler, ``process_result`` and the ``try``
    around ``future.result()`` are all entered, which is what makes a
    mid-run failure a failure of the RUN rather than of one call. The
    engineer is stubbed at ``kstrl.factory._run_component`` because the
    subject is the phase chain around it, not the loop.
    """
    scripts = tmp_path / "scripts" / "kstrl"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "prompt.md").write_text("p", encoding="utf-8")
    (scripts / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    for comp in components:
        write_component_prd(tmp_path, comp.prd_path, stories=[PASSING_STORY])
    if components:
        # Without a real repo the diff phase fails as infrastructure and
        # no component reaches a terminal verdict, so the run's exit code
        # would say nothing about the config edit under test.
        _init_git_repo(tmp_path)
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=list(components),
    )
    manifest.save(tmp_path / "manifest.json")
    built: list[ComponentPipeline] = []
    real = ComponentPipeline

    def capture(**kwargs: Any) -> ComponentPipeline:
        # ``on_pipeline`` runs at CONSTRUCTION, which is the boundary
        # between what a run resolves for itself and what its epilogue
        # re-reads afterwards. A caller counting run-start work needs
        # that line and cannot get it from the return value.
        if on_pipeline is not None:
            on_pipeline()
        pipeline = real(**kwargs)
        built.append(pipeline)
        return pipeline

    narration = io.StringIO()
    with patch("kstrl.factory.ComponentPipeline", side_effect=capture):
        result = run_factory(
            manifest,
            config,
            KstrlConfig(
                prompt_file=scripts / "prompt.md",
                prd_file=scripts / "prd.json",
                sleep_seconds=0,
                agent_cmd="echo test",
            ),
            PlainUI(no_color=True, file=narration),
            tmp_path,
        )
    return RunOutcome(manifest, result, built, narration.getvalue())


def _empty_run(
    tmp_path: Path,
    config: FactoryConfig,
    *,
    on_pipeline: Callable[[], None] | None = None,
) -> tuple[Manifest, ComponentPipeline]:
    """:func:`_drive_run` for the tests that only want the pipeline."""
    outcome = _drive_run(tmp_path, config, on_pipeline=on_pipeline)
    assert len(outcome.pipelines) == 1
    return outcome.manifest, outcome.pipelines[0]


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
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))

        _empty_run(
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

        _empty_run(
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
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        config = FactoryConfig(use_worktrees=False, create_prs=False, review_mode="skip")

        _, first = _empty_run(tmp_path, config)
        (tmp_path / "kstrl.toml").write_text(
            _BEFORE.format(autonomy="false").replace(
                "max_files_changed = 5", "max_files_changed = 777"
            )
        )
        manifest, second = _empty_run(tmp_path, config)

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


class TestTheFactoryHandsThePipelineWhatItRecords:
    def test_the_recorded_hash_is_the_pipeline_envelope(self, tmp_path: Path) -> None:
        """With ``[autonomy] enabled = false``, the default. At 414d662
        the assignment that gave the pipeline the resolved policy sat
        inside ``if autonomy_active:``, so on this path the pipeline was
        handed nothing and fell through to disk per component."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        manifest, pipeline = _empty_run(
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
        manifest, pipeline = _empty_run(
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
        _, pipeline = _empty_run(
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
        _, pipeline = _empty_run(
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


class TestRunEnvelope:
    """The unit the two halves above share."""

    def test_it_reads_the_three_sections_the_hash_and_the_gates_use(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        envelope = RunEnvelope.load(tmp_path)
        assert envelope.policy.max_files_changed == 5
        assert envelope.adequacy.enabled is True
        assert envelope.autonomy.enabled is False
        assert envelope.autonomy_level == 0
        assert envelope.policy_hash() == envelope.policy.envelope_hash()

    def test_the_stored_level_is_read_only_when_the_ladder_is_on(self, tmp_path: Path) -> None:
        """0 when the ladder is off is not a default, it is the same
        expression ``_phase_verify`` used to evaluate per component."""
        AutonomyState(level=3).save(tmp_path)
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = false\n")
        assert RunEnvelope.load(tmp_path).autonomy_level == 0
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\nmax_level = 4\n")
        assert RunEnvelope.load(tmp_path).autonomy_level == 3

    def test_the_policy_override_wins_over_the_file(self, tmp_path: Path) -> None:
        """``FactoryConfig.policy_config`` is the injection seam callers
        already use; the envelope keeps it rather than adding a second."""
        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        injected = PolicyConfig(enabled=True, max_files_changed=42)
        envelope = RunEnvelope.load(tmp_path, policy_override=injected)
        assert envelope.policy is injected
        assert envelope.adequacy.enabled is True

    def test_it_is_frozen(self, tmp_path: Path) -> None:
        """The factory clamps it with ``replace``; nothing edits one in
        place, so a holder cannot have it changed underneath."""
        import dataclasses

        (tmp_path / "kstrl.toml").write_text(_BEFORE.format(autonomy="false"))
        envelope = RunEnvelope.load(tmp_path)
        with pytest.raises(dataclasses.FrozenInstanceError):
            envelope.autonomy_level = 4  # type: ignore[misc]
