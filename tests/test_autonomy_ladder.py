"""R8.2 autonomy ladder tests, kept to the parts that exercise real state.

Persistence (`TestPersistence`) round-trips `AutonomyState` through real
tmp_path files, including the missing/corrupt/out-of-range fallbacks to
L1 and the "no flags in the saved payload" contract. Config loading
(`TestConfig`) covers `AutonomyConfig.load` reading a real `kstrl.toml`
and an env override. Threshold replay (`TestReplay`) covers
`replay_file`/`load_runs` against real experiment files: a missing
file, an undecodable one, a torn row from a crash mid-write (#331), and
a loaded run feeding back through `replay` without mutating stored
state. Envelope-ceiling clamping (`TestEnvelopeCeiling`) drives
`resolve_runtime_level` against a real XDG control-state directory
(#195, R8.9). The rest (`TestFactoryWiring`, `TestBundleClampsPolicy`,
`TestRunOutcomesReachState`) runs the ladder through a real
`run_factory` call with a stubbed agent: the stored level overriding a
contradicting config, evidence accumulating across runs, and a policy
violation demoting and reaching the evolution journal.
`TestTransitionAudit` covers `commit_transition` writing that journal
for real, including a real OSError and a real malformed-config path
(#257).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.autonomy import (
    DEMOTION_COOLDOWN_RUNS,
    MIN_DECISIVE_RUNS,
    AutonomyConfig,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
)
from kstrl.autonomy_replay import load_runs, replay, replay_file
from tests.helpers import gitrepo
from tests.helpers.component_prd import write_component_prd
from tests.helpers.plan_approval import approve_plan
from tests.helpers.replay import UNDECODABLE_TSV
from tests.helpers.stack_confirmation import in_process_stack


def _eligible_state(level: AutonomyLevel = AutonomyLevel.L1_SUPERVISED) -> AutonomyState:
    """A state that satisfies every criterion for the next level."""
    state = AutonomyState(level=int(level))
    state.decisive_runs_at_level = MIN_DECISIVE_RUNS
    state.components_merged_at_level = 50
    state.clean_merges_at_level = 50
    return state


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        state = _eligible_state()
        state.promote(actor="human", ack="evidence reviewed", signal_blockers=())
        state.record_merged_component(human_edited=False)
        state.save(tmp_path)
        loaded = AutonomyState.load(tmp_path)
        assert loaded.level == state.level
        assert loaded.last_promoted_by == "human"
        assert loaded.components_merged_at_level == 1
        assert len(loaded.history) == 1
        assert loaded.history[0].direction == "promote"

    def test_missing_file_defaults_to_l1(self, tmp_path: Path) -> None:
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)

    def test_corrupt_file_falls_back_to_l1(self, tmp_path: Path) -> None:
        # Unknown autonomy must resolve to the LEAST autonomy.
        path = AutonomyState.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)

    def test_out_of_range_level_falls_back_to_l1(self, tmp_path: Path) -> None:
        path = AutonomyState.path_for(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"level": 99}))
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L1_SUPERVISED)

    def test_saved_payload_has_no_flags(self, tmp_path: Path) -> None:
        AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO)).save(tmp_path)
        data = json.loads(AutonomyState.path_for(tmp_path).read_text())
        assert data["level"] == 3
        for forbidden in ("pause_before_pr_merge", "auto_merge_when_green", "flags"):
            assert forbidden not in data


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------
class TestConfig:
    def test_load_reads_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\nmax_level = 2\n")
        config = AutonomyConfig.load(tmp_path)
        assert config.enabled is True and config.max_level == 2

    def test_env_overrides_toml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = false\n")
        monkeypatch.setenv("KSTRL_AUTONOMY_ENABLED", "1")
        assert AutonomyConfig.load(tmp_path).enabled is True


# --------------------------------------------------------------------------
# Threshold replay
# --------------------------------------------------------------------------
class TestReplay:
    def test_missing_experiments_file_is_not_an_error(self, tmp_path: Path) -> None:
        report = replay_file(tmp_path / "nope.tsv")
        assert report.total_runs == 0
        assert report.sufficient_data is False

    def test_an_unreadable_experiments_file_is_a_refusal_not_no_runs(
        self,
        tmp_path: Path,
    ) -> None:
        """#352: an empty read is a silent removal of the mechanism.

        A decode error and a permission error used to reach the
        operator as "INSUFFICIENT DATA", which is a sentence about the
        project's history rather than about the file. The exception goes
        to the caller now, and ``ks autonomy replay`` names the cause
        above the same exit code.

        A byte that is no valid utf-8 sequence is the reachable half of
        this on any machine; the mode-0200 half needs a permission the
        superuser ignores, so it is not the one asserted here.
        """
        path = tmp_path / "experiments.tsv"
        path.write_bytes(UNDECODABLE_TSV)

        with pytest.raises(ValueError):
            load_runs(path)

    def test_replay_never_mutates_stored_state(self, tmp_path: Path) -> None:
        # Reading a replay is a report, never a transition.
        AutonomyState(level=int(AutonomyLevel.L2_GATED_MERGE)).save(tmp_path)
        replay_file(tmp_path / "nope.tsv", tmp_path)
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_loads_real_tsv_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "experiments.tsv"
        path.write_text(
            "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
            "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\t"
            "total_tokens\ttotal_cost_usd\tunreported_calls\n"
            "factory-1\t2026-07-20T14:23:31Z\tslugify\t1\t0\t1\t0\t1.00\t246.1\t"
            "1.00\tpr:failed-to-create-pr\t30204\t0.5\t0\n"
            "factory-2\t2026-07-20T16:08:20Z\tslugify\t2\t2\t0\t0\t1.00\t666.5\t"
            "0.50\t\t7558457\t5.28\t0\n"
        )
        runs = load_runs(path)
        assert len(runs) == 2
        assert runs[0].infra_aborted is True  # pr: prefix
        assert runs[1].decisive is True
        assert runs[1].merged is None  # #601: this row predates merge evidence
        report = replay(runs)
        assert report.decisive_runs == 1
        assert report.infra_aborted_runs == 1
        # #601: this 14-column row predates merged/clean_merged, so it
        # contributes 0 KNOWN merges (not its `completed` count of 2) and
        # is counted as unpredictable rather than silently read as zero.
        assert report.components_merged == 0
        assert report.merged_unknown_runs == 1

    def test_a_torn_row_is_not_a_run_the_ladder_promotes_on(self, tmp_path: Path) -> None:
        """#331's read half, on the SECOND reader of experiments.tsv.

        A crash mid-write left a row torn, the next ``record_run``
        appended onto it, and ``csv.DictReader`` zipped the
        concatenation against the header: a run id from the fragment,
        this run's fields shifted along by however many columns the
        fragment held, and ``_as_int`` turning each of them into 0
        rather than raising. That is a fabricated run in the population
        a promotion is decided on, and it is silent.

        The torn row here is written the way a crash writes one, and the
        real ``record_run`` appends onto it, so this measures the reader
        against bytes the writer actually produces.
        """
        from kstrl.factory import FactoryResult
        from kstrl.manifest import Manifest
        from tests.helpers.journal import journal_at, tear

        journal = journal_at(tmp_path)
        manifest = Manifest(
            version="1",
            spec_file="",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[],
        )
        journal.record_run("run-1", manifest, FactoryResult())
        tear(journal.config.experiments_path)
        journal.record_run("run-2", manifest, FactoryResult())

        runs = load_runs(journal.config.experiments_path)

        assert [run.run_id for run in runs] == ["run-1", "run-2"]


# --------------------------------------------------------------------------
# Factory wiring: levels drive the flag bundle ("Done when")
# --------------------------------------------------------------------------
def _init_git_repo(root: Path) -> None:
    """A real repo: without one the diff phase fails as infrastructure and
    no component can reach a terminal verdict."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    run("init")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("base\n")
    run("add", ".")
    run("commit", "-m", "base")


def _run_factory_with_autonomy(
    tmp_path: Path,
    level: AutonomyLevel,
    *,
    enabled: bool,
    configured_pause: bool,
    policy_enabled: bool = True,
    findings: list[object] | None = None,
    succeed: bool = True,
) -> object:
    """Run the factory against a stored level; return the FactoryConfig."""
    from kstrl.config import KstrlConfig
    from kstrl.factory import ComponentResult, FactoryConfig, run_factory
    from kstrl.manifest import Component, Manifest
    from kstrl.ui.plain import PlainUI
    from kstrl.verify import CheckResult, VerificationResult, VerifyConfig

    _init_git_repo(tmp_path)
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    # Without it the run is refused before it starts (#293 review): a
    # component whose pre-run PRD will not read has no plan-time scope.
    write_component_prd(tmp_path, "scripts/kstrl/feature/comp-a/prd.json")
    (tmp_path / "kstrl.toml").write_text(
        f"[autonomy]\nenabled = {'true' if enabled else 'false'}\n"
        f"[policy]\nenabled = {'true' if policy_enabled else 'false'}\n"
    )
    AutonomyState(level=int(level)).save(tmp_path)

    manifest = Manifest(
        version="1",
        spec_file="",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "Component A",
                "Desc",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            )
        ],
    )
    # #602: these tests measure the ladder's evidence, not the plan gate,
    # so the plan is approved first; L1 would otherwise park it.
    approve_plan(tmp_path, manifest)
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        pause_before_pr_merge=configured_pause,
        project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
        verify_config=VerifyConfig(
            project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    result = ComponentResult("comp-a", success=succeed, iterations=1)
    # Findings reach the component the way they do in production: a
    # policy CheckResult carries them and the pipeline lifts them into
    # the finding stream (R8.1). Pre-seeding the manifest would not
    # survive - begin_attempt clears that stream.
    if findings:
        blocking = [f for f in findings if getattr(f, "severity", "") != "advisory"]
        verification = VerificationResult(
            passed=not blocking,
            checks=[
                CheckResult(
                    "policy_envelope",
                    not blocking,
                    "policy",
                    findings=list(findings),  # type: ignore[arg-type]
                )
            ],
        )
    else:
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("diff_scope", True, "ok")],
        )
    # The ladder forces review_mode="hard" at every level, so the review
    # phase always runs; stub it green so these tests measure the LADDER,
    # not the reviewer.
    from kstrl.review import ReviewResult

    with (
        patch("kstrl.factory._run_component", return_value=result),
        patch(
            "kstrl.factory.run_mechanical_verification",
            return_value=verification,
        ),
        patch(
            "kstrl.factory.run_review",
            return_value=ReviewResult(passed=True, mode="hard"),
        ),
    ):
        run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)
    return config


class TestFactoryWiring:
    def test_level_drives_flags_over_contradicting_config(
        self,
        tmp_path: Path,
    ) -> None:
        # L1 demands the merge gate ON; config says off. Bundle must win.
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L1_SUPERVISED,
            enabled=True,
            configured_pause=False,
        )
        assert config.pause_before_pr_merge is True  # type: ignore[attr-defined]
        assert config.review_mode == "hard"  # type: ignore[attr-defined]

    def test_l3_drops_a_gate_the_operator_did_not_ask_for(self, tmp_path: Path) -> None:
        """#195 narrowed what this case means, and it is still a case.

        ``_run_factory_with_autonomy`` builds ``FactoryConfig(...)`` by
        hand, so its ``pause_before_pr_merge=True`` carries no
        provenance: nobody wrote it in kstrl.toml, in the environment or
        on the command line. A True like that is still the ladder's to
        drop at L3. An EXPLICIT one is not, and
        ``tests/test_explicit_merge_gate.py`` is where that is measured,
        through the real config sources.
        """
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=True,
        )
        assert config.pause_before_pr_merge is False  # type: ignore[attr-defined]
        assert config.explicit_fields == frozenset()  # type: ignore[attr-defined]

    def test_disabled_ladder_leaves_config_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        # Opt-in: with [autonomy] off, the stored level changes nothing.
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=False,
            configured_pause=True,
        )
        assert config.pause_before_pr_merge is True  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Review regressions (PR #174)
# --------------------------------------------------------------------------
class TestEnvelopeCeiling:
    """L3 is *Enveloped* auto-merge: no envelope, no auto-merge."""

    def test_l3_clamps_to_l2_without_policy_envelope(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        level, notes = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True),
            policy_enabled=False,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L2_GATED_MERGE
        assert any("requires the R8.1 policy envelope" in n for n in notes)

    def test_l4_clamps_to_l2_without_policy_envelope(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        level, _ = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True),
            policy_enabled=False,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L2_GATED_MERGE

    def test_l3_allowed_with_envelope(self, tmp_path: Path) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        level, notes = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True),
            policy_enabled=True,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L3_ENVELOPED_AUTO
        assert notes == []

    def test_lowest_ceiling_wins(self, tmp_path: Path) -> None:
        from kstrl.autonomy import resolve_runtime_level

        state = AutonomyState(level=int(AutonomyLevel.L4_DEPLOY))
        level, notes = resolve_runtime_level(
            state,
            AutonomyConfig(enabled=True, max_level=3),
            policy_enabled=False,
            root_dir=tmp_path,
        )
        assert level is AutonomyLevel.L2_GATED_MERGE  # envelope beats max_level
        assert len(notes) == 2

    def test_merge_gate_stays_on_when_envelope_disabled(
        self,
        tmp_path: Path,
    ) -> None:
        # The end-to-end version: an L3 repo with [policy] off must NOT
        # get auto-merge.
        config = _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=False,
            policy_enabled=False,
        )
        assert config.pause_before_pr_merge is True  # type: ignore[attr-defined]


class TestRunOutcomesReachState:
    """A run must actually move the ladder's counters (not just in tests)."""

    # test_successful_run_records_evidence (a create_prs=False run is
    # decisive and merges nothing, #601) was deleted here: it asserted
    # the same predicate as
    # tests/test_ladder_merge_evidence.py::test_a_part_completed_without_a_pr_is_not_a_merge,
    # which drives a real run_factory rather than the lighter
    # _run_factory_with_autonomy helper (#601 simplify pass).

    def test_evidence_accumulates_across_runs(self, tmp_path: Path) -> None:
        for _ in range(3):
            _run_factory_with_autonomy(
                tmp_path,
                AutonomyLevel.L1_SUPERVISED,
                enabled=True,
                configured_pause=True,
            )
            # Helper rewrites state each call, so re-seed from disk:
            state = AutonomyState.load(tmp_path)
            state.save(tmp_path)
        assert AutonomyState.load(tmp_path).decisive_runs_at_level >= 1

    def test_disabled_ladder_records_nothing(self, tmp_path: Path) -> None:
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L1_SUPERVISED,
            enabled=False,
            configured_pause=True,
        )
        assert AutonomyState.load(tmp_path).decisive_runs_at_level == 0

    def test_policy_violation_demotes_and_journals(self, tmp_path: Path) -> None:
        from kstrl.findings import Finding

        violation = Finding.policy_violation(
            category="paths_deny",
            explanation="touched a denied path",
        )
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=False,
            policy_enabled=True,
            findings=[violation],
        )
        reloaded = AutonomyState.load(tmp_path)
        assert reloaded.level == int(AutonomyLevel.L2_GATED_MERGE)
        assert reloaded.cooldown_runs_remaining == DEMOTION_COOLDOWN_RUNS
        assert reloaded.history[-1].trigger == "policy_violation"
        # ... and the transition reached the evolution journal.
        journal = (tmp_path / ".kstrl" / "evolution.jsonl").read_text()
        assert '"event_type":"autonomy_transition"' in journal
        assert '"direction":"demote"' in journal

    def test_advisory_finding_does_not_demote(self, tmp_path: Path) -> None:
        from kstrl.findings import Finding

        advisory = Finding.policy_violation(
            category="max_lines_changed",
            explanation="too many lines",
            severity="advisory",
        )
        _run_factory_with_autonomy(
            tmp_path,
            AutonomyLevel.L3_ENVELOPED_AUTO,
            enabled=True,
            configured_pause=False,
            policy_enabled=True,
            findings=[advisory],
        )
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L3_ENVELOPED_AUTO)


class TestTransitionAudit:
    """Every committed transition reaches the durable audit stream."""

    def test_commit_transition_writes_journal(self, tmp_path: Path) -> None:
        from kstrl.autonomy import commit_transition

        state = _eligible_state()
        record = state.promote(actor="human", ack="reviewed", signal_blockers=())
        commit_transition(state, record, tmp_path, run_id="run-1")
        journal = (tmp_path / ".kstrl" / "evolution.jsonl").read_text()
        assert '"event_type":"autonomy_transition"' in journal
        assert '"direction":"promote"' in journal
        assert '"actor":"human"' in journal
        # State was saved too, not just journaled.
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_commit_transition_emits_event_when_in_a_run(
        self,
        tmp_path: Path,
    ) -> None:
        from kstrl.autonomy import commit_transition
        from kstrl.event_catalog import AutonomyTransition
        from kstrl.events import EventBus

        seen: list[object] = []

        class _Collector:
            def emit(self, event: object) -> None:
                seen.append(event)

            def close(self) -> None:
                return None

        bus = EventBus(_Collector(), run_id="run-1")  # type: ignore[arg-type]
        state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
        record = state.demote(DemotionTrigger.HEALTH_BREACH, "breach")
        assert record is not None
        commit_transition(state, record, tmp_path, bus=bus, run_id="run-1")
        assert any(isinstance(e, AutonomyTransition) for e in seen)

    def test_journal_failure_is_not_fatal(self, tmp_path: Path) -> None:
        """Losing the log must never strand the ladder unsaved.

        A real failure on a real path (a directory where the journal
        file should be) rather than a patched ``kstrl.autonomy.open``,
        which stopped intercepting anything when #312 moved the write
        into ``EvolutionJournal.append_entries``. A patched builtin pins
        WHERE the code writes, which is not what this test is about.
        """
        from kstrl.autonomy import commit_transition
        from kstrl.evolution import EvolutionConfig

        journal_path = EvolutionConfig.load(tmp_path).journal_path
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.mkdir()

        state = _eligible_state()
        record = state.promote(actor="human", ack="ok", signal_blockers=())
        with pytest.warns(RuntimeWarning, match="journal append failed"):
            commit_transition(state, record, tmp_path)
        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)

    def test_an_unparseable_journal_config_is_not_fatal_either(
        self,
        tmp_path: Path,
    ) -> None:
        """#257 sweep: the guard here caught only OSError, and
        ``EvolutionConfig.load`` raises ValueError on a malformed
        [evolution] section. The state save has already happened by
        then, so a typo in an unrelated knob would leave the ladder
        saved and unjournaled - exactly the drift this function exists
        to prevent.
        """
        from kstrl.autonomy import commit_transition

        (tmp_path / "kstrl.toml").write_text("[evolution\nenabled = true\n")
        state = _eligible_state()
        record = state.promote(actor="human", ack="ok", signal_blockers=())

        with pytest.warns(RuntimeWarning, match="Evolution config unreadable"):
            commit_transition(state, record, tmp_path)

        assert AutonomyState.load(tmp_path).level == int(AutonomyLevel.L2_GATED_MERGE)
