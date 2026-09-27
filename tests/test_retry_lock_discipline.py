"""A command that changes run state does it under the run lock, after its confirmation (#597).

The defect: `ks retry` reset the manifest, deleted the failed branch and
removed the kept evidence worktree before it asked Start/Quit and before
anything took ``.kstrl/factory.lock``, so Quit, or a refusal because a live
run held the lock, left all of that done. `ks inbox retry` saved the
requeue and closed the inbox item without looking at the lock, and a live
run's next whole-document manifest save undid the requeue. `ks inbox
approve` on a parked merge recorded the decision before the run it starts
was refused on the lock (#596 item 2). `ks decompose` and `ks factory
--spec` wrote a new manifest under a live run.

Every test drives the real CLI against a real git repository with a stub
engineer and no LLM. The run lock is held by a child process the test
starts and kills by its own process group, never found by name.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
from kstrl.inbox import Inbox, InboxConfig
from kstrl.interaction import PromptRequest, PromptResponse
from kstrl.manifest import Manifest
from tests import test_merge_gate_park as park
from tests.helpers.procs import kill_group, wait_for_line
from tests.test_build_manifest_preflight import MANIFESTS, greenfield, run_ks
from tests.test_decompose import VALID_DECOMPOSE_OUTPUT
from tests.test_retry_carries_flags import RUN_FLAGS, _env, _failed_run, _ks, _repo

STORAGE_BRANCH = "refs/heads/kstrl/factory/storage"

#: Bound for the lock holder's ready line, so a holder that never locks
#: fails as HUNG instead of hanging the test.
_HOLDER_TIMEOUT_SECONDS = 10.0

_HOLDER = """
import fcntl, sys, time
fp = open(sys.argv[1], "a+")
fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(120)
"""


#: A stub engineer that records whether the run lock is held while it runs,
#: then reports the story complete. Its verdict is "held" or "free".
_LOCK_PROBING_ENGINEER = """
import fcntl, sys
sys.stdin.read()
with open(sys.argv[1], "a+", encoding="utf-8") as fp:
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        verdict = "free"
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except OSError:
        verdict = "held"
with open(sys.argv[2], "a", encoding="utf-8") as out:
    out.write(verdict + "\\n")
print("<promise>COMPLETE</promise>")
"""


@pytest.fixture
def hold_lock() -> Iterator[Any]:
    """Start a child that holds ``<root>/.kstrl/factory.lock``; kill its group at teardown."""
    started: list[subprocess.Popen[str]] = []

    def _hold(root: Path) -> None:
        lock = root / ".kstrl" / "factory.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(lock)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        started.append(holder)
        wait_for_line(holder, "locked", _HOLDER_TIMEOUT_SECONDS)

    yield _hold
    for holder in started:
        kill_group(holder.pid)
        holder.wait(timeout=10)


def _manifest_file(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def _branch(root: Path) -> str:
    """The failed branch's tip, or "" when it is gone."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", STORAGE_BRANCH],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    ).stdout.strip()


def _state(root: Path) -> tuple[bytes, str, str, bool]:
    """What a retry changes: the manifest's bytes, the failed branch, the kept worktree."""
    comp = Manifest.load(_manifest_file(root)).get_component("storage")
    assert comp is not None
    worktree = comp.evidence_worktree
    return (
        _manifest_file(root).read_bytes(),
        _branch(root),
        worktree,
        bool(worktree) and Path(worktree).exists(),
    )


def _failed_storage(tmp_path: Path) -> tuple[Path, tuple[bytes, str, str, bool]]:
    """`storage` failed with its branch and evidence worktree kept; the state before any retry."""
    root = _repo(tmp_path)
    _failed_run(root, "--max-cost-usd", "5", "--keep-worktrees-on-failure", *RUN_FLAGS)
    before = _state(root)
    assert before[1] and before[3], before[1:]
    return root, before


def _lock_is_free(root: Path) -> bool:
    with open(root / ".kstrl" / "factory.lock", "a+", encoding="utf-8") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return True


class _Channel:
    """Stands in for the terminal: records the question and gives ``response``.

    ``on_ask`` runs while the question is open, the way a live run's save
    would land while an operator reads it.
    """

    asked: list[str] = []
    choice = 1
    answered = True
    on_ask: Any = None

    def __init__(self, ui: Any) -> None:
        pass

    def can_prompt(self) -> bool:
        return True

    def request(self, req: PromptRequest) -> PromptResponse:
        _Channel.asked.append(req.header)
        if _Channel.on_ask is not None:
            _Channel.on_ask()
        return PromptResponse(
            request_id=req.request_id, choice=_Channel.choice, answered=_Channel.answered
        )


def _retry_in_process(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    choice: int,
    answered: bool,
    on_ask: Any = None,
    agent_cmd: str | None = None,
) -> Any:
    _Channel.asked = []
    _Channel.choice, _Channel.answered, _Channel.on_ask = choice, answered, on_ask
    monkeypatch.setattr(cli_mod, "UiInteractionChannel", _Channel)
    # _env() drops every KSTRL_ knob from the copy it returns; setenv alone
    # would leave the caller's own in this process's environment.
    for name in [k for k in os.environ if k.startswith("KSTRL_")]:
        monkeypatch.delenv(name)
    env = _env() if agent_cmd is None else _env(AGENT_CMD=agent_cmd)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return CliRunner().invoke(
        cli_mod.cli, ["retry", "storage", "--root", str(root), "--ui", "plain", "--no-color"]
    )


class TestKsRetryChangesNothingBeforeItsGates:
    def test_quit_at_the_confirmation_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, before = _failed_storage(tmp_path)

        result = _retry_in_process(root, monkeypatch, choice=1, answered=True)

        assert result.exit_code == 0, result.output
        assert len(_Channel.asked) == 1, result.output
        assert _state(root) == before, result.output

    def test_an_unanswered_confirmation_changes_nothing_and_starts_no_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-C or end of input at the question is not Start."""
        root, before = _failed_storage(tmp_path)

        result = _retry_in_process(root, monkeypatch, choice=0, answered=False)

        assert result.exit_code == 0, result.output
        assert len(_Channel.asked) == 1, result.output
        assert "Starting:" not in result.output, result.output
        assert _state(root) == before, result.output

    def test_a_held_lock_refuses_the_retry_before_it_changes_anything(
        self, tmp_path: Path, hold_lock: Any
    ) -> None:
        root, before = _failed_storage(tmp_path)
        hold_lock(root)

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr

        assert retried.returncode == 2, out
        assert "refusing to start a second factory run" in out, out
        # The refusal is the lock's, not git's "branch used by worktree".
        assert "Delete it manually" not in out, out
        assert _state(root) == before, out

    def test_a_manifest_saved_while_the_question_was_open_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, before = _failed_storage(tmp_path)
        saved_by_a_run: list[bytes] = []

        def a_run_saves() -> None:
            manifest = Manifest.load(_manifest_file(root))
            cli_comp = manifest.get_component("cli")
            assert cli_comp is not None
            cli_comp.status = "completed"
            manifest.save(_manifest_file(root))
            saved_by_a_run.append(_manifest_file(root).read_bytes())

        result = _retry_in_process(root, monkeypatch, choice=0, answered=True, on_ask=a_run_saves)

        assert result.exit_code == 2, result.output
        assert "changed while the confirmation was open" in result.output, result.output
        assert _manifest_file(root).read_bytes() == saved_by_a_run[0], result.output
        assert _state(root)[1:] == before[1:], result.output

    def test_the_retry_runs_under_the_lock_it_took(self, tmp_path: Path) -> None:
        """The in-process `ks factory` uses the lock `ks retry` holds instead of refusing itself."""
        root, _before = _failed_storage(tmp_path)

        retried = _ks(root, "retry", "storage")
        out = retried.stdout + retried.stderr

        # `storage` fails again (its PRD still does not pass), so 1, not 2.
        assert retried.returncode == 1, out
        assert "Starting:" in out, out
        assert "refusing to start a second factory run" not in out, out
        assert _lock_is_free(root)

    def test_the_retry_takes_the_lock_once_and_holds_it_into_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One acquisition, and the engineer the run starts finds the lock held.

        A retry that releases its lock before re-entering `ks factory` and
        lets the run take a fresh one reopens the window between the reset
        and the run: that shows here as two acquisitions. A retry that hands
        on a lock it has already released runs with no lock at all: that
        shows here as a free lock while the engineer runs.
        """
        import kstrl.factory as factory_mod

        root, _before = _failed_storage(tmp_path)
        seen = tmp_path / "lock_seen_by_engineer.txt"
        engineer = tmp_path / "engineer.py"
        engineer.write_text(_LOCK_PROBING_ENGINEER, encoding="utf-8")
        acquired: list[Path] = []
        real_acquire = factory_mod._acquire_run_lock

        def counting_acquire(root_dir: Path, ui: Any, force: bool) -> Any:
            acquired.append(root_dir)
            return real_acquire(root_dir, ui, force)

        monkeypatch.setattr(factory_mod, "_acquire_run_lock", counting_acquire)
        monkeypatch.setattr(cli_mod, "_acquire_run_lock", counting_acquire)
        agent = " ".join(
            shlex.quote(part)
            for part in (
                sys.executable,
                str(engineer),
                str(root / ".kstrl" / "factory.lock"),
                str(seen),
            )
        )

        result = _retry_in_process(root, monkeypatch, choice=0, answered=True, agent_cmd=agent)

        assert result.exit_code == 1, result.output
        assert "Starting:" in result.output, result.output
        assert len(acquired) == 1, (acquired, result.output)
        assert seen.read_text(encoding="utf-8").split() == ["held"], result.output
        assert _lock_is_free(root)


class TestInboxCommandsChangeNothingUnderALiveRun:
    def test_inbox_retry_under_a_held_lock_changes_nothing(
        self, tmp_path: Path, hold_lock: Any
    ) -> None:
        root, before = _failed_storage(tmp_path)
        box = Inbox(root, InboxConfig.load(root))
        item = next(i for i in box.items() if i.component == "storage" and i.is_open)
        hold_lock(root)

        retried = subprocess.run(
            [sys.executable, "-m", "kstrl", "inbox", "retry", item.id, "--root", str(root)]
            + ["--ui", "plain", "--no-color"],
            cwd=root,
            env=_env(),
            capture_output=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        out = retried.stdout + retried.stderr

        assert retried.returncode == 2, out
        assert "storage was not requeued" in out, out
        assert "ks retry storage" in out, out
        assert "Requeued" not in out, out
        assert _manifest_file(root).read_bytes() == before[0], out
        reread = Inbox(root, InboxConfig.load(root)).get(item.id)
        assert reread is not None and reread.is_open, out

    def test_approving_a_parked_merge_under_a_held_lock_records_nothing(
        self, tmp_path: Path, hold_lock: Any
    ) -> None:
        root, env, _reviewed = park._parked_run(tmp_path)
        item = park._park_item(root)
        manifest_before = park._manifest_path(root).read_bytes()
        hold_lock(root)

        approved = park._ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        out = approved.stdout + approved.stderr

        assert approved.returncode == 2, out
        assert f"{item.id[:8]} was not approved" in out, out
        assert f"approved {item.id[:8]}" not in out, out
        assert park._manifest_path(root).read_bytes() == manifest_before, out
        assert park._park_item(root).id == item.id
        assert not any("pr create" in line for line in park._lines(tmp_path / "gh.log")), out


class TestDecomposeWritesNoManifestUnderALiveRun:
    def _decompose(self, tmp_path: Path) -> tuple[Path, str, bytes]:
        root = greenfield(tmp_path, extra={"pyproject.toml": MANIFESTS["pyproject.toml"]})
        reply = tmp_path / "architect.json"
        reply.write_text(VALID_DECOMPOSE_OUTPUT, encoding="utf-8")
        manifest = _manifest_file(root)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"owned by": "the live run"}\n', encoding="utf-8")
        return root, f"cat > /dev/null; cat '{reply}'", manifest.read_bytes()

    @pytest.mark.parametrize("command", ["decompose", "factory"])
    def test_a_held_lock_refuses_before_the_manifest_is_written(
        self, tmp_path: Path, hold_lock: Any, command: str
    ) -> None:
        root, agent, before = self._decompose(tmp_path)
        hold_lock(root)

        proc = run_ks(
            root,
            command,
            "--spec",
            str(root / "spec.md"),
            "--project-name",
            "demo",
            "--root",
            str(root),
            "--agent-cmd",
            agent,
            "--yes" if command == "factory" else "--no-tui",
            "--ui",
            "plain",
            "--no-color",
        )

        assert proc.returncode == 2, proc.stdout
        # `ks serve` classifies a lock refusal by this option's name in the output.
        assert "--force-lock" in proc.stdout, proc.stdout
        assert "Manifest saved" not in proc.stdout, proc.stdout
        assert _manifest_file(root).read_bytes() == before, proc.stdout
        assert not list((root / ".kstrl").rglob("prd.json")), proc.stdout

    def test_force_lock_writes_through_a_held_lock(self, tmp_path: Path, hold_lock: Any) -> None:
        root, agent, before = self._decompose(tmp_path)
        hold_lock(root)

        proc = run_ks(
            root,
            "decompose",
            "--spec",
            str(root / "spec.md"),
            "--project-name",
            "demo",
            "--root",
            str(root),
            "--agent-cmd",
            agent,
            "--force-lock",
            "--ui",
            "plain",
            "--no-color",
        )

        assert proc.returncode == 0, proc.stdout
        assert _manifest_file(root).read_bytes() != before, proc.stdout
