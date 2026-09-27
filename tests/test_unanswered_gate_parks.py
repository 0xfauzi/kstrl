"""A gate that was asked and not answered is not consent (#594).

The defect: with ``pause_before_pr_merge`` on, ``_phase_checkpoint``
returned ``NOT_PROMPTED`` when the channel said it could prompt and the
request then came back unanswered (a TUI that detached mid-prompt), and
mapped any choice outside the three options to ``APPROVED``.
``process_result`` special-cases only ``PARKED``, so both reached
``_phase_pr`` and the component was pushed, opened and merged.
``PlainUI.choose`` made it reachable from a terminal: end of input or
Ctrl-C returned the default option, which is Approve at the merge gate
and Start at ``ks factory``'s confirm.

The fix: an unanswered or out-of-range answer parks through the same
code as the non-interactive gate (one merge_gate item carrying the
parked commit, ``checkpoint_resolved decision=parked decided_by=inbox``),
``PlainUI.choose`` reports an interrupted prompt as an index outside the
options so the channel says ``answered=False``, and ``ks factory``
starts only on an answered Start.

Four layers: the pipeline driven through ``process_result`` with each
way an answer can go missing; a census of every return of
``_phase_checkpoint`` plus an enumeration of every answer shape through
``process_result``; the real ``run_embedded`` with a TUI that dies or
exits while the gate waits; and the real ``ks`` CLI on a pseudo-terminal
that is the child's controlling terminal, answered with end of input.
"""

from __future__ import annotations

import ast
import builtins
import io
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from kstrl.config import KstrlConfig
from kstrl.events import CallbackSink, CheckpointResolved, Event
from kstrl.feature_cmd import run_feature
from kstrl.inbox import Inbox, InboxConfig
from kstrl.interaction import (
    PromptKind,
    PromptRequest,
    PromptResponse,
    QueueInteractionChannel,
    UiInteractionChannel,
)
from kstrl.loop import LoopResult
from kstrl.pipeline import (
    CheckpointDecision,
    ComponentPipeline,
    PipelineOutcome,
    PrDisposition,
    PrPhaseResult,
    Transition,
)
from kstrl.tui import embed
from kstrl.ui.plain import PlainUI
from tests.helpers.prompt_calls import offline_run
from tests.test_feature_cmd import StubAgent, _params
from tests.test_merge_gate_park import (
    CMDS,
    FACTORY_FLAGS,
    HTTP,
    HTTP_BRANCH,
    _engineer_ran,
    _env,
    _git,
    _ks,
    _lines,
    _manifest_path,
    _park_item,
    _repo,
    _status,
)
from tests.test_pipeline import _ChoiceUI, _factory_config, _make_pipeline, _success

PIPELINE_SOURCE = Path(__file__).resolve().parents[1] / "kstrl" / "pipeline.py"


@pytest.fixture(autouse=True)
def _no_real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/test_pipeline.py's stubs: no git diff, no real agent."""
    monkeypatch.setattr("kstrl.git.get_diff_content", lambda *a, **k: "diff --git a b\n")
    monkeypatch.setattr("kstrl.agents.get_agent", lambda *a, **k: object())


class _FakeChannel:
    """A channel that can prompt and answers every request as scripted."""

    def __init__(self, *, answered: bool, choice: int, can_prompt: bool = True) -> None:
        self._answered = answered
        self._choice = choice
        self._can_prompt = can_prompt

    def can_prompt(self) -> bool:
        return self._can_prompt

    def request(self, req: PromptRequest) -> PromptResponse:
        return PromptResponse(
            request_id=req.request_id, choice=self._choice, answered=self._answered
        )


class _TtyPlain(PlainUI):
    """The real PlainUI, reporting stdin as a terminal."""

    def can_prompt(self) -> bool:
        return True


class _Gate:
    """One component driven through the real process_result with the gate on."""

    def __init__(self, root: Path, *, ui: PlainUI | None = None, interaction: Any = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.log = io.StringIO()
        self.pipeline, self.manifest, _, _ = _make_pipeline(
            root,
            config=_factory_config(create_prs=True, pause_before_pr_merge=True, max_retries=3),
            ui=ui or PlainUI(no_color=True, file=self.log),
        )
        if interaction is not None:
            self.pipeline.interaction = interaction
        self.events: list[Event] = []
        self.pipeline.bus.add_sink(CallbackSink(self.events.append))
        self.entered_pr: list[str] = []

        def _record_pr(comp: Any) -> PrPhaseResult:
            self.entered_pr.append(comp.id)
            return PrPhaseResult(disposition=PrDisposition.SKIPPED)

        self.pipeline._phase_pr = _record_pr  # type: ignore[method-assign]

    def run(self) -> PipelineOutcome:
        comp = self.manifest.get_component("comp-a")
        assert comp is not None
        self.pipeline.begin_attempt(comp)
        outcome = self.pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        return outcome

    def resolved(self) -> list[tuple[str, str]]:
        return [
            (e.decision, e.decided_by) for e in self.events if isinstance(e, CheckpointResolved)
        ]

    def merge_gate_items(self) -> list[Any]:
        box = Inbox(self.root, InboxConfig())
        return [item for item in box.open_items() if str(item.kind) == "merge_gate"]


def _assert_parked(gate: _Gate, outcome: PipelineOutcome) -> None:
    assert outcome.checkpoint == CheckpointDecision.PARKED, outcome.checkpoint
    assert outcome.transition == Transition.AWAITING_APPROVAL, outcome.transition
    assert gate.entered_pr == [], "_phase_pr was entered with the gate unanswered"
    items = gate.merge_gate_items()
    assert len(items) == 1, [(str(i.kind), i.title) for i in items]
    assert items[0].component == "comp-a"
    assert "head_sha" in items[0].evidence, items[0].evidence
    assert gate.resolved() == [("parked", "inbox")], gate.resolved()


class TestAnUnansweredGateParks:
    def test_an_unanswered_request_parks_and_files_one_merge_gate_item(
        self, tmp_path: Path
    ) -> None:
        gate = _Gate(tmp_path, interaction=_FakeChannel(answered=False, choice=0))
        _assert_parked(gate, gate.run())
        assert "got no answer" in gate.log.getvalue(), gate.log.getvalue()

    def test_a_choice_outside_the_options_parks_and_is_logged(self, tmp_path: Path) -> None:
        gate = _Gate(tmp_path, interaction=_FakeChannel(answered=True, choice=7))
        _assert_parked(gate, gate.run())
        assert "choice=7" in gate.log.getvalue(), gate.log.getvalue()

    def test_a_queue_channel_detached_while_the_gate_waits_parks(self, tmp_path: Path) -> None:
        channel = QueueInteractionChannel()
        # The resolver goes away while the request is pending, which is
        # what embed.py's detach does to a waiting prompt.
        channel.attach(lambda req: channel.detach())
        gate = _Gate(tmp_path, interaction=channel)
        _assert_parked(gate, gate.run())

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    def test_an_interrupted_plain_prompt_parks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
    ) -> None:
        def _raise(prompt: str = "") -> str:
            raise interrupt

        monkeypatch.setattr(builtins, "input", _raise)
        gate = _Gate(tmp_path, ui=_TtyPlain(no_color=True, file=io.StringIO()))
        _assert_parked(gate, gate.run())

    def test_an_answered_approve_still_merges(self, tmp_path: Path) -> None:
        gate = _Gate(tmp_path, ui=_ChoiceUI(0))
        outcome = gate.run()
        assert outcome.checkpoint == CheckpointDecision.APPROVED
        assert gate.entered_pr == ["comp-a"]
        assert gate.resolved() == [("approved", "operator")]
        assert gate.merge_gate_items() == []


class TestAnInterruptedPromptIsNotAnAnswer:
    """The contract lane #597's `ks retry` confirm relies on."""

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    def test_the_channel_reports_an_interrupted_plain_prompt_unanswered(
        self, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
    ) -> None:
        def _raise(prompt: str = "") -> str:
            raise interrupt

        monkeypatch.setattr(builtins, "input", _raise)
        response = UiInteractionChannel(_TtyPlain(no_color=True, file=io.StringIO())).request(
            PromptRequest(
                kind=PromptKind.CONFIRM,
                header="Proceed with factory execution?",
                options=("Start", "Quit"),
                default=0,
            )
        )
        assert response.answered is False, response


def _checkpoint_function() -> ast.FunctionDef:
    tree = ast.parse(PIPELINE_SOURCE.read_text(encoding="utf-8"))
    found = [
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == ComponentPipeline.__name__
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_phase_checkpoint"
    ]
    assert len(found) == 1, f"ComponentPipeline._phase_checkpoint found {len(found)} times"
    return found[0]


def _gate_off_bodies(func: ast.FunctionDef) -> list[ast.If]:
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "not self.factory_config.pause_before_pr_merge"
    ]


def _located(func: ast.FunctionDef, kind: type[ast.AST]) -> list[tuple[str, ast.AST]]:
    """Every node of ``kind`` in the function, tagged gate_off or gate_on."""
    gate_off = _gate_off_bodies(func)
    assert len(gate_off) == 1, "the `if not self.factory_config.pause_before_pr_merge` guard moved"
    off_nodes = {id(n) for stmt in gate_off[0].body for n in ast.walk(stmt)}
    return [
        ("gate_off" if id(node) in off_nodes else "gate_on", node)
        for node in ast.walk(func)
        if isinstance(node, kind)
    ]


class TestTheGateCensus:
    #: Every return of `_phase_checkpoint`, by where it sits and what it returns.
    EXPECTED_RETURNS = [
        ("gate_off", "CheckpointDecision.NOT_PROMPTED"),
        ("gate_on", "CheckpointDecision.PARKED"),
        ("gate_on", "decision"),
    ]
    #: Every `CheckpointDecision.<member>` the function names, by where it sits.
    #: A second APPROVED is a `.get` default; a NOT_PROMPTED past the guard
    #: is the #594 defect itself.
    EXPECTED_MEMBERS = {
        ("gate_off", "NOT_PROMPTED"): 1,
        ("gate_on", "APPROVED"): 1,
        ("gate_on", "REJECTED"): 2,
        ("gate_on", "RETRY"): 2,
        ("gate_on", "PARKED"): 1,
    }

    def test_every_return_of_the_checkpoint_is_accounted_for(self) -> None:
        func = _checkpoint_function()
        nested = [
            n
            for n in ast.walk(func)
            if n is not func and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        assert nested == [], "a nested function hides its returns from this census"
        returns = sorted(
            (where, ast.unparse(node.value) if node.value is not None else "None")
            for where, node in _located(func, ast.Return)
            if isinstance(node, ast.Return)
        )
        assert returns == sorted(self.EXPECTED_RETURNS), returns

    def test_every_decision_the_checkpoint_names_is_accounted_for(self) -> None:
        func = _checkpoint_function()
        members = Counter(
            (where, node.attr)
            for where, node in _located(func, ast.Attribute)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == CheckpointDecision.__name__
        )
        assert dict(members) == self.EXPECTED_MEMBERS, dict(members)

    def test_only_an_answered_approve_reaches_the_pr_phase(self, tmp_path: Path) -> None:
        reached: dict[tuple[bool, bool, int], tuple[str, bool]] = {}
        for can_prompt in (False, True):
            for answered in (False, True):
                for choice in (-1, 0, 1, 2, 3, 7):
                    case = (can_prompt, answered, choice)
                    channel = _FakeChannel(answered=answered, choice=choice, can_prompt=can_prompt)
                    gate = _Gate(tmp_path / "-".join(map(str, case)), interaction=channel)
                    outcome = gate.run()
                    reached[case] = (outcome.checkpoint.value, bool(gate.entered_pr))
        merged = sorted(case for case, (_, entered) in reached.items() if entered)
        assert merged == [(True, True, 0)], merged
        assert not any(value == "not_prompted" for value, _ in reached.values()), reached

    def test_not_prompted_means_the_gate_is_off(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path, config=_factory_config(create_prs=True, pause_before_pr_merge=False)
        )
        entered: list[str] = []
        pipeline._phase_pr = lambda comp: (  # type: ignore[method-assign]
            entered.append(comp.id) or PrPhaseResult(disposition=PrDisposition.SKIPPED)
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.checkpoint == CheckpointDecision.NOT_PROMPTED
        assert entered == ["comp-a"]


#: Every place kstrl builds a prompt, and what happens when nobody answers
#: it. A new prompt fails this census until its author adds a row here and
#: says what its unanswered path does: the class #594 fixed is a prompt whose
#: unanswered path nobody decided.
EXPECTED_PROMPT_SITES = {
    "cli.py::factory": "exits without starting the run (#594)",
    "cli.py::retry": "exits without starting the retry (lane #597)",
    "feature_cmd.py::run_feature": "refuses: quit to amend",
    "guards.py::enforce_allowed_paths": "quits",
    "loop.py::run_loop": "continues: an iteration pause is not a gate",
    "pipeline.py::ComponentPipeline._phase_checkpoint": "parks for the inbox (#594)",
    "tui/screens/inbox.py::InboxScreen.action_reject": "a dismissed modal decides nothing",
    "tui/screens/retry.py::RetryScreen.on_scope_read": "a dismissed modal starts nothing",
}

KSTRL_PACKAGE = PIPELINE_SOURCE.parent


def _refuse_an_aliased_import(tree: ast.Module, rel: str) -> None:
    """The census keys on the name `PromptRequest`; an alias would hide a site."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliased = [a.asname for a in node.names if a.name == "PromptRequest" and a.asname]
            assert aliased == [], (
                f"{rel}: PromptRequest imported as {aliased}; the census keys on the name"
            )


def _called_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _count_prompt_calls(
    node: ast.AST, scope: tuple[str, ...], rel: str, found: Counter[str]
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _count_prompt_calls(child, (*scope, child.name), rel, found)
            continue
        if isinstance(child, ast.Call) and _called_name(child) == "PromptRequest":
            found[f"{rel}::{'.'.join(scope) or '<module>'}"] += 1
        _count_prompt_calls(child, scope, rel, found)


def _prompt_sites() -> Counter[str]:
    """Every `PromptRequest(...)` call in kstrl/, by file and enclosing scope.

    Not seen: a call through a name rebound from it (`P = PromptRequest`)
    and a request copied with `dataclasses.replace`. An aliased import is
    refused rather than missed.
    """
    found: Counter[str] = Counter()
    for path in sorted(KSTRL_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(KSTRL_PACKAGE).as_posix()
        _refuse_an_aliased_import(tree, rel)
        _count_prompt_calls(tree, (), rel, found)
    return found


class TestEveryPromptStatesItsUnansweredPath:
    def test_every_prompt_site_is_enrolled(self) -> None:
        sites = _prompt_sites()
        assert dict(sites) == dict.fromkeys(EXPECTED_PROMPT_SITES, 1), dict(sites)


class _StandInApp:
    """Replaces KstrlTuiApp in run_embedded: attaches the channel as the
    real app's on_mount does, waits until the gate is asking, then dies
    (``crash``) or exits with the forced-quit code 130."""

    crash = True

    def __init__(self, **kwargs: Any) -> None:
        self.channel: QueueInteractionChannel = kwargs["channel"]

    def run(self) -> int:
        asking = threading.Event()
        self.channel.attach(lambda req: asking.set())
        assert asking.wait(timeout=30), "the gate never asked"
        if self.crash:
            raise RuntimeError("the TUI died")
        return 130


class _ExitingApp(_StandInApp):
    crash = False


class TestAnEmbeddedRunParksWhenTheTuiGoesAway:
    @pytest.mark.parametrize("app", [_StandInApp, _ExitingApp], ids=["tui-crash", "tui-exit"])
    def test_the_gate_waiting_when_the_tui_goes_parks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app: type[_StandInApp]
    ) -> None:
        monkeypatch.setattr(embed, "KstrlTuiApp", app)
        gates: list[_Gate] = []
        outcomes: list[PipelineOutcome] = []

        def _target(ctx: embed.EmbeddedContext) -> int:
            # The real app attaches in on_mount; a gate reached before that
            # parks as non-interactive and never asks. Wait for the attach
            # so this drives the detach-while-asking path, not that one.
            deadline = time.monotonic() + 30
            while not ctx.channel.can_prompt() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ctx.channel.can_prompt(), "the stand-in app never attached"
            gate = _Gate(tmp_path / "project", interaction=ctx.channel)
            gates.append(gate)
            outcomes.append(gate.run())
            return 0

        code = embed.run_embedded(
            _target, root_dir=tmp_path, run_id="factory-20260927-embed594", poll_interval=0.05
        )
        assert code == (0 if app.crash else 130)
        assert len(outcomes) == 1
        _assert_parked(gates[0], outcomes[0])


# --- the real CLI on a pseudo-terminal ---------------------------------------

#: The child takes the pty as its controlling terminal, as a shell in a real
#: terminal would have it, then execs `python -m kstrl`. Measured without it:
#: the pty is nobody's controlling terminal, the first verification command
#: (a session of its own) acquires it, and the terminal is revoked when that
#: command exits, so every later prompt sees a stdin that is not a tty.
CTTY_EXEC = (
    "import fcntl, os, sys, termios; "
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
    "os.execv(sys.executable, [sys.executable, '-m', 'kstrl', *sys.argv[1:]])"
)

needs_pty = pytest.mark.skipif(
    not hasattr(os, "openpty") or sys.platform == "win32",
    reason="needs a pseudo-terminal and TIOCSCTTY",
)


def _ks_on_a_terminal(root: Path, env: dict[str, str], *args: str) -> tuple[int, int, str]:
    """Run `ks` on a pty and answer every prompt with end of input (Ctrl-D).

    Returns the exit code, the number of prompts answered and the transcript.
    End of input rather than Ctrl-C: the EOF byte waits in the line
    discipline until the read, while a SIGINT sent between the prompt's
    write and its read is lost until the process wakes, which is a hang.
    """
    master, slave = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", CTTY_EXEC, *args, "--root", str(root)],
        cwd=root,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    out = b""
    answered = 0
    deadline = time.monotonic() + 240
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:  # EIO on Linux once the child has exited
                    break
                if not data:
                    break
                out += data
            if out.count(b"Select option") > answered:
                answered += 1
                os.write(master, b"\x04")
            if not ready and proc.poll() is not None:
                break
        code = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
        os.close(master)
    return code, answered, out.decode("utf-8", "replace")


@needs_pty
class TestTheRealCliOnATerminal:
    def test_end_of_input_at_the_merge_gate_parks_and_approve_merges_the_parked_commit(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        env = _env(tmp_path)
        code, answered, out = _ks_on_a_terminal(
            root,
            env,
            "factory",
            "--manifest",
            str(_manifest_path(root)),
            *FACTORY_FLAGS,
            "--pause-before-pr-merge",
        )
        gh = _lines(tmp_path / "gh.log")
        assert not any("pr create" in line or "pr merge" in line for line in gh), gh
        assert _status(root, HTTP) == "awaiting_approval", out
        assert _status(root, CMDS) == "pending", out
        assert (code, answered) == (1, 1), out
        reviewed = _git(root, "rev-parse", f"refs/heads/{HTTP_BRANCH}")
        item = _park_item(root)
        assert item.evidence.get("head_sha") == reviewed, item.evidence

        approved = _ks(root, env, "inbox", "approve", item.id, "--ui", "plain", "--no-color")
        assert _lines(tmp_path / "gh.pushed") == [f"{reviewed}\trefs/heads/{HTTP_BRANCH}"], (
            approved.stdout + approved.stderr
        )
        assert _status(root, HTTP) == "completed", approved.stdout + approved.stderr

    def test_end_of_input_at_the_factory_confirm_starts_nothing(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        env = _env(tmp_path)
        code, answered, out = _ks_on_a_terminal(
            root,
            env,
            "factory",
            "--manifest",
            str(_manifest_path(root)),
            *[flag for flag in FACTORY_FLAGS if flag != "--yes"],
        )
        assert "Proceed with factory execution?" in out, out
        assert _engineer_ran(tmp_path) == [], out
        assert not (root / ".kstrl" / "runs").exists(), out
        assert (code, answered) == (0, 1), out
        assert "Factory not started." in out, out


class TestAnInterruptedFeatureGateStartsNothing:
    """`ks feature`'s review gate, the third paid-run prompt in the class:
    on the terminal channel an interrupted prompt read as Start."""

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    def test_an_interrupted_feature_gate_quits_to_amend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
    ) -> None:
        def _raise(prompt: str = "") -> str:
            raise interrupt

        loops: list[int] = []

        def _loop(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
            loops.append(len(loops))
            return LoopResult(completed=True, iterations=1, exit_code=0)

        monkeypatch.setattr(builtins, "input", _raise)
        monkeypatch.setattr("kstrl.feature_cmd.run_loop", _loop)
        stream = io.StringIO()
        code = run_feature(
            _params(tmp_path),
            KstrlConfig(),
            StubAgent(),
            _TtyPlain(no_color=True, file=stream),
            tmp_path,
            run=offline_run(tmp_path, "feature"),
        )
        assert "Start implementation" in stream.getvalue(), stream.getvalue()
        assert code == 0, stream.getvalue()
        # One loop is the understand phase; a second is the implementation
        # the interrupted gate must not start.
        assert len(loops) == 1, f"{len(loops)} engineer loops ran"
        assert "Amend the understand file" in stream.getvalue(), stream.getvalue()
