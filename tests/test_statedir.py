"""R8.9 control-state relocation: statedir, legacy refusal, L3 gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.io.statedir import (
    CONTROL_AUTONOMY,
    CONTROL_FILENAMES,
    control_dir,
    control_dir_accessible,
    control_file,
    control_is_external,
    ensure_control_state,
    legacy_control_paths,
    normalize_remote_url,
    repo_id,
)
from kstrl.queue.workqueue import Queue


@pytest.fixture(autouse=True)
def _clear_xdg_cache() -> None:
    from tests.helpers.xdg import clear_xdg_state_home_cache

    clear_xdg_state_home_cache()
    yield
    clear_xdg_state_home_cache()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestNormalizeRemoteUrl:
    def test_https_strips_git_suffix(self) -> None:
        assert normalize_remote_url("https://github.com/Org/Repo.git") == "github.com/org/repo"

    def test_ssh_scplike(self) -> None:
        assert normalize_remote_url("git@github.com:Org/Repo.git") == "github.com/org/repo"

    def test_ssh_url(self) -> None:
        assert normalize_remote_url("ssh://git@github.com/Org/Repo.git") == "github.com/org/repo"

    def test_git_suffix_casefold(self) -> None:
        assert normalize_remote_url("https://github.com/Org/Repo.GIT") == "github.com/org/repo"

    def test_strips_userinfo(self) -> None:
        assert (
            normalize_remote_url("https://user:token@github.com/Org/Repo.git")
            == "github.com/org/repo"
        )

    def test_strips_default_https_port(self) -> None:
        assert normalize_remote_url("https://github.com:443/Org/Repo.git") == "github.com/org/repo"

    def test_keeps_nondefault_port(self) -> None:
        assert (
            normalize_remote_url("https://git.example.com:8443/Org/Repo.git")
            == "git.example.com:8443/org/repo"
        )


class TestRepoId:
    def test_same_origin_forms_share_id(self, repo: Path) -> None:
        with patch(
            "kstrl.io.statedir._origin_url",
            return_value="https://github.com/acme/widget.git",
        ):
            a = repo_id(repo)
        with patch(
            "kstrl.io.statedir._origin_url",
            return_value="git@github.com:acme/widget.git",
        ):
            b = repo_id(repo)
        assert a == b
        assert a.startswith("widget-")

    def test_path_fallback_when_no_origin(self, repo: Path) -> None:
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            first = repo_id(repo)
            second = repo_id(repo)
        assert first == second
        assert "-" in first


class TestControlDir:
    def test_honors_xdg_state_home(self, repo: Path) -> None:
        xdg = Path(os.environ["XDG_STATE_HOME"]).resolve()
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            target = control_dir(repo)
        assert target.is_relative_to(xdg)
        assert target.parent.name == "kstrl"

    def test_control_file_rejects_unknown(self, repo: Path) -> None:
        with pytest.raises(ValueError, match="unknown control file"):
            control_file(repo, "nope.json")


class TestMigration:
    def test_a_legacy_file_is_refused_by_name_with_the_move_command(self, repo: Path) -> None:
        from kstrl.io.statedir import control_untrusted_reason

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            legacy = legacy_control_paths(repo)
            for name, path in legacy.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"legacy-{name}\n", encoding="utf-8")
            # Every control file that HAS a former in-tree location is
            # named. A post-R8.9 control file has none.
            assert set(legacy) < set(CONTROL_FILENAMES)

            ensure_control_state(repo)
            reason = control_untrusted_reason(repo)

            assert reason is not None
            for name, path in legacy.items():
                assert str(path) in reason
                assert f"mv {path} {control_file(repo, name)}" in reason
                # kstrl moved nothing: the file stays where the operator left it.
                assert path.read_text(encoding="utf-8") == f"legacy-{name}\n"
                assert not control_file(repo, name).exists()

    def test_a_post_migration_control_file_has_no_legacy_location(
        self,
        repo: Path,
    ) -> None:
        """R10.7 / #228: a control file added after R8.9 was never in-tree.

        It still has to be a real control file - resolvable through
        ``control_file``, and inside the set ``_control_files_compromised``
        checks for symlinks - without acquiring a former in-tree path that
        ``legacy_control_remaining`` would then look for forever.
        """
        from kstrl.io.statedir import CONTROL_PR_COUNT_STREAK

        assert CONTROL_PR_COUNT_STREAK in CONTROL_FILENAMES
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            assert CONTROL_PR_COUNT_STREAK not in legacy_control_paths(repo)
            path = control_file(repo, CONTROL_PR_COUNT_STREAK)
            assert path.parent == control_dir(repo)

    def test_ensure_then_consumers_use_xdg(self, repo: Path) -> None:
        from kstrl.learning.autonomy import AutonomyState
        from kstrl.owner.inbox import Inbox

        xdg = Path(os.environ["XDG_STATE_HOME"])
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            AutonomyState(level=2).save(repo)
            assert AutonomyState.path_for(repo).is_relative_to(xdg)
            assert Inbox(repo).path.is_relative_to(xdg)
            assert AutonomyState.path_for(repo).name == CONTROL_AUTONOMY


class TestControlIsExternal:
    def test_true_after_clean_ensure(self, repo: Path) -> None:
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            assert control_is_external(repo) is True

    def test_false_while_legacy_remains(self, repo: Path) -> None:
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            leftover = legacy_control_paths(repo)[CONTROL_AUTONOMY]
            leftover.parent.mkdir(parents=True, exist_ok=True)
            leftover.write_text("{}\n", encoding="utf-8")
            assert control_is_external(repo) is False

    def test_false_when_xdg_under_repo(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        nested = repo / "nested-xdg"
        nested.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            assert control_is_external(repo) is False


class TestPauseFailClosed:
    def test_inaccessible_control_dir_is_paused(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            monkeypatch.setattr(
                "kstrl.queue.workqueue_store.control_untrusted_reason",
                lambda _root: "control state directory inaccessible",
            )
            queue = Queue(repo)
            state = queue.pause_state()
            assert state.paused is True
            assert "inaccessible" in state.reason

    def test_missing_pause_marker_is_running(self, repo: Path) -> None:
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            assert control_dir_accessible(repo)
            queue = Queue(repo)
            assert queue.pause_state().paused is False


class TestAutonomyL3Gate:
    def test_resolve_clamps_l3_when_xdg_under_repo(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.learning.autonomy import AutonomyLevel, AutonomyState
        from kstrl.learning.autonomy_config import AutonomyConfig, resolve_runtime_level

        nested = repo / "nested-xdg"
        nested.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
            level, notes = resolve_runtime_level(
                state,
                AutonomyConfig(enabled=True, max_level=4),
                policy_enabled=True,
                root_dir=repo,
            )
            assert level is AutonomyLevel.L2_GATED_MERGE
            assert any("R8.9" in note for note in notes)

    def test_control_relocation_error_for_l3(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.learning.autonomy import AutonomyLevel
        from kstrl.learning.autonomy_config import control_relocation_error

        nested = repo / "nested-xdg"
        nested.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            err = control_relocation_error(
                repo,
                target_level=AutonomyLevel.L3_ENVELOPED_AUTO,
            )
            assert err is not None
            assert "R8.9" in err
            assert (
                control_relocation_error(
                    repo,
                    target_level=AutonomyLevel.L2_GATED_MERGE,
                )
                is None
            )

    def test_l3_allowed_when_external(self, repo: Path) -> None:
        from kstrl.learning.autonomy import AutonomyLevel, AutonomyState
        from kstrl.learning.autonomy_config import AutonomyConfig, resolve_runtime_level

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            state = AutonomyState(level=int(AutonomyLevel.L3_ENVELOPED_AUTO))
            level, notes = resolve_runtime_level(
                state,
                AutonomyConfig(enabled=True, max_level=4),
                policy_enabled=True,
                root_dir=repo,
            )
            assert level is AutonomyLevel.L3_ENVELOPED_AUTO
            assert notes == []


class TestLegacyHaltPaths:
    def test_all_legacy_control_paths_in_machinery(self) -> None:
        from kstrl.verify.policy import ENFORCEMENT_MACHINERY_PATHS

        expected = {
            ".kstrl/autonomy.json",
            ".kstrl/inbox.jsonl",
            ".kstrl/queue/spend.json",
            ".kstrl/queue/pause.json",
            ".kstrl/queue/github_processed.json",
            "**/kstrl/io/statedir.py",
            # #776 L10: the verifier and the autonomy ladder were divided
            # into modules. A diff that edits the moved code must halt as it
            # did before the move, so each module is a member.
            "**/kstrl/verify/verify.py",
            "**/kstrl/proc/scrubbed_run.py",
            "**/kstrl/verify/verify_model.py",
            "**/kstrl/verify/verify_commands.py",
            "**/kstrl/verify/verify_diff.py",
            "**/kstrl/learning/autonomy.py",
            "**/kstrl/learning/autonomy_config.py",
            "**/kstrl/learning/autonomy_transitions.py",
        }
        for path in expected:
            assert path in ENFORCEMENT_MACHINERY_PATHS

    def test_each_kstrl_machinery_path_names_a_file_of_this_tree(self) -> None:
        """#863 moves modules into packages. A ``**/kstrl/`` row that names
        the old path of a moved module matches no file, so the halt for that
        file stops with no message. The test above compares strings only, and
        a codemod that rewrites both sides keeps it green. This test reads
        the tree: each row must name a file that exists, and that file must
        match its own row."""
        from kstrl.verify.policy import ENFORCEMENT_MACHINERY_PATHS, match_glob

        root = Path(__file__).resolve().parent.parent
        rows = [row for row in ENFORCEMENT_MACHINERY_PATHS if row.startswith("**/kstrl/")]
        assert rows, "no **/kstrl/ row: the walk below examines nothing"
        for row in rows:
            rel = row.removeprefix("**/")
            assert (root / rel).is_file(), f"{row} names no file of this tree"
            assert match_glob(rel, ENFORCEMENT_MACHINERY_PATHS) == row


class TestReviewFailClosed:
    """Regressions for the adversarial review on PR #213."""

    def test_an_in_tree_inbox_is_refused_by_ks_inbox_and_fails_the_gates_closed(
        self,
        repo: Path,
    ) -> None:
        """#863: kstrl no longer moves ``.kstrl/inbox.jsonl``. Measured before
        this test: ``ks inbox ls`` printed "Inbox clear" with an open item in
        the tree, and every gate read only the empty control-directory log."""
        from click.testing import CliRunner

        from kstrl.cli import cli
        from kstrl.io.statedir import CONTROL_INBOX
        from kstrl.owner.inbox import Inbox

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            legacy = legacy_control_paths(repo)[CONTROL_INBOX]
            legacy.parent.mkdir(parents=True, exist_ok=True)
            row = '{"schema_version":1,"id":"a1","kind":"halted_run","title":"t","status":"open"}\n'
            legacy.write_text(row, encoding="utf-8")

            for argv in (["inbox", "ls"], ["inbox", "show", "a1"]):
                result = CliRunner().invoke(cli, [*argv, "--root", str(repo)])
                assert result.exit_code == 2, result.output
                assert ".kstrl/inbox.jsonl" in result.output
                assert "mv " in result.output
                assert "Inbox clear" not in result.output

            assert Inbox(repo).scan().unreadable is True
            # kstrl moved nothing.
            assert legacy.read_text(encoding="utf-8") == row
            assert not control_file(repo, CONTROL_INBOX).exists()

    def test_a_legacy_file_keeps_pause_and_spend_closed(
        self,
        repo: Path,
    ) -> None:
        from kstrl.io.statedir import CONTROL_PAUSE, CONTROL_SPEND
        from kstrl.serve_spend import ServeStateError, SpendLedger

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            legacy = legacy_control_paths(repo)
            legacy[CONTROL_PAUSE].parent.mkdir(parents=True, exist_ok=True)
            legacy[CONTROL_PAUSE].write_text(
                json.dumps({"paused": True, "reason": "budget", "since": ""}),
                encoding="utf-8",
            )
            legacy[CONTROL_SPEND].write_text(
                json.dumps(
                    {
                        "spend": {
                            "date": "2099-01-01",
                            "spent_usd": 12.5,
                            "runs": 1,
                            "covered_calls": 1,
                            "total_calls": 1,
                            "unmetered_phases": [],
                        },
                        "consecutive_poison": 0,
                        "cost_coverage_seen": True,
                    }
                ),
                encoding="utf-8",
            )

            ensure_control_state(repo)
            assert legacy[CONTROL_PAUSE].exists()
            assert legacy[CONTROL_SPEND].exists()
            queue = Queue(repo)
            assert queue.pause_state().paused is True
            assert "in-tree" in queue.pause_state().reason
            with pytest.raises(ServeStateError, match="in-tree"):
                SpendLedger(repo).read_state("2099-01-01")

    def test_relative_xdg_survives_chdir(
        self,
        repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.helpers.xdg import clear_xdg_state_home_cache

        rel = Path("rel-xdg-state")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(rel))
        clear_xdg_state_home_cache()
        with patch("kstrl.io.statedir._origin_url", return_value=None):
            queue = Queue(repo)
            queue.pause(reason="budget")
            assert queue.is_paused()
            work = tmp_path / "workdir"
            work.mkdir()
            monkeypatch.chdir(work)
            assert queue.is_paused()

    def test_dual_state_fail_closes_pause_and_spend(self, repo: Path) -> None:
        from kstrl.io.statedir import CONTROL_PAUSE, CONTROL_SPEND, control_file
        from kstrl.serve_spend import ServeStateError, SpendLedger

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            control_file(repo, CONTROL_PAUSE).write_text(
                json.dumps({"paused": False, "reason": "", "since": ""}),
                encoding="utf-8",
            )
            control_file(repo, CONTROL_SPEND).write_text(
                json.dumps(
                    {
                        "spend": {
                            "date": "2099-01-01",
                            "spent_usd": 1.0,
                            "runs": 1,
                            "covered_calls": 1,
                            "total_calls": 1,
                            "unmetered_phases": [],
                        },
                        "consecutive_poison": 0,
                        "cost_coverage_seen": True,
                    }
                ),
                encoding="utf-8",
            )
            legacy = legacy_control_paths(repo)
            legacy[CONTROL_PAUSE].parent.mkdir(parents=True, exist_ok=True)
            legacy[CONTROL_PAUSE].write_text(
                json.dumps({"paused": True, "reason": "legacy", "since": ""}),
                encoding="utf-8",
            )
            legacy[CONTROL_SPEND].write_text(
                json.dumps(
                    {
                        "spend": {
                            "date": "2099-01-01",
                            "spent_usd": 99.0,
                            "runs": 1,
                            "covered_calls": 1,
                            "total_calls": 1,
                            "unmetered_phases": [],
                        },
                        "consecutive_poison": 0,
                        "cost_coverage_seen": True,
                    }
                ),
                encoding="utf-8",
            )
            ensure_control_state(repo)
            assert Queue(repo).pause_state().paused is True
            with pytest.raises(ServeStateError, match="in-tree"):
                SpendLedger(repo).read_state("2099-01-01")

    def test_symlink_control_file_not_external(self, repo: Path) -> None:
        from kstrl.io.statedir import CONTROL_AUTONOMY, control_file

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            target = repo / "evil-autonomy.json"
            target.write_text("{}\n", encoding="utf-8")
            path = control_file(repo, CONTROL_AUTONOMY)
            path.symlink_to(target)
            assert control_is_external(repo) is False
            assert Queue(repo).pause_state().paused is True

    def test_control_lock_raises_when_mkdir_fails(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.io.statedir import ControlUnavailableError, control_lock

        with patch("kstrl.io.statedir._origin_url", return_value=None):
            ensure_control_state(repo)

            def boom(self: Path, *args: object, **kwargs: object) -> None:
                raise OSError("permission denied")

            monkeypatch.setattr(Path, "mkdir", boom)
            with pytest.raises(ControlUnavailableError):
                with control_lock(repo):
                    pass
