"""R2.1/R2.2 config control plane tests.

Every phase config must resolve through the real CLI construction path
with precedence: explicit CLI flag > env var > kstrl.toml > dataclass
default. The tests here invoke the actual click commands (``kstrl
factory`` / ``ks run`` / ``ks evolve`` / ``ks init``) with
``run_factory`` (or ``EvolutionJournal``) replaced by a capturing fake,
so the assertion target is the exact config object the orchestrator
would receive - not a loader called in isolation.

Nine config surfaces map to kstrl.toml sections:
KstrlConfig (agent/run/paths/git/ui), TimeoutConfig ([timeout]),
KnowledgeConfig ([knowledge]), FactoryConfig ([factory]), VerifyConfig
([verify]), SecurityConfig ([security]), ContractConfig ([contract]),
CodebaseScanConfig ([codebase_scan]), EvolutionConfig ([evolution]).
KnowledgeConfig is consumed inside run_factory (factory.py calls
``KnowledgeConfig.load(root_dir)``), so its CLI-path coverage here is
the loader round-trip plus the new ``from_env``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
import kstrl.evolution as evolution_mod
from kstrl.evolution import EvolutionConfig
from kstrl.factory import FactoryConfig
from kstrl.feedforward import CodebaseScanConfig
from kstrl.knowledge import KnowledgeConfig
from kstrl.verify import VerifyConfig

# The harness moved to tests/helpers/factorycli.py on #195, when
# tests/test_explicit_merge_gate.py needed the same three pieces to prove
# that --pause-before-pr-merge is recorded as an explicit request.
# invoke_factory is imported under this file's existing private name so
# its 31 call sites read unchanged and the alias cannot outlive the
# import. write_manifest is not imported: invoke_factory writes its own.
from tests.helpers.factorycli import capture_run_factory
from tests.helpers.factorycli import invoke_factory as _invoke_factory
from tests.helpers.stack_confirmation import confirm_stack, write_stack

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace run_factory with a capturing fake; return the capture dict."""
    return capture_run_factory(monkeypatch)


def _invoke_run(tmp_path: Path, *extra_args: str, no_verify: bool = True) -> Any:
    # R2.4: `ks run` preflights prd.json before run_factory, so the
    # round-trip needs a schema-valid PRD in place.
    # #696 flag day: almost no caller writes a [stack], and this harness
    # is about config resolution, never verification, so --no-verify
    # skips the stack checkpoint that would otherwise refuse before
    # run_factory (mocked by `captured`) is ever reached. The exception
    # is a caller whose SUBJECT is VerifyConfig: --no-verify also sets
    # ``factory_config.verify_config`` to None, so it passes
    # ``no_verify=False`` and confirms its own [stack] first.
    prd_path = tmp_path / "scripts" / "kstrl" / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    if not prd_path.exists():
        prd_path.write_text(json.dumps({"branchName": "kstrl/test", "userStories": []}))
    runner = CliRunner()
    return runner.invoke(
        cli_mod.cli,
        [
            "run",
            "1",
            "--root",
            str(tmp_path),
            "--agent-cmd",
            "true",
            "--ui",
            "plain",
            *(("--no-verify",) if no_verify else ()),
            *extra_args,
        ],
    )


# ---------------------------------------------------------------------------
# TOML round-trips through the real `ks factory` construction path
# ---------------------------------------------------------------------------


class TestFactoryCommandTomlRoundTrip:
    def test_factory_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\n"
            "max_parallel = 7\n"
            "max_retries = 9\n"
            'review_mode = "advisory"\n'
            "max_adversarial_calls = 5\n"
            "pause_before_pr_merge = true\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        fc = captured["factory_config"]
        assert fc.max_parallel == 7
        assert fc.max_retries == 9
        assert fc.review_mode == "advisory"
        assert fc.max_adversarial_calls == 5
        assert fc.pause_before_pr_merge is True

    def test_verify_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        # #696 flag day: [verify] test_command is retired (verification
        # commands come only from a confirmed [stack]); mutation_threshold
        # and require_self_critique are ordinary [verify] keys, unaffected.
        # A confirmed [stack] (not --no-verify) gets this one past the
        # checkpoint, since --no-verify also nulls verify_config.
        (tmp_path / "kstrl.toml").write_text(
            "[verify]\nmutation_threshold = 75.0\nrequire_self_critique = true\n"
        )
        write_stack(tmp_path)
        confirm_stack(tmp_path)
        result = _invoke_factory(tmp_path, no_verify=False)
        assert result.exit_code == 0, result.output
        vc = captured["factory_config"].verify_config
        assert vc is not None
        assert vc.mutation_threshold == 75.0
        assert vc.require_self_critique is True

    def test_security_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[security]\nmode = "hard"\nfail_threshold = "critical"\ntimeout_seconds = 123.0\n'
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        sc = captured["factory_config"].security_config
        assert sc is not None
        assert sc.mode == "hard"
        assert sc.fail_threshold == "critical"
        assert sc.timeout_seconds == 123.0

    def test_contract_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        # #696 flag day: [contract] test_command is retired (Phase 3 runs
        # every check of the confirmed [stack]); mode and timeout are
        # ordinary [contract] keys, unaffected.
        (tmp_path / "kstrl.toml").write_text('[contract]\nmode = "final"\ntimeout = 44.0\n')
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        cc = captured["factory_config"].contract_config
        assert cc is not None
        assert cc.mode == "final"
        assert cc.timeout == 44.0

    def test_contract_toml_skip_disables_phase(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[contract]\nmode = "skip"\n')
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].contract_config is None

    def test_codebase_scan_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[codebase_scan]\nmodule_map = false\nmax_context_tokens = 1234\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].codebase_scan_config
        assert ff is not None
        assert ff.module_map is False
        assert ff.max_context_tokens == 1234

    def test_timeout_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[timeout]\nagent_iteration = 42.0\ncomponent_total = 99.0\n"
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        tc = captured["factory_config"].timeout_config
        assert tc is not None
        assert tc.agent_iteration == 42.0
        assert tc.component_total == 99.0

    def test_base_config_sections(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        # KstrlConfig covers the [agent]/[run]/[paths]/[git]/[ui] sections.
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\nmodel = "model-from-toml"\n[run]\nsleep_seconds = 0.25\n'
        )
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        base = captured["base_config"]
        assert base.model == "model-from-toml"
        assert base.sleep_seconds == 0.25


# ---------------------------------------------------------------------------
# TOML round-trips through the real `ks run` construction path
# ---------------------------------------------------------------------------


class TestRunCommandTomlRoundTrip:
    def test_verify_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        # #696 flag day: [verify] test_command is retired (verification
        # commands come only from a confirmed [stack]); mutation_threshold
        # is an ordinary [verify] key, unaffected. A confirmed [stack]
        # (not --no-verify) gets this one past the checkpoint, since
        # --no-verify also nulls verify_config.
        (tmp_path / "kstrl.toml").write_text("[verify]\nmutation_threshold = 61.0\n")
        write_stack(tmp_path)
        confirm_stack(tmp_path)
        result = _invoke_run(tmp_path, no_verify=False)
        assert result.exit_code == 0, result.output
        vc = captured["factory_config"].verify_config
        assert vc is not None
        assert vc.mutation_threshold == 61.0

    def test_security_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text('[security]\nmode = "advisory"\n')
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        sc = captured["factory_config"].security_config
        assert sc is not None
        assert sc.mode == "advisory"

    def test_codebase_scan_section(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text("[codebase_scan]\nmax_context_tokens = 555\n")
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].codebase_scan_config
        assert ff is not None
        assert ff.max_context_tokens == 555

    def test_factory_tunables_honored(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nmax_retries = 6\nmax_adversarial_calls = 2\n"
        )
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        fc = captured["factory_config"]
        assert fc.max_retries == 6
        assert fc.max_adversarial_calls == 2

    def test_single_component_structure_is_forced(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        # Structural fields cannot be overridden by toml: `ks run` is
        # by definition a local single-component no-PR invocation.
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\n"
            "max_parallel = 8\n"
            "use_worktrees = true\n"
            "single_pr = true\n"
            "create_prs = true\n"
        )
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        fc = captured["factory_config"]
        assert fc.max_parallel == 1
        assert fc.use_worktrees is False
        assert fc.single_pr is False
        assert fc.create_prs is False

    def test_review_mode_defaults_to_advisory(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].review_mode == "advisory"

    def test_review_mode_toml_optin_is_honored(
        self, tmp_path: Path, captured: dict[str, Any]
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[factory]\nreview_mode = "hard"\n')
        result = _invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].review_mode == "hard"


# ---------------------------------------------------------------------------
# `ks evolve` builds EvolutionConfig via load (the [evolution] section)
# ---------------------------------------------------------------------------


class TestEvolveCommandTomlRoundTrip:
    def test_enabled_false_in_toml_stops_evolve(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[evolution]\nenabled = false\n")
        runner = CliRunner()
        result = runner.invoke(
            cli_mod.cli,
            ["evolve", "--status", "--root", str(tmp_path), "--ui", "plain"],
        )
        assert result.exit_code == 2
        assert "disabled" in result.output

    def test_journal_path_reaches_journal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[evolution]\njournal_path = "custom/evo.jsonl"\n')
        box: dict[str, Any] = {}

        # Subclasses the real journal so a second call does not AttributeError (#327 F7).
        class FakeJournal(evolution_mod.EvolutionJournal):
            def __init__(self, config: EvolutionConfig) -> None:
                super().__init__(config)
                box["config"] = config

        monkeypatch.setattr(evolution_mod, "EvolutionJournal", FakeJournal)
        runner = CliRunner()
        result = runner.invoke(
            cli_mod.cli,
            ["evolve", "--status", "--root", str(tmp_path), "--ui", "plain"],
        )
        assert result.exit_code == 0, result.output
        assert box["config"].journal_path == tmp_path / "custom/evo.jsonl"


# ---------------------------------------------------------------------------
# Precedence: explicit CLI flag > env > toml (factory / verify / security)
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_factory_flag_beats_env_beats_toml(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 5\n")
        monkeypatch.setenv("FACTORY_MAX_PARALLEL", "6")
        result = _invoke_factory(tmp_path, "--max-parallel", "7")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_parallel == 7

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].max_parallel == 6

        monkeypatch.delenv("FACTORY_MAX_PARALLEL")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_parallel == 5

    def test_security_flag_beats_env_beats_toml(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text('[security]\nmode = "advisory"\n')
        monkeypatch.setenv("KSTRL_SECURITY_MODE", "skip")
        result = _invoke_factory(tmp_path, "--security-mode", "hard")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].security_config.mode == "hard"

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].security_config.mode == "skip"

        monkeypatch.delenv("KSTRL_SECURITY_MODE")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].security_config.mode == "advisory"

    def test_verify_env_equal_to_default_still_beats_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: the old overlay compared the env-derived value to
        # the dataclass default and skipped it on equality, so an env
        # var explicitly set to the default value could not override a
        # toml value.
        (tmp_path / "kstrl.toml").write_text("[verify]\nmutation_threshold = 75.0\n")
        monkeypatch.setenv("KSTRL_MUTATION_THRESHOLD", "50")
        config = VerifyConfig.load(tmp_path)
        assert config.mutation_threshold == 50.0


# ---------------------------------------------------------------------------
# R2.2: the two safety knobs are reachable via all three surfaces
# ---------------------------------------------------------------------------


class TestSafetyKnobs:
    def test_max_adversarial_calls_all_surfaces(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_adversarial_calls = 1\n")
        monkeypatch.setenv("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "2")
        result = _invoke_factory(tmp_path, "--max-adversarial-calls", "3")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_adversarial_calls == 3

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].max_adversarial_calls == 2

        monkeypatch.delenv("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_adversarial_calls == 1

    def test_max_total_tokens_all_surfaces(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R3.1: the token budget is reachable via flag > env > toml."""
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_total_tokens = 100\n")
        monkeypatch.setenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "200")
        result = _invoke_factory(tmp_path, "--max-total-tokens", "300")
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_total_tokens == 300

        result = _invoke_factory(tmp_path)
        assert captured["factory_config"].max_total_tokens == 200

        monkeypatch.delenv("KSTRL_FACTORY_MAX_TOTAL_TOKENS")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].max_total_tokens == 100

    def test_pause_before_pr_merge_all_surfaces(
        self,
        tmp_path: Path,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\npause_before_pr_merge = true\n")
        # env (explicitly false) beats toml (true)
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "0")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].pause_before_pr_merge is False

        # flag beats env
        result = _invoke_factory(tmp_path, "--pause-before-pr-merge")
        assert captured["factory_config"].pause_before_pr_merge is True

        # negated flag also wins
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "1")
        result = _invoke_factory(tmp_path, "--no-pause-before-pr-merge")
        assert captured["factory_config"].pause_before_pr_merge is False

        # toml alone
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert captured["factory_config"].pause_before_pr_merge is True


# ---------------------------------------------------------------------------
# NOTE lines: toml-driven changes are surfaced at factory startup
# ---------------------------------------------------------------------------


class TestTomlNotes:
    def test_note_emitted_for_toml_value(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 9\n")
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert "NOTE: [factory] max_parallel = 9" in result.output

    def test_no_note_when_flag_overrides(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 9\n")
        result = _invoke_factory(tmp_path, "--max-parallel", "4")
        assert result.exit_code == 0, result.output
        assert "NOTE: [factory] max_parallel" not in result.output

    def test_no_notes_without_toml(self, tmp_path: Path, captured: dict[str, Any]) -> None:
        result = _invoke_factory(tmp_path)
        assert result.exit_code == 0, result.output
        assert "NOTE: [" not in result.output


# ---------------------------------------------------------------------------
# from_env for the loaders that were missing it (R2.1)
# ---------------------------------------------------------------------------


class TestNewFromEnv:
    def test_codebase_scan_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_CODEBASE_SCAN_MAX_TOKENS", "123")
        monkeypatch.setenv("KSTRL_CODEBASE_SCAN_MODULE_MAP", "false")
        config = CodebaseScanConfig.from_env()
        assert config.max_context_tokens == 123
        assert config.module_map is False

    def test_evolution_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "3")
        monkeypatch.setenv("KSTRL_EVOLUTION_JOURNAL_PATH", "custom/j.jsonl")
        config = EvolutionConfig.from_env(tmp_path)
        assert config.lookback_runs == 3
        assert config.journal_path == tmp_path / "custom/j.jsonl"
        # defaults resolve against root_dir, not CWD
        assert config.experiments_path == tmp_path / ".kstrl/experiments.tsv"

    def test_knowledge_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_MAX_CORE_TOKENS", "99")
        monkeypatch.setenv("KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE", "transitive")
        config = KnowledgeConfig.from_env(tmp_path)
        assert config.max_core_tokens == 99
        assert config.dependency_scope == "transitive"
        assert config.knowledge_root == tmp_path / ".kstrl" / "knowledge"

    def test_knowledge_from_env_rejects_bad_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_DEPENDENCY_SCOPE", "everything")
        with pytest.raises(ValueError, match="DEPENDENCY_SCOPE"):
            KnowledgeConfig.from_env(tmp_path)

    def test_factory_from_env_reads_safety_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "4")
        monkeypatch.setenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", "true")
        config = FactoryConfig.from_env()
        assert config.max_adversarial_calls == 4
        assert config.pause_before_pr_merge is True


# ---------------------------------------------------------------------------
# Knowledge section loader round-trip (consumed by run_factory internally)
# ---------------------------------------------------------------------------


class TestKnowledgeSectionRoundTrip:
    def test_toml_reaches_loaded_config(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[knowledge]\nmax_core_tokens = 777\ndistill_model = "m"\n'
        )
        config = KnowledgeConfig.load(tmp_path)
        assert config.max_core_tokens == 777
        assert config.distill_model == "m"
