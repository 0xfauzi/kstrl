"""A command that changes run state does it under the run lock, after its confirmation (#597).

The defect: `ks retry` reset the manifest, deleted the failed branch and
removed the kept evidence worktree before it asked Start/Quit and before
anything took ``.kstrl/factory.lock``, so Quit, or a refusal because a live
run held the lock, left all of that done. `ks inbox retry` saved the
requeue and closed the inbox item without looking at the lock, and a live
run's next whole-document manifest save undid the requeue. `ks inbox
approve` on a parked merge recorded the decision before the run it starts
was refused on the lock (#596 item 2). `ks decompose` and `ks factory
--spec` wrote a new manifest under a live run. Plain `ks factory` also
loaded its manifest and asked its confirmation before taking any lock
(the altitude reviewer's own probe), so a concurrent `ks inbox retry`
found the lock free while the question was open.

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
from kstrl.ui.plain import PlainUI
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


class TestRunLockRelease:
    def test_release_clears_held_as_well_as_fp(self, tmp_path: Path) -> None:
        """A released lock is not held (#597).

        ``release()`` used to clear ``fp`` and leave ``held`` True, so a
        released lock handed on still read as held by whatever checked
        ``.held`` (``run_factory``'s stale-state pruning, and #597's own
        ``ks factory`` refusal below). Idempotent: releasing twice is not
        an error and the second call changes nothing further.
        """
        import kstrl.factory as factory_mod

        lock = factory_mod._acquire_run_lock(tmp_path, PlainUI(no_color=True), force=False)
        assert lock.held is True
        assert lock.fp is not None

        lock.release()

        assert lock.held is False
        assert lock.fp is None

        lock.release()

        assert lock.held is False
        assert lock.fp is None

    def test_a_released_lock_handed_to_ks_factory_is_refused(self, tmp_path: Path) -> None:
        """`ks factory` refuses a handed lock already released, rather than using it (#597).

        Only a bug in the caller (`ks retry`, `ks inbox approve`) could
        hand on a lock its own ``release()`` already ran on; this is the
        assertion against that silently being trusted, not a case the
        CLI is meant to reach in ordinary use.
        """
        import kstrl.factory as factory_mod
        from kstrl.launch_record import option_argv

        root, _before = _failed_storage(tmp_path)
        released = factory_mod._acquire_run_lock(root, PlainUI(no_color=True), force=False)
        released.release()

        argv = option_argv(
            cli_mod.factory,
            {
                "manifest_path": str(_manifest_file(root)),
                "root": str(root),
                "yes": True,
                "tui": False,
                "ui": "plain",
                "no_color": True,
            },
        )
        ctx = cli_mod.factory.make_context("factory", argv)
        ctx.meta[cli_mod._HANDED_RUN_LOCK] = released

        with pytest.raises(RuntimeError, match="a released run lock was handed"):
            with ctx:
                cli_mod.factory.invoke(ctx)


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

    def test_a_save_the_plan_does_not_show_is_kept_by_the_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reset is applied to the manifest re-read under the lock, not the one read before.

        A run can save while the question is open without changing the plan
        (a field it stamps, a component it completes). A retry that checks
        the re-read copy but resets and saves the copy it loaded before the
        question writes that save away.
        """
        root, _before = _failed_storage(tmp_path)

        def a_run_stamps_a_field() -> None:
            manifest = Manifest.load(_manifest_file(root))
            cli_comp = manifest.get_component("cli")
            assert cli_comp is not None
            cli_comp.linear_issue_identifier = "EXC-597"
            manifest.save(_manifest_file(root))

        result = _retry_in_process(
            root, monkeypatch, choice=0, answered=True, on_ask=a_run_stamps_a_field
        )

        assert result.exit_code == 1, result.output
        assert "Starting:" in result.output, result.output
        cli_comp = Manifest.load(_manifest_file(root)).get_component("cli")
        assert cli_comp is not None
        assert cli_comp.linear_issue_identifier == "EXC-597", result.output

    def test_a_held_lock_with_force_lock_does_not_crash(
        self, tmp_path: Path, hold_lock: Any
    ) -> None:
        """`ks retry --force-lock` under a held lock runs, rather than crashing on a handed lock.

        `_acquire_run_lock(force=True)` on a held lock returns
        ``_RunLock(fp=None, held=False)``, the same shape the no-fcntl
        degrade returns: both are legitimate to hand to `ks factory`.
        Checking ``fp is not None`` in ``_resolve_factory_run_lock``
        could not tell either of those apart from a lock whose own
        ``release()`` had already run, so a forced retry against a held
        lock raised ``AssertionError: a released run lock was handed to
        `ks factory`: _RunLock(fp=None, held=False)`` after it had
        already reset the manifest and deleted the failed branch.
        """
        root, _before = _failed_storage(tmp_path)
        hold_lock(root)

        retried = _ks(root, "retry", "storage", "--force-lock")
        out = retried.stdout + retried.stderr

        # `storage` fails again (its PRD still does not pass), so 1, not 2.
        assert retried.returncode == 1, out
        assert "Starting:" in out, out
        assert "Traceback" not in out, out


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


#: The approve path's own lock-probing engineer (mirrors
#: ``_LOCK_PROBING_ENGINEER`` above): probes the run lock, then commits
#: one file so mechanical verification sees a normal build for the
#: dependent it runs as.
_PARK_LOCK_PROBING_ENGINEER = """
import fcntl, pathlib, subprocess, sys
lock_path, out_path = sys.argv[1], sys.argv[2]
sys.stdin.read()
with open(lock_path, "a+", encoding="utf-8") as fp:
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        verdict = "free"
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except OSError:
        verdict = "held"
with open(out_path, "a", encoding="utf-8") as out:
    out.write(verdict + "\\n")
pathlib.Path("work.txt").write_text("work\\n")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "work"], check=True)
print("<promise>COMPLETE</promise>")
"""


class TestApprovingAParkedMergeHoldsTheLockIntoTheDependentRun:
    def test_approve_takes_the_lock_once_and_holds_it_into_the_dependent_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The approve path's equivalent of the retry test above with the same name shape.

        Nothing pinned that ``_decide_parked_merge_if_parked`` holds the
        lock it hands to the run for the WHOLE run, not just up to
        ``factory.invoke``: releasing it there and letting the run take a
        fresh one is the same probe-then-acquire window the plan rejects
        for `ks retry`, and a lock-held PROBE alone cannot tell that
        window from the real thing, because a freshly re-acquired lock
        also reads "held" while the dependent's engineer runs. Counting
        acquisitions (as the retry test does) is what tells them apart.
        `http`'s approval merges without re-running its engineer; `cmds`,
        the dependent, is what actually runs one, so its engineer is the
        probe.
        """
        import kstrl.factory as factory_mod

        root, env, _reviewed = park._parked_run(tmp_path)
        item = park._park_item(root)
        seen = tmp_path / "lock_seen_by_engineer.txt"
        engineer_script = tmp_path / "park_engineer.py"
        engineer_script.write_text(_PARK_LOCK_PROBING_ENGINEER, encoding="utf-8")
        agent_cmd = " ".join(
            shlex.quote(part)
            for part in (
                sys.executable,
                str(engineer_script),
                str(root / ".kstrl" / "factory.lock"),
                str(seen),
            )
        )
        acquired: list[Path] = []
        real_acquire = factory_mod._acquire_run_lock

        def counting_acquire(root_dir: Path, ui: Any, force: bool) -> Any:
            acquired.append(root_dir)
            return real_acquire(root_dir, ui, force)

        monkeypatch.setattr(factory_mod, "_acquire_run_lock", counting_acquire)
        monkeypatch.setattr(cli_mod, "_acquire_run_lock", counting_acquire)
        for name in [k for k in os.environ if k.startswith("KSTRL_")]:
            monkeypatch.delenv(name)
        probe_env = dict(env)
        probe_env["AGENT_CMD"] = agent_cmd
        for name, value in probe_env.items():
            monkeypatch.setenv(name, value)

        result = CliRunner().invoke(
            cli_mod.cli,
            ["inbox", "approve", item.id, "--root", str(root), "--ui", "plain", "--no-color"],
        )

        assert result.exit_code != 2, result.output
        assert len(acquired) == 1, (acquired, result.output)
        assert seen.read_text(encoding="utf-8").split() == ["held"], result.output
        assert _lock_is_free(root)


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


class TestFactoryTakesTheLockBeforeItLoadsTheManifest:
    def test_an_inbox_retry_while_the_factory_confirmation_is_open_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ks factory --manifest` takes the lock before it loads the manifest (#597).

        Altitude probe (simplify/altitude/test_factory_stale.py): before
        this fix, plain `ks factory` loaded the manifest and asked its
        confirmation before taking any lock, so an `ks inbox retry`
        issued while that confirmation was open found the lock free,
        requeued `storage` and closed the inbox item - and the factory
        then ran from the stale in-memory manifest it had already
        loaded, put `storage` back to FAILED and undid the requeue with
        no trace, the exact outcome `inbox_retry`'s own docstring says
        the lock prevents. Taking the lock above the manifest load means
        the concurrent `ks inbox retry` is refused instead.
        """
        root, _before = _failed_storage(tmp_path)
        box = Inbox(root, InboxConfig.load(root))
        item = next(i for i in box.items() if i.component == "storage" and i.is_open)
        seen: dict[str, Any] = {}

        def inbox_retry_while_the_question_is_open() -> None:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kstrl",
                    "inbox",
                    "retry",
                    item.id,
                    "--root",
                    str(root),
                    "--ui",
                    "plain",
                    "--no-color",
                ],
                cwd=root,
                env=_env(),
                capture_output=True,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                timeout=120,
            )
            seen["rc"] = proc.returncode
            seen["out"] = proc.stdout + proc.stderr

        _Channel.asked = []
        _Channel.choice, _Channel.answered, _Channel.on_ask = (
            0,
            True,
            inbox_retry_while_the_question_is_open,
        )
        monkeypatch.setattr(cli_mod, "UiInteractionChannel", _Channel)
        for name in [k for k in os.environ if k.startswith("KSTRL_")]:
            monkeypatch.delenv(name)
        for name, value in _env().items():
            monkeypatch.setenv(name, value)

        result = CliRunner().invoke(
            cli_mod.cli,
            [
                "factory",
                "--manifest",
                str(_manifest_file(root)),
                "--root",
                str(root),
                "--no-tui",
                "--ui",
                "plain",
                "--no-color",
            ],
        )

        assert len(_Channel.asked) == 1, result.output
        # `storage`'s PRD still does not pass, so the factory fails it again (exit 1),
        # never the lock refusal (exit 2) - the confirmation's own run is unaffected.
        assert result.exit_code == 1, result.output
        assert seen["rc"] == 2, seen["out"]
        assert "storage was not requeued" in seen["out"], seen["out"]
        assert "Requeued" not in seen["out"], seen["out"]
        reread = Inbox(root, InboxConfig.load(root)).get(item.id)
        assert reread is not None and reread.is_open, seen["out"]
