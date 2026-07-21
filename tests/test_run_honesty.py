"""R2.3: `ks run` and factory flags are honest (CRIT-8, H-10, H-11).

- CRIT-8: max_iterations (N), interactive, and allowed_paths forward from
  the invoking config through ``_submit_args`` into ``_run_component``;
  the hardcoded 30-iteration non-interactive loop is gone.
- --no-verify: ``FactoryConfig.skip_verification`` is an explicit skip
  sentinel that ``run_factory`` honors - Phase 1 runs ZERO checks, states
  the skip in output, and records a phase_skipped finding.
- H-10: both ``ks run`` and ``ks factory`` wire FeedforwardConfig
  (via the toml/env control plane), and the built Phase 0 context reaches
  the engineer prompt.
- H-11: the rendered engineer prompt names the SAME per-component PRD
  file that ``verify.check_prd_stories`` re-reads for a decomposed
  component (DEFAULT_PROMPT >= 1.1.0 ships the ``$prd_path`` placeholder
  substituted by loop.py).

Fake agents are real subprocesses (CustomAgent via ``agent_cmd``); no LLM
is involved.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from string import Template
from typing import Any

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
import kstrl.loop as loop_mod
from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, FactoryResult, run_factory
from kstrl.init_cmd import DEFAULT_PROMPT
from kstrl.loop import LoopResult
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

COMPLETE_LINE = "echo '<promise>COMPLETE</promise>'"
COMP_PRD_PATH = "scripts/kstrl/feature/comp-a/prd.json"
# Header emitted by feedforward.build_feedforward_context; asserting on it
# proves Phase 0 context reached the engineer prompt.
FEEDFORWARD_HEADER = "=== CODEBASE CONTEXT (auto-generated) ==="


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30,
    )


def _init_repo(root: Path) -> None:
    """Real git repo with a decomposed component PRD (passes=true) and a
    small Python source file so feedforward has content to summarize."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return f'hello {name}'\n"
    )
    _git("add", "src/app.py", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)

    feature_dir = root / "scripts" / "kstrl" / "feature" / "comp-a"
    feature_dir.mkdir(parents=True)
    (feature_dir / "prd.json").write_text(json.dumps({
        "branchName": "kstrl/factory/comp-a",
        "userStories": [{
            "id": "US-001", "title": "Test",
            "acceptanceCriteria": ["AC1"],
            "priority": 1, "passes": True, "notes": "",
        }],
    }))


def _manifest() -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="t",
        base_branch="main", single_pr=False,
        components=[Component(
            id="comp-a", title="A", description="", dependencies=[],
            prd_path=COMP_PRD_PATH,
            branch_name="kstrl/factory/comp-a",
        )],
    )


def _factory_config(**overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        max_parallel=1, max_retries=0, retry_delay=0,
        use_worktrees=False, create_prs=False, review_mode="skip",
        skip_verification=True,
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _base_config(root: Path, agent_cmd: str, **overrides: Any) -> KstrlConfig:
    defaults: dict[str, Any] = dict(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0, agent_cmd=agent_cmd,
        kstrl_branch="", kstrl_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )
    defaults.update(overrides)
    return KstrlConfig(**defaults)


class TestIterationForwarding:
    """CRIT-8: `ks run N` runs at most N iterations, not 30."""

    def test_n3_executes_exactly_three_iterations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        count_file = tmp_path / "invocations.txt"
        # Consumes the prompt, records the invocation, never completes.
        agent_cmd = f"cat > /dev/null; echo x >> {count_file}"

        result = run_factory(
            _manifest(), _factory_config(),
            _base_config(root, agent_cmd, max_iterations=3),
            PlainUI(no_color=True), root,
            manifest_path=tmp_path / "manifest.json",
        )

        assert result.failed == ["comp-a"]
        invocations = len(count_file.read_text().splitlines())
        assert invocations == 3, (
            f"expected exactly 3 agent iterations for N=3, got {invocations}"
        )

    def test_loop_settings_forwarded_into_run_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """max_iterations, interactive, and allowed_paths all reach the
        KstrlConfig that _run_component hands to run_loop."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        captured: dict[str, KstrlConfig] = {}

        def fake_run_loop(
            config: KstrlConfig, ui: Any, agent: Any,
            cwd: Path | None = None, context_prefix: str | None = None,
            timeouts: Any = None, breaker_config: Any = None,
            **kwargs: Any,
        ) -> LoopResult:
            captured["config"] = config
            return LoopResult(completed=True, iterations=1, exit_code=0)

        monkeypatch.setattr(loop_mod, "run_loop", fake_run_loop)

        result = run_factory(
            _manifest(), _factory_config(),
            _base_config(
                root, COMPLETE_LINE, max_iterations=7,
                interactive=True, allowed_paths=["src/", "docs/"],
            ),
            PlainUI(no_color=True), root,
            manifest_path=tmp_path / "manifest.json",
        )

        assert result.completed == ["comp-a"]
        config = captured["config"]
        assert config.max_iterations == 7
        assert config.interactive is True
        assert config.allowed_paths == ["src/", "docs/"]


class TestNoVerifySkipSentinel:
    """--no-verify genuinely skips Phase 1 (zero checks, stated, recorded)."""

    def test_skip_sentinel_runs_zero_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        marker = tmp_path / "check-ran.marker"
        # Even with a verify_config PRESENT, the sentinel wins: none of
        # these commands may execute.
        verify_config = VerifyConfig(
            test_command=f"touch {marker}",
            typecheck_command=f"touch {marker}",
            lint_command=f"touch {marker}",
        )
        manifest = _manifest()

        result = run_factory(
            manifest,
            _factory_config(skip_verification=True, verify_config=verify_config),
            _base_config(root, COMPLETE_LINE),
            PlainUI(no_color=True), root,
            manifest_path=tmp_path / "manifest.json",
        )

        assert result.completed == ["comp-a"]
        assert not marker.exists(), (
            "--no-verify still executed a mechanical check command"
        )
        # PlainUI info lines go to stderr.
        assert "Phase 1 SKIPPED" in capsys.readouterr().err
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.verification_passed is None
        assert any(
            f.phase == "verify" and f.severity == "skipped"
            for f in comp.findings
        ), "skip must be recorded as a phase_skipped finding"

    def test_checks_run_when_not_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mirror: the same setup without the sentinel does run checks."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        marker = tmp_path / "check-ran.marker"
        verify_config = VerifyConfig(
            test_command=f"touch {marker}",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
        )
        manifest = _manifest()

        result = run_factory(
            manifest,
            _factory_config(skip_verification=False, verify_config=verify_config),
            _base_config(root, COMPLETE_LINE),
            PlainUI(no_color=True), root,
            manifest_path=tmp_path / "manifest.json",
        )

        assert result.completed == ["comp-a"]
        assert marker.exists(), "verification did not run without the sentinel"
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.verification_passed is True


class TestCliWiring:
    """The CLI passes the sentinel + feedforward config into run_factory."""

    def _capture_run_factory(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_run_factory(
            manifest: Manifest, factory_config: FactoryConfig,
            base_config: KstrlConfig, ui: Any, root_dir: Path,
            manifest_path: Path | None = None,
            **kwargs: Any,
        ) -> FactoryResult:
            captured["factory_config"] = factory_config
            captured["base_config"] = base_config
            return FactoryResult()

        monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
        return captured

    def _write_run_prd(self, root: Path) -> None:
        """R2.4: `ks run` preflights prd.json before run_factory."""
        prd_path = root / "scripts" / "kstrl" / "prd.json"
        prd_path.parent.mkdir(parents=True, exist_ok=True)
        prd_path.write_text(
            json.dumps({"branchName": "kstrl/test", "userStories": []})
        )

    def test_run_no_verify_sets_skip_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._capture_run_factory(monkeypatch)
        monkeypatch.setenv("AGENT_CMD", "echo hi")
        self._write_run_prd(tmp_path)

        result = CliRunner().invoke(
            cli_mod.cli,
            ["run", "2", "--root", str(tmp_path), "--no-verify"],
        )

        assert result.exit_code == 0, result.output
        cfg = captured["factory_config"]
        assert cfg.skip_verification is True
        assert cfg.verify_config is None
        assert cfg.feedforward_config is not None
        assert captured["base_config"].max_iterations == 2

    def test_run_default_does_not_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._capture_run_factory(monkeypatch)
        monkeypatch.setenv("AGENT_CMD", "echo hi")
        self._write_run_prd(tmp_path)

        result = CliRunner().invoke(
            cli_mod.cli, ["run", "2", "--root", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        cfg = captured["factory_config"]
        assert cfg.skip_verification is False
        assert isinstance(cfg.verify_config, VerifyConfig)
        assert cfg.feedforward_config is not None

    def _write_manifest(self, root: Path) -> Path:
        manifest_path = root / "m.json"
        _manifest().save(manifest_path)
        return manifest_path

    def test_factory_wires_feedforward_from_control_plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """H-10: `ks factory` passes a FeedforwardConfig loaded from
        toml/env; previously feedforward_config was never set and Phase 0
        silently never ran."""
        captured = self._capture_run_factory(monkeypatch)
        monkeypatch.setenv("AGENT_CMD", "echo hi")
        manifest_path = self._write_manifest(tmp_path)
        args = [
            "factory", "--manifest", str(manifest_path),
            "--root", str(tmp_path), "--yes",
        ]

        # Default: enabled.
        result = CliRunner().invoke(cli_mod.cli, args)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].feedforward_config
        assert ff is not None and ff.enabled is True

        # toml disables it.
        (tmp_path / "kstrl.toml").write_text("[feedforward]\nenabled = false\n")
        result = CliRunner().invoke(cli_mod.cli, args)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].feedforward_config
        assert ff is not None and ff.enabled is False

        # Env overrides toml.
        monkeypatch.setenv("KSTRL_FEEDFORWARD_ENABLED", "1")
        result = CliRunner().invoke(cli_mod.cli, args)
        assert result.exit_code == 0, result.output
        ff = captured["factory_config"].feedforward_config
        assert ff is not None and ff.enabled is True

    def test_factory_no_verify_sets_skip_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._capture_run_factory(monkeypatch)
        monkeypatch.setenv("AGENT_CMD", "echo hi")
        manifest_path = self._write_manifest(tmp_path)

        result = CliRunner().invoke(cli_mod.cli, [
            "factory", "--manifest", str(manifest_path),
            "--root", str(tmp_path), "--yes", "--no-verify",
        ])

        assert result.exit_code == 0, result.output
        cfg = captured["factory_config"]
        assert cfg.skip_verification is True
        assert cfg.verify_config is None


class TestFeedforwardAndPrdPathEndToEnd:
    """Real `ks factory` CLI run: Phase 0 context and the per-component
    PRD path both land in the prompt the engineer receives."""

    def test_factory_run_builds_feedforward_and_names_component_prd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        _init_repo(root)
        manifest_path = root / "m.json"
        manifest = _manifest()
        manifest.save(manifest_path)

        dump = tmp_path / "prompt-received.txt"
        monkeypatch.setenv("AGENT_CMD", f"cat > {dump}; " + COMPLETE_LINE)
        monkeypatch.setenv("KSTRL_BRANCH", "")
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        result = CliRunner().invoke(cli_mod.cli, [
            "factory", "--manifest", str(manifest_path),
            "--root", str(root), "--yes",
            "--no-prs", "--no-worktrees",
            "--review-mode", "skip", "--contract-check", "skip",
            "--no-verify", "--ui", "plain", "--no-color",
        ])

        assert result.exit_code == 0, result.output
        prompt = dump.read_text()

        # H-10: Phase 0 feedforward context reached the built prompt.
        assert FEEDFORWARD_HEADER in prompt

        # H-11: the prompt names the decomposed component's PRD - the
        # exact path check_prd_stories reads (wt_path / comp.prd_path;
        # wt_path == root with --no-worktrees) - not the legacy default.
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert str(root / comp.prd_path) in prompt
        assert "scripts/kstrl/prd.json" not in prompt

        # The skip sentinel flowed through the real CLI stack.
        assert "Phase 1 SKIPPED" in result.output

    def test_rendered_prompt_and_check_prd_stories_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With verification RUNNING, the component completes because
        check_prd_stories reads the same feature PRD (passes=true) that
        the rendered DEFAULT_PROMPT told the agent to use."""
        root = tmp_path / "repo"
        _init_repo(root)
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        dump = tmp_path / "prompt-received.txt"
        manifest = _manifest()

        result = run_factory(
            manifest,
            _factory_config(
                skip_verification=False,
                verify_config=VerifyConfig(
                    test_command="true", typecheck_command="true",
                    lint_command="true", check_diff_scope=False,
                    check_bad_patterns=False,
                ),
            ),
            # No prompt.md exists in the repo, so the run exercises the
            # harness DEFAULT_PROMPT fallback and its $prd_path contract.
            _base_config(root, f"cat > {dump}; " + COMPLETE_LINE),
            PlainUI(no_color=True), root,
            manifest_path=tmp_path / "manifest.json",
        )

        # Completion proves check_prd_stories PASSED on the feature PRD.
        assert result.completed == ["comp-a"]
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.verification_passed is True

        prompt = dump.read_text()
        assert str(root / comp.prd_path) in prompt
        assert "scripts/kstrl/prd.json" not in prompt


class TestDefaultPromptPlaceholderContract:
    """H-11 unit level: DEFAULT_PROMPT carries the placeholders and
    renders them exactly the way loop.run_loop substitutes."""

    def test_placeholders_present_and_legacy_paths_absent(self) -> None:
        assert "$prd_path" in DEFAULT_PROMPT
        assert "$progress_path" in DEFAULT_PROMPT
        assert "$codebase_map_path" in DEFAULT_PROMPT
        assert "scripts/kstrl/prd.json" not in DEFAULT_PROMPT
        assert "scripts/kstrl/progress.txt" not in DEFAULT_PROMPT

    def test_rendering_matches_loop_substitution(self) -> None:
        prd = "/w/scripts/kstrl/feature/comp-a/prd.json"
        rendered = Template(DEFAULT_PROMPT).safe_substitute(
            prd_path=prd,
            progress_path="/w/scripts/kstrl/progress.txt",
            codebase_map_path="/w/scripts/kstrl/codebase_map.md",
        )
        assert prd in rendered
        for leftover in ("$prd_path", "$progress_path", "$codebase_map_path"):
            assert leftover not in rendered
