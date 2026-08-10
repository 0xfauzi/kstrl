"""R8.9 control-state relocation: statedir, migration, L3 gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.statedir import (
    CONTROL_AUTONOMY,
    CONTROL_FILENAMES,
    control_dir,
    control_dir_accessible,
    control_file,
    control_is_external,
    ensure_control_state,
    legacy_control_paths,
    migrate_control_state,
    normalize_remote_url,
    repo_id,
    state_dir,
)
from kstrl.workqueue import Queue


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestNormalizeRemoteUrl:
    def test_https_strips_git_suffix(self) -> None:
        assert (
            normalize_remote_url("https://github.com/Org/Repo.git")
            == "github.com/org/repo"
        )

    def test_ssh_scplike(self) -> None:
        assert (
            normalize_remote_url("git@github.com:Org/Repo.git")
            == "github.com/org/repo"
        )

    def test_ssh_url(self) -> None:
        assert (
            normalize_remote_url("ssh://git@github.com/Org/Repo.git")
            == "github.com/org/repo"
        )


class TestRepoId:
    def test_same_origin_forms_share_id(self, repo: Path) -> None:
        with patch(
            "kstrl.statedir._origin_url",
            return_value="https://github.com/acme/widget.git",
        ):
            a = repo_id(repo)
        with patch(
            "kstrl.statedir._origin_url",
            return_value="git@github.com:acme/widget.git",
        ):
            b = repo_id(repo)
        assert a == b
        assert a.startswith("widget-")

    def test_path_fallback_when_no_origin(self, repo: Path) -> None:
        with patch("kstrl.statedir._origin_url", return_value=None):
            first = repo_id(repo)
            second = repo_id(repo)
        assert first == second
        assert "-" in first


class TestControlDir:
    def test_honors_xdg_state_home(self, repo: Path) -> None:
        xdg = Path(os.environ["XDG_STATE_HOME"])
        with patch("kstrl.statedir._origin_url", return_value=None):
            target = control_dir(repo)
        assert target.is_relative_to(xdg)
        assert target.parent.name == "kstrl"

    def test_control_file_rejects_unknown(self, repo: Path) -> None:
        with pytest.raises(ValueError, match="unknown control file"):
            control_file(repo, "nope.json")


class TestMigration:
    def test_moves_legacy_files_once(self, repo: Path) -> None:
        with patch("kstrl.statedir._origin_url", return_value=None):
            legacy = legacy_control_paths(repo)
            for name, path in legacy.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"legacy-{name}\n", encoding="utf-8")

            with pytest.warns(DeprecationWarning, match="relocated control file"):
                moved = migrate_control_state(repo)
            assert set(moved) == set(CONTROL_FILENAMES)

            for name in CONTROL_FILENAMES:
                assert not legacy[name].exists()
                target = control_file(repo, name)
                assert target.read_text(encoding="utf-8") == f"legacy-{name}\n"

            marker = state_dir(repo) / "control_relocated"
            assert marker.exists()
            payload = json.loads(marker.read_text(encoding="utf-8"))
            assert payload["control_dir"] == str(control_dir(repo))

            # Second call is a no-op.
            assert migrate_control_state(repo) == []

    def test_ensure_then_consumers_use_xdg(self, repo: Path) -> None:
        from kstrl.autonomy import AutonomyState
        from kstrl.inbox import Inbox

        xdg = Path(os.environ["XDG_STATE_HOME"])
        with patch("kstrl.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            AutonomyState(level=2).save(repo)
            assert AutonomyState.path_for(repo).is_relative_to(xdg)
            assert Inbox(repo).path.is_relative_to(xdg)
            assert AutonomyState.path_for(repo).name == CONTROL_AUTONOMY


class TestControlIsExternal:
    def test_true_after_clean_ensure(self, repo: Path) -> None:
        with patch("kstrl.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            assert control_is_external(repo) is True

    def test_false_while_legacy_remains(self, repo: Path) -> None:
        with patch("kstrl.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            leftover = legacy_control_paths(repo)[CONTROL_AUTONOMY]
            leftover.parent.mkdir(parents=True, exist_ok=True)
            leftover.write_text("{}\n", encoding="utf-8")
            assert control_is_external(repo) is False

    def test_false_when_xdg_under_repo(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        nested = repo / "nested-xdg"
        nested.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        with patch("kstrl.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            assert control_is_external(repo) is False


class TestPauseFailClosed:
    def test_inaccessible_control_dir_is_paused(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with patch("kstrl.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            monkeypatch.setattr(
                "kstrl.workqueue.control_dir_accessible",
                lambda _root: False,
            )
            queue = Queue(repo)
            state = queue.pause_state()
            assert state.paused is True
            assert "inaccessible" in state.reason

    def test_missing_pause_marker_is_running(self, repo: Path) -> None:
        with patch("kstrl.statedir._origin_url", return_value=None):
            ensure_control_state(repo)
            assert control_dir_accessible(repo)
            queue = Queue(repo)
            assert queue.pause_state().paused is False


class TestAutonomyL3Gate:
    def test_resolve_clamps_l3_when_xdg_under_repo(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.autonomy import (
            AutonomyConfig,
            AutonomyLevel,
            AutonomyState,
            resolve_runtime_level,
        )

        nested = repo / "nested-xdg"
        nested.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        with patch("kstrl.statedir._origin_url", return_value=None):
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
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl.autonomy import AutonomyLevel, control_relocation_error

        nested = repo / "nested-xdg"
        nested.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        with patch("kstrl.statedir._origin_url", return_value=None):
            err = control_relocation_error(
                repo, target_level=AutonomyLevel.L3_ENVELOPED_AUTO,
            )
            assert err is not None
            assert "R8.9" in err
            assert (
                control_relocation_error(
                    repo, target_level=AutonomyLevel.L2_GATED_MERGE,
                )
                is None
            )

    def test_l3_allowed_when_external(self, repo: Path) -> None:
        from kstrl.autonomy import (
            AutonomyConfig,
            AutonomyLevel,
            AutonomyState,
            resolve_runtime_level,
        )

        with patch("kstrl.statedir._origin_url", return_value=None):
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
        from kstrl.policy import ENFORCEMENT_MACHINERY_PATHS

        expected = {
            ".kstrl/autonomy.json",
            ".kstrl/inbox.jsonl",
            ".kstrl/queue/spend.json",
            ".kstrl/queue/pause.json",
            ".kstrl/queue/github_processed.json",
            "**/kstrl/statedir.py",
        }
        for path in expected:
            assert path in ENFORCEMENT_MACHINERY_PATHS
