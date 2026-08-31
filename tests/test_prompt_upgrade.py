"""#286: what kstrl DOES about a prompt that is an older template.

The classifier and the ledger's own invariants are in
tests/test_prompt_staleness.py; the shared fixtures come from there.
This file covers the acting half: what `ks init` reports, what
``--upgrade-prompts`` rewrites and what it refuses to touch, and which
operator-facing surfaces the warning reaches.

Every guard here was measured broken before it was written. The
permission tightening (0o644 to 0o600) and the three ways a rewrite
could damage a shared file (symlink, hard link, symlinked scaffold
directory) were all reproduced on a real filesystem, so these tests
assert on real filesystem state rather than on a call recorder.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kstrl import init_cmd
from kstrl.cli import cli
from kstrl.init_cmd import (
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_VERSION,
    ScaffoldedTemplate,
    classify_scaffolded_path,
    staleness_notice,
)
from kstrl.init_wizard import plan_scaffold
from tests.spine_utils import git as spine_git
from tests.test_init_cmd import run_init_capturing
from tests.test_prompt_staleness import (
    NEW_BODY,
    OLD_BODY,
    SYNTHETIC,
    _prompt_at,
    _sha256,
    synthetic_ledger,
)

__all__ = ["synthetic_ledger"]


class TestInitReportsAndUpgrades:
    def test_rerun_reports_a_stale_scaffold(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        _prompt_at(tmp_path, OLD_BODY)
        _, output = run_init_capturing(tmp_path)
        assert "prompt.md already exists" in output
        assert "9.0.0" in output
        assert "ks init --upgrade-prompts" in output

    def test_rerun_stays_quiet_on_a_current_scaffold(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        _, output = run_init_capturing(tmp_path)
        assert "prompt.md already exists" in output
        assert "upgrade-prompts" not in output

    def test_upgrade_rewrites_a_pristine_older_scaffold(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        path = _prompt_at(tmp_path, OLD_BODY)
        _, output = run_init_capturing(tmp_path, upgrade_prompts=True)
        assert path.read_text() == NEW_BODY
        assert "9.0.0 -> 9.1.0" in output
        assert "no local edits" in output

    def test_upgrade_never_touches_an_edited_prompt(self, tmp_path: Path) -> None:
        edited = DEFAULT_PROMPT + "\nalways run the fuzzer\n"
        path = _prompt_at(tmp_path, edited)
        _, output = run_init_capturing(tmp_path, upgrade_prompts=True)
        assert path.read_text() == edited
        assert "matches no template kstrl has shipped" in output
        assert "left alone" in output

    def test_upgrade_is_a_no_op_on_a_current_scaffold(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        before = (tmp_path / "scripts" / "kstrl" / "prompt.md").read_text()
        _, output = run_init_capturing(tmp_path, upgrade_prompts=True)
        assert (tmp_path / "scripts" / "kstrl" / "prompt.md").read_text() == before
        assert f"already at {DEFAULT_PROMPT_VERSION}" in output

    @pytest.mark.parametrize(
        ("extra_args", "expected_body"),
        [
            # `ks init` stays non-destructive unless the operator says so.
            ([], OLD_BODY),
            (["--upgrade-prompts"], NEW_BODY),
        ],
        ids=["off-by-default", "opted-in"],
    )
    def test_the_flag_is_the_only_thing_that_rewrites(
        self,
        tmp_path: Path,
        synthetic_ledger: ScaffoldedTemplate,
        extra_args: list[str],
        expected_body: str,
    ) -> None:
        path = _prompt_at(tmp_path, OLD_BODY)
        result = CliRunner().invoke(
            cli, ["init", str(tmp_path), *extra_args, "--ui", "plain", "--no-color"]
        )
        assert result.exit_code == 0, result.output
        assert path.read_text() == expected_body


def _symlinked_prompt(root: Path, body: str) -> tuple[Path, Path]:
    """A scaffolded prompt that is a link into a file shared elsewhere."""
    shared = root / "shared-prompt.md"
    shared.write_text(body)
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    link = kstrl_dir / "prompt.md"
    link.symlink_to(shared)
    return link, shared


def _mode(path: Path) -> int:
    """The permission bits of ``path`` itself, never a symlink target."""
    return stat.S_IMODE(path.lstat().st_mode)


class TestUpgradeLeavesTheFileItFoundIntact:
    """Review of #290: the upgrade rewrote more than the bytes.

    Both cases below were measured on a real filesystem before the fix
    (0o644 became 0o600; a symlink became a regular file), so both
    assert on real filesystem state rather than on a call recorder.
    """

    @pytest.mark.parametrize("mode", [0o644, 0o664], ids=["rw-r--r--", "rw-rw-r--"])
    def test_upgrade_preserves_the_mode_it_found(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate, mode: int
    ) -> None:
        """`mkstemp` creates 0600 and `os.replace` carries that across,
        so the upgrade used to tighten the file. The rule is preserve
        what was there, not write one blessed mode, hence two."""
        path = _prompt_at(tmp_path, OLD_BODY)
        os.chmod(path, mode)
        run_init_capturing(tmp_path, upgrade_prompts=True)
        assert path.read_text() == NEW_BODY
        assert _mode(path) == mode

    def test_upgrade_will_not_replace_a_symlinked_prompt(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """An operator sharing one prompt across projects by symlink got
        the link swapped for a regular file, the shared source left on
        the old body, and every other project still stale: the exact
        outcome this feature exists to prevent, produced by it."""
        link, shared = _symlinked_prompt(tmp_path, OLD_BODY)

        _, output = run_init_capturing(tmp_path, upgrade_prompts=True)

        assert link.is_symlink()
        assert shared.read_text() == OLD_BODY
        assert "is a symlink to" in output
        assert str(shared.resolve()) in output
        assert "left alone" in output

    def test_upgrade_will_not_replace_a_hard_linked_prompt(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """The symlink guard missed this one: measured, `os.replace`
        broke the link, moved the project to the new body and left the
        shared source on the old one, with no warning at all."""
        shared = tmp_path / "shared-prompt.md"
        shared.write_text(OLD_BODY)
        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        linked = kstrl_dir / "prompt.md"
        os.link(shared, linked)
        inode = linked.stat().st_ino

        _, output = run_init_capturing(tmp_path, upgrade_prompts=True)

        assert linked.stat().st_ino == inode
        assert shared.read_text() == OLD_BODY
        assert "hard link" in output
        assert "left alone" in output

    def test_upgrade_will_not_write_through_a_symlinked_scaffold_dir(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """`scripts/kstrl` itself a link into a shared harness: nothing
        on the leaf is a link, so only comparing against the project
        root catches it. Measured, the upgrade rewrote a file outside
        the directory `ks init` was pointed at."""
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "prompt.md").write_text(OLD_BODY)
        root = tmp_path / "project"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "kstrl").symlink_to(shared_dir)

        _, output = run_init_capturing(root, upgrade_prompts=True)

        assert (shared_dir / "prompt.md").read_text() == OLD_BODY
        assert "outside this project" in output
        assert "left alone" in output

    def test_two_reasons_are_both_reported(self, tmp_path: Path, monkeypatch) -> None:
        """A relocated prompt that is ALSO a link has two problems, and
        naming one of them would send the operator down a path that
        fails for the other."""
        monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (SYNTHETIC,))
        shared = tmp_path / "shared-prompt.md"
        shared.write_text(OLD_BODY)
        elsewhere = tmp_path / "prompts"
        elsewhere.mkdir()
        link = elsewhere / "prompt.md"
        link.symlink_to(shared)

        notice = staleness_notice(link)
        assert notice is not None
        assert "a symlink to" in notice.advice
        assert "not the copy under scripts/kstrl/" in notice.advice

    def test_a_symlinked_prompt_is_still_reported_as_stale(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """Refusing to REWRITE it is not a reason to stop SAYING it is
        old. The content is stale; only the remedy differs."""
        link, shared = _symlinked_prompt(tmp_path, OLD_BODY)

        notice = staleness_notice(link)
        assert notice is not None
        assert "9.0.0" in notice.headline
        assert "is a symlink to" in notice.advice
        assert str(shared.resolve()) in notice.advice
        assert "Run `ks init --upgrade-prompts`" not in notice.advice


class TestScaffoldRoundTripsItsOwnBytes:
    """Classification is byte equality, so the write and the read have to
    agree about the encoding on every locale, not just a UTF-8 one.

    Every shipped body is ASCII today, which is exactly why this is cheap
    to pin now: the first non-ASCII character in a template would
    otherwise make a freshly scaffolded file classify as ``unrecognised``
    on a non-UTF-8 machine, and so never be reported or upgraded.
    """

    def test_a_non_ascii_template_scaffolds_and_classifies_as_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "# engineer instructions\n\nPrefer \u00e9lan and \u2713 marks.\n"
        template = ScaffoldedTemplate(
            filename="prompt.md",
            constant_name="DEFAULT_PROMPT",
            body=body,
            history=((_sha256(body), "9.0.0"),),
        )
        monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (template,))
        monkeypatch.setattr(init_cmd, "DEFAULT_PROMPT", body)

        run_init_capturing(tmp_path)

        path = tmp_path / "scripts" / "kstrl" / "prompt.md"
        assert path.read_bytes() == body.encode("utf-8")
        state = classify_scaffolded_path(path)
        assert state is not None
        assert state.status == "current"

    def test_the_same_test_passes_where_the_default_encoding_is_not_utf8(self) -> None:
        """Re-runs the test above in a child whose default encoding is
        ASCII. In the parent's UTF-8 locale that test cannot fail with
        the encodings unpinned, so this is the half that discriminates:
        measured, it passes as shipped and fails with either `encoding=`
        removed. Naming one node id keeps the child from re-collecting
        this wrapper, so there is no recursion."""
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}
        probe = subprocess.run(
            [sys.executable, "-c", "import locale; print(locale.getencoding())"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        encoding = probe.stdout.strip()
        if "utf" in encoding.lower():
            pytest.skip(f"this platform ignores LC_ALL=C (default encoding {encoding})")
        node = (
            f"{Path(__file__).name}::TestScaffoldRoundTripsItsOwnBytes"
            "::test_a_non_ascii_template_scaffolds_and_classifies_as_current"
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", node, "-q", "-p", "no:cacheprovider"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_upgrade_writes_the_same_bytes_the_scaffold_would_have(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--upgrade-prompts` and `ks init` must produce byte-identical
        files, or one of them lands on a body the ledger cannot name."""
        body = "# newer\n\n\u00e9\u2713\n"
        template = ScaffoldedTemplate(
            filename="prompt.md",
            constant_name="DEFAULT_PROMPT",
            body=body,
            history=((_sha256(OLD_BODY), "9.0.0"), (_sha256(body), "9.1.0")),
        )
        monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (template,))
        monkeypatch.setattr(init_cmd, "DEFAULT_PROMPT", body)

        upgraded = _prompt_at(tmp_path, OLD_BODY)
        run_init_capturing(tmp_path, upgrade_prompts=True)

        fresh_root = tmp_path / "fresh"
        fresh_root.mkdir()
        run_init_capturing(fresh_root)
        scaffolded = fresh_root / "scripts" / "kstrl" / "prompt.md"

        assert upgraded.read_bytes() == scaffolded.read_bytes()
        assert upgraded.read_bytes() == body.encode("utf-8")


class TestFeatureChecksThePromptItActuallyRuns:
    """`ks feature` is the one command whose engineer prompt is NOT
    ``config.prompt_file``: feature_cmd overwrites the resolved value
    with the literal ``scripts/kstrl/prompt.md`` for both the implement
    and the repair loop. So with ``[paths] prompt`` pointing elsewhere,
    checking the resolved path would warn about a file the command never
    opens and stay silent about the stale one it does.
    """

    def _project_with_a_relocated_prompt(
        self, tmp_path: Path, *, engineer_body: str, relocated_body: str
    ) -> Path:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        (kstrl_dir / "feature" / "demo").mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text(engineer_body)
        (kstrl_dir / "feature_understand_prompt.md").write_text("understand\n")
        (kstrl_dir / "codebase_map.md").write_text("# map\n")
        (kstrl_dir / "progress.txt").write_text("")
        (kstrl_dir / "feature" / "demo" / "prd.json").write_text(
            '{"branchName": "kstrl/demo", "userStories": []}'
        )
        elsewhere = project / "prompts"
        elsewhere.mkdir()
        (elsewhere / "prompt.md").write_text(relocated_body)
        (project / "kstrl.toml").write_text('[paths]\nprompt = "prompts/prompt.md"\n')
        return project

    def _invoke(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> Result:
        import kstrl.cli as cli_mod

        # Stop after the preflight: the phases beyond it need a live
        # agent, and what is under test is which path was checked.
        monkeypatch.setattr(cli_mod, "run_feature", lambda *a, **k: 0)
        return CliRunner().invoke(
            cli,
            [
                "feature",
                "--root",
                str(project),
                "--prd",
                "scripts/kstrl/feature/demo/prd.json",
                "--agent-cmd",
                "true",
                "--no-tui",
                "--ui",
                "plain",
                "--no-color",
            ],
        )

    def test_it_warns_about_the_scaffolded_prompt_it_runs_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        project = self._project_with_a_relocated_prompt(
            tmp_path, engineer_body=OLD_BODY, relocated_body=NEW_BODY
        )
        result = self._invoke(project, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "9.0.0" in result.output
        assert str(project / "scripts" / "kstrl" / "prompt.md") in result.output

    def test_it_stays_silent_about_the_relocated_file_it_never_opens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        project = self._project_with_a_relocated_prompt(
            tmp_path, engineer_body=NEW_BODY, relocated_body=OLD_BODY
        )
        result = self._invoke(project, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "9.0.0" not in result.output
        assert str(project / "prompts" / "prompt.md") not in result.output

    def test_the_understand_phase_is_still_covered_under_prompt_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """PROMPT_FILE makes cli pass ``understand_prompt_file=None``,
        and feature_cmd then leaves the understand loop on the resolved
        config. Checking only the engineer path lost that file entirely
        (regression found reviewing the first fix), so the preflight
        checks whichever of the two the phase will open."""
        project = self._project_with_a_relocated_prompt(
            tmp_path, engineer_body=NEW_BODY, relocated_body=OLD_BODY
        )
        monkeypatch.setenv("PROMPT_FILE", str(project / "prompts" / "prompt.md"))
        result = self._invoke(project, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "9.0.0" in result.output
        assert str(project / "prompts" / "prompt.md") in result.output

    def test_the_loop_runs_on_the_path_the_preflight_checked(self, tmp_path: Path) -> None:
        """The coupling, as a mechanism rather than a comment: the
        engineer prompt is resolved once by the caller and carried on
        FeatureParams, so the file warned about and the file the
        implement loop reads are the same object. It used to be a
        literal in cli.py matching a literal in feature_cmd.py, agreeing
        only by luck."""
        from unittest.mock import patch

        from kstrl.config import KstrlConfig
        from kstrl.feature_cmd import run_feature
        from kstrl.ui.plain import PlainUI
        from tests.test_feature_cmd import ScriptedChannel, StubAgent, _params

        seen: list[Path] = []

        def record(config, ui, agent, *args, **kwargs):  # type: ignore[no-untyped-def]
            from kstrl.loop import LoopResult

            seen.append(config.prompt_file)
            return LoopResult(completed=True, iterations=1, exit_code=0)

        params = _params(tmp_path)
        with patch("kstrl.feature_cmd.run_loop", record):
            run_feature(
                params,
                KstrlConfig(),
                StubAgent(),
                PlainUI(no_color=True, file=io.StringIO()),
                tmp_path,
                interaction=ScriptedChannel(0),
            )
        assert params.prompt_file in seen


class TestTheWizardPreviewDoesNotSayEverythingIsFine:
    """`ks init`'s TUI preview reports an existing prompt.md as
    "exists - kept". That action is honest (the wizard's own run_init
    call does leave it alone) but on its own it reads as "your scaffold
    is fine", which is the belief #286 exists to correct, on the surface
    most likely to be read that way.
    """

    def test_a_stale_prompt_carries_its_shipped_label(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        _prompt_at(tmp_path, OLD_BODY)
        entry = next(e for e in plan_scaffold(tmp_path) if e.path.name == "prompt.md")
        assert entry.action == "keep"
        assert entry.stale_label == "9.0.0"

    def test_nothing_else_carries_a_label(self, tmp_path: Path) -> None:
        """Same rule as the run-time warning: no claim we cannot prove.
        A current scaffold, an edited prompt and an absent one all say
        nothing, and the classifier's own answers for those three are
        pinned in TestClassification."""
        run_init_capturing(tmp_path)
        _prompt_at(tmp_path, DEFAULT_PROMPT + "\nmine\n")
        for entry in plan_scaffold(tmp_path):
            assert entry.stale_label is None


class TestOperatorActuallySeesIt:
    """The warning has to land on the operator's terminal.

    #275's migration warning went to the worker bus (``run_loop`` runs in
    a pool worker whose UI writes to that component's engineer.jsonl) and
    never reached the parent, which is why the check lives in the CLI
    preflight instead of beside the read in ``run_loop``. These tests
    drive the real command through CliRunner and assert on what it
    printed.
    """

    def _project(self, tmp_path: Path, body: str) -> Path:
        project = tmp_path / "project"
        kstrl_dir = project / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prompt.md").write_text(body)
        (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')
        (project / "kstrl.toml").write_text('[factory]\nreview_mode = "skip"\n')
        spine_git("init", "-q", "-b", "main", cwd=project)
        spine_git("config", "user.email", "cli@test", cwd=project)
        spine_git("config", "user.name", "CLI Test", cwd=project)
        spine_git("add", "-A", cwd=project)
        spine_git("commit", "-q", "-m", "init", cwd=project)
        return project

    def _invoke_run(self, project: Path) -> Result:
        return CliRunner().invoke(
            cli,
            [
                "run",
                "1",
                "--root",
                str(project),
                "--agent-cmd",
                "printf '<promise>COMPLETE</promise>\\n'",
                "--sleep",
                "0",
                "--no-verify",
                "--ui",
                "plain",
                "--no-color",
            ],
        )

    def test_ks_run_prints_the_warning_before_it_starts(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        result = self._invoke_run(self._project(tmp_path, OLD_BODY))
        assert result.exit_code == 0, result.output
        assert "9.0.0" in result.output
        assert "ks init --upgrade-prompts" in result.output
        # Before any agent work, not buried after it. "Preflight" is
        # deliberately NOT the anchor: run_loop prints that from inside
        # the worker, whose output never reaches this stream at all,
        # which is the whole reason the check is not in run_loop.
        assert "Preflight" not in result.output
        assert result.output.index("upgrade-prompts") < result.output.index(
            "Factory: Validating DAG"
        )

    def test_ks_run_stays_silent_on_a_current_prompt(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        result = self._invoke_run(self._project(tmp_path, NEW_BODY))
        assert result.exit_code == 0, result.output
        assert "upgrade-prompts" not in result.output

    def test_ks_run_stays_silent_on_an_edited_prompt(self, tmp_path: Path) -> None:
        """The real ledger, an ordinary hand-written prompt: no nag."""
        result = self._invoke_run(self._project(tmp_path, "just do the thing\n"))
        assert result.exit_code == 0, result.output
        assert "upgrade-prompts" not in result.output
