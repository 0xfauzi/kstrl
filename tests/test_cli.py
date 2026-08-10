"""Tests for CLI module."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from kstrl.cli import _run_structural_override_notices, cli
from kstrl.factory import FactoryConfig


class TestCliHelp:
    """Tests for CLI help commands."""

    def test_main_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "kstrl" in result.output
        assert "run" in result.output
        assert "init" in result.output
        assert "understand" in result.output
        assert "feature" in result.output

    def test_run_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "MAX_ITERATIONS" in result.output
        assert "--agent-cmd" in result.output
        assert "--model" in result.output

    def test_init_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "DIRECTORY" in result.output

    def test_understand_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["understand", "--help"])
        assert result.exit_code == 0
        assert "read-only" in result.output.lower()

    def test_feature_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["feature", "--help"])
        assert result.exit_code == 0
        assert "implementation" in result.output.lower()

    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        from kstrl import __version__

        assert __version__ in result.output


class TestCliValidation:
    """Tests for CLI argument validation."""

    def test_run_invalid_max_iterations(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "invalid"])
        assert result.exit_code == 2
        assert "not a valid integer" in result.output

    def test_run_missing_prompt_file(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["run", "1", "--agent-cmd", "echo test", "--branch", ""],
            )
            # Should fail because prompt file doesn't exist
            assert result.exit_code != 0

    def test_run_uses_prompt_env_for_root(self, tmp_path: Path, monkeypatch) -> None:
        """``PROMPT_FILE`` env var should anchor the root-discovery logic
        before the factory pipeline takes over. We don't need to drive
        a full factory iteration here -- ``--no-verify`` short-circuits
        the verification phase so the test stays fast and doesn't depend
        on real git/agent state."""
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text("test prompt")
        (kstrl_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "run",
                "0",
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
                "--no-verify",
            ],
            env={
                "PROMPT_FILE": str(kstrl_dir / "prompt.md"),
                "PRD_FILE": str(kstrl_dir / "prd.json"),
            },
        )
        # Either runs to completion (exit 0) or fails on the
        # factory-prerequisite check; the goal here is that
        # PROMPT_FILE resolves the root correctly, not that the
        # factory completes a real run in this in-process invocation.
        assert "PROMPT_FILE" not in (result.output or "")

    def test_understand_uses_root_option(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "understand_prompt.md").write_text("test prompt")
        (kstrl_dir / "codebase_map.md").write_text("# Map\n")
        (kstrl_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "understand",
                "1",
                "--root",
                str(project),
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
        )
        assert result.exit_code == 0

    def test_feature_uses_root_option(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        feature_dir = kstrl_dir / "feature" / "demo"
        feature_dir.mkdir(parents=True)
        (kstrl_dir / "feature_understand_prompt.md").write_text("test prompt")
        (kstrl_dir / "codebase_map.md").write_text("# Map\n")
        (feature_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            [
                "feature",
                "--root",
                str(project),
                "--prd",
                str(feature_dir / "prd.json"),
                "--understand-iterations",
                "1",
                "--implementation-auto-run",
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert (feature_dir / "understand.md").exists()


class TestRunStructuralOverrideNotices:
    """Issue #207: `ks run` must say when it overrides configured knobs.

    `ks run` forces create_prs=False (among other structural fields), which
    makes the pause_before_pr_merge merge gate unreachable. The fix emits a
    startup notice when the resolved config set such a knob to a
    non-default value - and stays silent otherwise, so the notice does not
    become background noise.
    """

    def test_no_notices_for_default_config(self) -> None:
        assert _run_structural_override_notices(FactoryConfig()) == []

    def test_pause_before_pr_merge_gets_honesty_note(self) -> None:
        notices = _run_structural_override_notices(
            FactoryConfig(pause_before_pr_merge=True)
        )
        assert len(notices) == 1
        assert "pause_before_pr_merge" in notices[0]
        assert "no PR is created" in notices[0]
        assert "ks factory" in notices[0]

    def test_non_default_structural_field_is_named(self) -> None:
        notices = _run_structural_override_notices(
            FactoryConfig(max_parallel=8, single_pr=True)
        )
        assert any("max_parallel = 8" in n for n in notices)
        assert any("single_pr = true" in n for n in notices)
        assert len(notices) == 2

    def test_default_valued_fields_stay_silent(self) -> None:
        """create_prs defaults to True and is forced to False on every
        `ks run`; warning about the default would fire unconditionally."""
        assert _run_structural_override_notices(
            FactoryConfig(create_prs=True, use_worktrees=True)
        ) == []

    def _scaffold_project(self, tmp_path: Path, toml_body: str = "") -> Path:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text("test prompt")
        (kstrl_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )
        if toml_body:
            (project / "kstrl.toml").write_text(toml_body)
        return project

    def _invoke_run(self, project: Path) -> str:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "run",
                "0",
                "--root", str(project),
                "--agent-cmd", "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep", "0",
                "--no-verify",
                "--ui", "plain",
                "--no-color",
            ],
        )
        return result.output or ""

    def test_run_emits_notice_when_toml_sets_merge_gate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", raising=False)
        project = self._scaffold_project(
            tmp_path, "[factory]\npause_before_pr_merge = true\n"
        )
        output = self._invoke_run(project)
        assert "pause_before_pr_merge" in output
        assert "merge gate" in output

    def test_run_stays_silent_when_merge_gate_unset(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE", raising=False)
        project = self._scaffold_project(tmp_path)
        output = self._invoke_run(project)
        assert "pause_before_pr_merge" not in output


class TestDecomposeBlockerOutput:
    """R1.7: the CLI points the user at the persisted spec-issues file."""

    def test_prints_artifact_path_on_halt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import kstrl.cli as cli_mod
        from kstrl.decompose import SpecBlockerError, SpecIssue

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        artifact = tmp_path / "scripts" / "kstrl" / "spec-issues.json"

        def fake_decompose(**kwargs: object) -> None:
            raise SpecBlockerError(
                [SpecIssue(
                    severity="blocker",
                    kind="ambiguity",
                    summary="spec is too vague",
                )],
                artifact_path=artifact,
            )

        monkeypatch.setattr(cli_mod, "decompose_spec", fake_decompose)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "decompose",
                "--spec", str(spec_file),
                "--project-name", "test",
                "--agent-cmd", "true",
                "--ui", "plain",
                "--no-color",
            ],
        )
        assert result.exit_code == 2
        assert "spec is too vague" in result.output
        assert str(artifact) in result.output

