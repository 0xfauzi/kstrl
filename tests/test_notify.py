"""R3.2 notification hooks + default-on progress log.

Covers:
- NotifyConfig toml/env resolution ([notify] section).
- NotifyHooks once-per-condition semantics via a counting stub command,
  context env vars, and non-fatal failure modes (nonzero exit, missing
  binary, timeout).
- Factory wiring: default-on progress log written under .ralph/ with
  run_id on events; configurable off; on_first_failure and on_complete
  fired exactly once per run through real run_factory calls; a
  MERGE_PENDING park fires the attention hook (real git + stub gh).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ralph_py.config import RalphConfig
from ralph_py.factory import ComponentResult, FactoryConfig, run_factory
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.observability import (
    NotifyConfig,
    NotifyHooks,
    latest_run_id,
    read_progress_events,
)
from ralph_py.ui.plain import PlainUI
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    git,
    make_manifest,
    write_stub_gh,
)


def _count_cmd(count_file: Path) -> str:
    """Shell command that appends one line per invocation."""
    return f"echo \"$RALPH_NOTIFY_EVENT $RALPH_NOTIFY_COMPONENT\" >> '{count_file}'"


def _lines(count_file: Path) -> list[str]:
    if not count_file.exists():
        return []
    return [
        line for line in count_file.read_text().splitlines() if line.strip()
    ]


class TestNotifyConfig:
    def test_defaults(self) -> None:
        config = NotifyConfig()
        assert config.on_complete == ""
        assert config.on_first_failure == ""
        assert config.hook_timeout == 30.0

    def test_load_reads_notify_section(self, tmp_path: Path) -> None:
        (tmp_path / "ralph.toml").write_text(
            "[notify]\n"
            "on_complete = \"printf 'a'\"\n"
            'on_first_failure = "curl example.com"\n'
            "hook_timeout = 5.0\n"
        )
        config = NotifyConfig.load(tmp_path)
        assert config.on_complete == "printf 'a'"
        assert config.on_first_failure == "curl example.com"
        assert config.hook_timeout == 5.0

    def test_env_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            '[notify]\non_complete = "from-toml"\n'
        )
        monkeypatch.setenv("RALPH_NOTIFY_ON_COMPLETE", "from-env")
        monkeypatch.setenv("RALPH_NOTIFY_ON_FIRST_FAILURE", "fail-env")
        config = NotifyConfig.load(tmp_path)
        assert config.on_complete == "from-env"
        assert config.on_first_failure == "fail-env"

    def test_missing_section_is_defaults(self, tmp_path: Path) -> None:
        config = NotifyConfig.load(tmp_path)
        assert config == NotifyConfig()


class TestNotifyHooksOnce:
    """Each condition fires its command at most once per run."""

    def test_first_failure_fires_once(self, tmp_path: Path) -> None:
        count = tmp_path / "count.txt"
        hooks = NotifyHooks(
            NotifyConfig(on_first_failure=_count_cmd(count)),
            run_id="r1", project="demo",
        )
        hooks.fire_first_failure("comp-a", "boom")
        hooks.fire_first_failure("comp-b", "boom again")
        assert _lines(count) == ["first_failure comp-a"]

    def test_complete_fires_once(self, tmp_path: Path) -> None:
        count = tmp_path / "count.txt"
        hooks = NotifyHooks(NotifyConfig(on_complete=_count_cmd(count)))
        hooks.fire_complete("done")
        hooks.fire_complete("done again")
        assert _lines(count) == ["run_complete "]

    def test_merge_pending_fires_once_and_is_distinct(
        self, tmp_path: Path,
    ) -> None:
        """merge_pending uses the on_first_failure command but is its own
        once-per-run condition: a later real failure still notifies."""
        count = tmp_path / "count.txt"
        hooks = NotifyHooks(
            NotifyConfig(on_first_failure=_count_cmd(count)),
        )
        hooks.fire_merge_pending("comp-a", "awaiting merge")
        hooks.fire_merge_pending("comp-b", "awaiting merge")
        hooks.fire_first_failure("comp-c", "boom")
        assert _lines(count) == [
            "merge_pending comp-a",
            "first_failure comp-c",
        ]

    def test_context_env_vars_reach_command(self, tmp_path: Path) -> None:
        out = tmp_path / "env.txt"
        cmd = (
            'echo "$RALPH_NOTIFY_EVENT|$RALPH_NOTIFY_RUN_ID'
            '|$RALPH_NOTIFY_PROJECT|$RALPH_NOTIFY_COMPONENT'
            f"|$RALPH_NOTIFY_DETAIL\" > '{out}'"
        )
        hooks = NotifyHooks(
            NotifyConfig(on_first_failure=cmd), run_id="r9", project="demo",
        )
        hooks.fire_first_failure("comp-a", "tests failed")
        assert out.read_text().strip() == (
            "first_failure|r9|demo|comp-a|tests failed"
        )

    def test_empty_command_is_noop(self, tmp_path: Path) -> None:
        hooks = NotifyHooks(NotifyConfig())
        hooks.fire_complete()
        hooks.fire_first_failure("comp-a", "boom")
        hooks.fire_merge_pending("comp-a")
        # Nothing to assert beyond "no exception, no file writes".


class TestNotifyHooksNonFatal:
    """Hook failures warn and never raise."""

    def test_nonzero_exit_warns(self) -> None:
        warnings: list[str] = []
        hooks = NotifyHooks(
            NotifyConfig(on_complete="exit 3"), warn=warnings.append,
        )
        hooks.fire_complete()
        assert len(warnings) == 1
        assert "exited 3" in warnings[0]

    def test_missing_binary_warns(self) -> None:
        warnings: list[str] = []
        hooks = NotifyHooks(
            NotifyConfig(
                on_complete="/nonexistent/ralph-notify-binary-xyz",
            ),
            warn=warnings.append,
        )
        hooks.fire_complete()
        # shell=True: the shell itself launches and exits 127.
        assert len(warnings) == 1

    def test_timeout_warns_and_does_not_raise(self) -> None:
        warnings: list[str] = []
        hooks = NotifyHooks(
            NotifyConfig(on_complete="sleep 30", hook_timeout=0.2),
            warn=warnings.append,
        )
        hooks.fire_complete()
        assert len(warnings) == 1
        assert "timed out" in warnings[0]

    def test_crashing_hook_never_retries(self, tmp_path: Path) -> None:
        count = tmp_path / "count.txt"
        cmd = f"echo x >> '{count}'; exit 1"
        warnings: list[str] = []
        hooks = NotifyHooks(
            NotifyConfig(on_first_failure=cmd), warn=warnings.append,
        )
        hooks.fire_first_failure("comp-a", "boom")
        hooks.fire_first_failure("comp-b", "boom")
        assert len(_lines(count)) == 1


def _plain_factory_config(**overrides: object) -> FactoryConfig:
    config = FactoryConfig(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        skip_verification=True,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _setup_plain_project(tmp_path: Path) -> Path:
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    return tmp_path


def _plain_base_config(root: Path) -> RalphConfig:
    return RalphConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _two_component_manifest() -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="demo",
        base_branch="main", single_pr=False,
        components=[
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
        ],
    )


class TestFactoryProgressLogDefaultOn:
    """R3.2 requirement 1: the log defaults on under .ralph/."""

    def test_default_on_writes_log_with_run_id(self, tmp_path: Path) -> None:
        root = _setup_plain_project(tmp_path)
        manifest = make_manifest([])
        result = run_factory(
            manifest, _plain_factory_config(), _plain_base_config(root),
            PlainUI(no_color=True), root,
        )
        assert result.exit_code == 0

        log_path = root / ".ralph" / "progress.jsonl"
        assert log_path.exists()
        events = read_progress_events(log_path)
        assert [e["event"] for e in events] == [
            "factory_started", "factory_completed",
        ]
        assert latest_run_id(events) != ""

    def test_disabled_writes_nothing(self, tmp_path: Path) -> None:
        root = _setup_plain_project(tmp_path)
        result = run_factory(
            make_manifest([]),
            _plain_factory_config(progress_log_enabled=False),
            _plain_base_config(root), PlainUI(no_color=True), root,
        )
        assert result.exit_code == 0
        assert not (root / ".ralph" / "progress.jsonl").exists()

    def test_explicit_path_overrides_default(self, tmp_path: Path) -> None:
        root = _setup_plain_project(tmp_path)
        custom = root / "custom-progress.jsonl"
        run_factory(
            make_manifest([]),
            _plain_factory_config(progress_log_path=custom),
            _plain_base_config(root), PlainUI(no_color=True), root,
        )
        assert custom.exists()
        assert not (root / ".ralph" / "progress.jsonl").exists()

    def test_two_runs_share_log_distinguishably(self, tmp_path: Path) -> None:
        root = _setup_plain_project(tmp_path)
        for _ in range(2):
            run_factory(
                make_manifest([]), _plain_factory_config(),
                _plain_base_config(root), PlainUI(no_color=True), root,
            )
        events = read_progress_events(root / ".ralph" / "progress.jsonl")
        run_ids = {e["run_id"] for e in events}
        assert len(run_ids) == 2


class TestFactoryFiresHooks:
    """R3.2 requirement 3 through real run_factory calls."""

    def test_failing_run_fires_first_failure_and_complete_once(
        self, tmp_path: Path,
    ) -> None:
        root = _setup_plain_project(tmp_path)
        fail_count = tmp_path / "fail-count.txt"
        complete_count = tmp_path / "complete-count.txt"
        config = _plain_factory_config(notify_config=NotifyConfig(
            on_first_failure=_count_cmd(fail_count),
            on_complete=_count_cmd(complete_count),
        ))

        def fake_run(
            component_id: str, *args: object, **kwargs: object,
        ) -> ComponentResult:
            return ComponentResult(component_id, success=False, error="boom")

        with patch("ralph_py.factory._run_component", side_effect=fake_run):
            result = run_factory(
                _two_component_manifest(), config, _plain_base_config(root),
                PlainUI(no_color=True), root,
            )

        # Two independent components both failed; the hook fired once.
        assert len(result.failed) == 2
        assert _lines(fail_count) == ["first_failure a"]
        assert _lines(complete_count) == ["run_complete "]

    def test_clean_run_fires_only_complete(self, tmp_path: Path) -> None:
        root = _setup_plain_project(tmp_path)
        fail_count = tmp_path / "fail-count.txt"
        complete_count = tmp_path / "complete-count.txt"
        config = _plain_factory_config(notify_config=NotifyConfig(
            on_first_failure=_count_cmd(fail_count),
            on_complete=_count_cmd(complete_count),
        ))

        result = run_factory(
            make_manifest([]), config, _plain_base_config(root),
            PlainUI(no_color=True), root,
        )
        assert result.exit_code == 0
        assert _lines(fail_count) == []
        assert _lines(complete_count) == ["run_complete "]

    def test_hook_failure_never_affects_the_run(self, tmp_path: Path) -> None:
        root = _setup_plain_project(tmp_path)
        config = _plain_factory_config(notify_config=NotifyConfig(
            on_complete="/nonexistent/ralph-hook-xyz",
            on_first_failure="exit 7",
        ))

        def fake_run(
            component_id: str, *args: object, **kwargs: object,
        ) -> ComponentResult:
            return ComponentResult(component_id, success=False, error="boom")

        with patch("ralph_py.factory._run_component", side_effect=fake_run):
            result = run_factory(
                _two_component_manifest(), config, _plain_base_config(root),
                PlainUI(no_color=True), root,
            )

        # Failure exit code comes from the failed components, not hooks.
        assert result.exit_code == 1
        assert len(result.failed) == 2


def _make_pr_repo(tmp_path: Path, comp_ids: tuple[str, ...]) -> Path:
    """Real git repo with COMMITTED ralph scaffolding plus a bare origin,
    so worktrees contain the PRDs without the (mocked-out) engineer's
    provisioning step (mirrors tests/test_pr_outcomes.py)."""
    root = tmp_path / "repo"
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for cid in comp_ids:
        feature = ralph_dir / "feature" / cid
        feature.mkdir(parents=True)
        (feature / "prd.json").write_text(json.dumps({
            "branchName": f"ralph/factory/{cid}",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
    (root / ".gitignore").write_text(
        ".ralph/\nscripts/ralph/manifest.json\n"
    )
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "notify@test", cwd=root)
    git("config", "user.name", "Notify Test", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("remote", "add", "origin", str(origin), cwd=root)
    git("push", "-q", "-u", "origin", "main", cwd=root)
    return root


class TestMergePendingFiresHook:
    """A MERGE_PENDING park pings the attention hook (real git + stub
    gh whose `pr view` never reports MERGED)."""

    def test_merge_pending_park_fires_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_pr_repo(tmp_path, ("alpha",))
        bin_dir = tmp_path / "stub-bin"
        bin_dir.mkdir()
        write_stub_gh(bin_dir)
        monkeypatch.setenv(
            "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        )
        monkeypatch.setenv("GH_SPINE_VIEW_STATE", "OPEN")
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        count = tmp_path / "count.txt"
        config = factory_config(
            create_prs=True, merge_timeout=1.0,
            notify_config=NotifyConfig(on_first_failure=_count_cmd(count)),
        )
        manifest = make_manifest([component("alpha")])

        def fake_run(
            component_id: str, *args: object, **kwargs: object,
        ) -> ComponentResult:
            return ComponentResult(component_id, success=True, iterations=1)

        with patch("ralph_py.factory._run_component", side_effect=fake_run):
            result = run_factory(
                manifest, config, base_config(root),
                PlainUI(no_color=True), root,
            )

        alpha = manifest.get_component("alpha")
        assert alpha is not None
        assert alpha.status == ComponentStatus.MERGE_PENDING.value
        assert result.merge_pending == ["alpha"]
        assert _lines(count) == ["merge_pending alpha"]
        # The park is also visible in the progress log for `ralph status`.
        events = read_progress_events(root / ".ralph" / "progress.jsonl")
        assert "merge_pending" in [e["event"] for e in events]


class TestNotifyStubIsCounted:
    """Meta-check: the counting stub counts every invocation, so the
    exactly-once assertions above are behavior tests, not vacuous."""

    def test_stub_counts_each_call(self, tmp_path: Path) -> None:
        count = tmp_path / "count.txt"
        for _ in range(3):
            subprocess.run(
                _count_cmd(count), shell=True, check=True,
                env={**os.environ, "RALPH_NOTIFY_EVENT": "e",
                     "RALPH_NOTIFY_COMPONENT": "c"},
            )
        assert len(_lines(count)) == 3


def test_progress_log_survives_manifest_json_shape(tmp_path: Path) -> None:
    """Regression guard: events written by a real run parse back as
    dicts (json round-trip, one object per line)."""
    root = _setup_plain_project(tmp_path)
    run_factory(
        make_manifest([]), _plain_factory_config(),
        _plain_base_config(root), PlainUI(no_color=True), root,
    )
    raw = (root / ".ralph" / "progress.jsonl").read_text().splitlines()
    for line in raw:
        assert isinstance(json.loads(line), dict)
