"""TUI surface C4: decompose as an event-stream run."""

from __future__ import annotations

import http.server
import io
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.agents.base import ARCHITECT_COMPONENT, ARCHITECT_ROLE, UsageRecord
from kstrl.commandrun import open_command_run
from kstrl.decompose import SpecBlockerError, decompose_spec
from kstrl.reducer import load_run_state
from kstrl.ui.plain import PlainUI
from tests.helpers.before_spend import no_base_check
from tests.helpers.prompt_calls import architect_call
from tests.helpers.rootperms import skip_as_root
from tests.helpers.stack_confirmation import PROPOSED_STACK
from tests.test_decompose import VALID_DECOMPOSE_OUTPUT, MockDecomposeAgent

MINOR_ISSUE_OUTPUT = json.dumps(
    {
        **json.loads(VALID_DECOMPOSE_OUTPUT),
        "spec_issues": [
            {
                "id": "edge-case",
                "severity": "minor",
                "kind": "missing_detail",
                "summary": "Edge case unspecified",
                "location": "spec.md:9",
                "suggestion": "Name the edge case",
            }
        ],
        "decisions": [
            {
                "issue": "edge-case",
                "question": "what does the empty-input path do",
                "disposition": "assumed",
                "resolution": "return an empty list; pinned by AC2",
            }
        ],
    }
)

BLOCKER_OUTPUT = json.dumps(
    {
        "stack": PROPOSED_STACK,
        "spec_issues": [
            {
                "id": "spec-empty",
                "severity": "blocker",
                "kind": "ambiguity",
                "summary": "Spec is empty",
                "location": "everywhere",
                "suggestion": "Write actual requirements",
            }
        ],
        "decisions": [
            {
                "issue": "spec-empty",
                "question": "what is this product for",
                "disposition": "escalated",
                "resolution": "the owner must say",
            }
        ],
        "components": [],
    }
)


class TwoShotAgent:
    """Garbage on the first attempt, valid JSON on the second."""

    def __init__(self, good_output: str) -> None:
        self._good = good_output
        self._calls = 0
        self.final_message: str | None = None

    @property
    def name(self) -> str:
        return "two-shot"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self._calls += 1
        if self._calls == 1:
            yield "this is not json at all"
            self.final_message = "this is not json at all"
            return
        yield from self._good.splitlines()
        self.final_message = self._good.splitlines()[-1]


class ExplodingAgent:
    name = "exploding"
    final_message: str | None = None

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        raise RuntimeError("architect exploded")
        yield  # pragma: no cover - makes this an iterator


def _spec_root(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("# Spec\nBuild it.")
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    return spec_file


def _decompose(
    tmp_path: Path,
    agent: object,
    console: io.StringIO | None = None,
    single_pr: bool = False,
) -> object:
    spec_file = _spec_root(tmp_path)
    ui = PlainUI(no_color=True, file=console if console is not None else io.StringIO())
    run = open_command_run(
        ui,
        tmp_path,
        "decompose",
        component=ARCHITECT_COMPONENT,
        enabled=True,
        heartbeat=False,
    )
    try:
        return decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=single_pr,
            agent=agent,  # type: ignore[arg-type]
            ui=ui,
            root_dir=tmp_path,
            bus=run.bus,
            transcript=run.transcript_writer(ARCHITECT_COMPONENT),
            prompt_call=run.agent_call(ARCHITECT_COMPONENT, ARCHITECT_ROLE),
            timeout=None,
            before_spend=no_base_check,
        )
    finally:
        run.close()


class TestDecomposeRun:
    def test_success_run_folds_with_plan_and_artifacts(
        self,
        tmp_path: Path,
    ) -> None:
        manifest = _decompose(
            tmp_path,
            MockDecomposeAgent(MINOR_ISSUE_OUTPUT),
        )

        state, _ = load_run_state(tmp_path)
        assert state.kind == "decompose"
        assert state.finished
        # The forming DAG: architect first, then the manifest order.
        assert state.plan_order == [
            ARCHITECT_COMPONENT,
            *[c.id for c in manifest.components],  # type: ignore[attr-defined]
        ]
        architect = state.components[ARCHITECT_COMPONENT]
        assert architect.status == "completed"
        assert [p["phase"] for p in architect.phase_history] == [
            "decompose",
            "audit",
        ]
        assert all(p["passed"] for p in architect.phase_history)
        assert [a["label"] for a in state.artifacts] == [
            "spec_issues",
            "decisions",
            "prd",
            "prd",
            "manifest",
        ]
        # Planned components carry their deps for the DAG view.
        assert state.components["api"].deps == ("database",)

    def test_folded_issues_match_the_disk_artifact(
        self,
        tmp_path: Path,
    ) -> None:
        _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT))
        state, _ = load_run_state(tmp_path)
        payload = json.loads(
            (tmp_path / "scripts" / "kstrl" / "spec-issues.json").read_text(),
        )
        disk_issues = [
            {
                "severity": i["severity"],
                "kind": i["kind"],
                "summary": i["summary"],
                "location": i.get("location", ""),
                "suggestion": i.get("suggestion", ""),
            }
            for i in payload["issues"]
        ]
        assert state.spec_issues == disk_issues
        assert state.spec_issue_counts == {"minor": 1}

    def test_retry_folds_a_failed_then_passed_attempt(
        self,
        tmp_path: Path,
    ) -> None:
        _decompose(tmp_path, TwoShotAgent(VALID_DECOMPOSE_OUTPUT))
        state, _ = load_run_state(tmp_path)
        architect = state.components[ARCHITECT_COMPONENT]
        decompose_phases = [p for p in architect.phase_history if p["phase"] == "decompose"]
        assert [p["passed"] for p in decompose_phases] == [False, True]
        assert architect.attempt == 2
        # ComponentCompleted gives the number of architect attempts (#776 B3D1).
        assert architect.iteration == 2
        assert architect.status == "completed"

    def test_blocker_halt_finishes_the_run_before_raising(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(SpecBlockerError):
            _decompose(tmp_path, MockDecomposeAgent(BLOCKER_OUTPUT))

        state, _ = load_run_state(tmp_path)
        assert state.finished  # not dead - the architect judged and halted
        architect = state.components[ARCHITECT_COMPONENT]
        assert architect.status == "failed"
        audit = [p for p in architect.phase_history if p["phase"] == "audit"]
        assert audit and audit[0]["passed"] is False
        assert state.spec_issue_counts == {"blocker": 1}
        assert [a["label"] for a in state.artifacts] == ["spec_issues", "decisions"]
        # No plan beyond the architect: nothing was decomposed.
        assert state.plan_order == [ARCHITECT_COMPONENT]

    def test_transcript_captures_the_architect_stream(
        self,
        tmp_path: Path,
    ) -> None:
        _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT))
        runs_root = tmp_path / ".kstrl" / "runs"
        run_dir = next(iter(runs_root.iterdir()))
        transcript = run_dir / "components" / ARCHITECT_COMPONENT / "engineer.log"
        assert "spec_issues" in transcript.read_text()

    def test_agent_exception_finishes_the_run(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="architect exploded"):
            _decompose(tmp_path, ExplodingAgent())
        state, _ = load_run_state(tmp_path)
        assert state.finished
        architect = state.components[ARCHITECT_COMPONENT]
        assert architect.status == "failed"
        assert architect.error == "RuntimeError: architect exploded"
        assert architect.phase_history[-1]["phase"] == "decompose"
        assert architect.phase_history[-1]["passed"] is False

    def test_rolled_back_prds_are_not_published_as_artifacts(
        self,
        tmp_path: Path,
    ) -> None:
        with (
            patch(
                "kstrl.decompose.Manifest.save",
                side_effect=OSError("manifest disk full"),
            ),
            pytest.raises(OSError, match="manifest disk full"),
        ):
            _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT))

        state, _ = load_run_state(tmp_path)
        assert state.finished
        architect = state.components[ARCHITECT_COMPONENT]
        assert architect.status == "failed"
        assert architect.error == "OSError: manifest disk full"
        # #260 round 2: the register is written AFTER the manifest
        # commits, so a manifest that never saved leaves no register
        # either. Round 1 wrote it first, and a run that died here left
        # a fresh register beside the PREVIOUS manifest for the factory
        # to read as that manifest's decisions.
        assert [a["label"] for a in state.artifacts] == ["spec_issues"]
        assert not (tmp_path / "scripts" / "kstrl" / "decisions.json").exists()
        assert list((tmp_path / "scripts" / "kstrl").rglob("prd.json")) == []

    def test_without_bus_no_run_dir_and_same_result(
        self,
        tmp_path: Path,
    ) -> None:
        """bus=None is exactly the pre-C4 behavior."""
        spec_file = _spec_root(tmp_path)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=MockDecomposeAgent(MINOR_ISSUE_OUTPUT),  # type: ignore[arg-type]
            ui=PlainUI(no_color=True, file=io.StringIO()),
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )
        assert len(manifest.components) == 2
        assert not (tmp_path / ".kstrl" / "runs").exists()


class MeteringAgent:
    """A decompose agent that reports usage the way a real adapter does.

    One double for every case #257 cares about, because the alternative
    was three that each re-implemented an existing one in this file:

    - ``cost=None`` is the codex shape, a token total and no price, which
      is what the coverage footer exists for.
    - several ``outputs`` drive the retry path, one record per attempt.
    - ``crash=True`` is the agent that dies AFTER the call was billed.

    The record is appended when the stream ends, where a real adapter
    appends it (``kstrl/agents/claude_code.py``), except on the crash
    path, where the money is spent before the failure.
    """

    def __init__(
        self,
        *outputs: str,
        cost: float | None = 0.5,
        crash: bool = False,
    ) -> None:
        self._outputs = list(outputs)
        self._cost = cost
        self._crash = crash
        self.final_message: str | None = None
        self.usage_records: list[UsageRecord] = []

    @property
    def name(self) -> str:
        return "metering"

    def _bill(self, total_tokens: int = 120) -> None:
        self.usage_records.append(
            UsageRecord(
                input_tokens=100,
                output_tokens=20,
                total_tokens=total_tokens,
                cost_usd=self._cost,
                duration_seconds=1.5,
                source="claude-stream-json",
            )
        )

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        # Attempt N reads output N, and the last one repeats if the
        # architect retries more times than the test scripted.
        output = self._outputs[min(len(self.usage_records), len(self._outputs) - 1)]
        if self._crash:
            self._bill()
            raise RuntimeError("architect exploded")
        yield from output.splitlines()
        if output.strip():
            self.final_message = output.splitlines()[-1]
        self._bill()


class TestArchitectUsageIsRecorded:
    """#257: the architect was the one paid role with no meter, so five
    real ``ks decompose`` runs each reported $0.00."""

    def test_the_blocker_halt_still_records_the_spend(
        self,
        tmp_path: Path,
    ) -> None:
        """The halt is the COMMON outcome on a first spec, so an
        emit-after-success would have missed exactly the case the issue is
        about."""
        with pytest.raises(SpecBlockerError):
            _decompose(tmp_path, MeteringAgent(BLOCKER_OUTPUT))

        state, _ = load_run_state(tmp_path)
        assert state.usage_calls == 1
        assert state.cost_calls == 1
        assert state.cost_usd == pytest.approx(0.5)
        assert state.total_tokens == 120
        architect = state.components[ARCHITECT_COMPONENT]
        assert architect.usage_calls == 1
        assert architect.cost_usd == pytest.approx(0.5)

    def test_the_success_path_records_the_spend(self, tmp_path: Path) -> None:
        _decompose(tmp_path, MeteringAgent(MINOR_ISSUE_OUTPUT))
        state, _ = load_run_state(tmp_path)
        assert state.usage_calls == 1
        assert state.cost_usd == pytest.approx(0.5)

    def test_every_retry_is_charged(self, tmp_path: Path) -> None:
        """A failed attempt cost real tokens; the meter never forgets it."""
        _decompose(
            tmp_path,
            MeteringAgent("this is not json at all", VALID_DECOMPOSE_OUTPUT),
        )
        state, _ = load_run_state(tmp_path)
        assert state.usage_calls == 2
        assert state.cost_usd == pytest.approx(1.0)

    def test_a_crash_after_the_call_still_records_the_spend(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(RuntimeError, match="architect exploded"):
            _decompose(tmp_path, MeteringAgent("", crash=True))
        state, _ = load_run_state(tmp_path)
        assert state.cost_usd == pytest.approx(0.5)

    def test_the_usage_event_names_the_architect_role(
        self,
        tmp_path: Path,
    ) -> None:
        """The meter's phase key is the ROLE name, which is what the
        coverage footer prints. It must be the fifth role's name, not the
        ``decompose``/``audit`` lifecycle phases this component reports
        elsewhere.

        #281: the two axes are asserted against DIFFERENT constants, and
        that is the property. The phase axis is kstrl's vocabulary on
        both sides, so it stays the bare role name; the component axis is
        one the architect LLM also writes to, so the role's key is
        namespaced out of reach of any component id.
        """
        _decompose(tmp_path, MeteringAgent(MINOR_ISSUE_OUTPUT))
        run_dir = next(iter((tmp_path / ".kstrl" / "runs").iterdir()))
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
        usage = [e for e in events if e.get("event") == "component_usage"]
        assert len(usage) == 1
        assert usage[0]["component"] == ARCHITECT_COMPONENT
        assert usage[0]["data"]["phase"] == ARCHITECT_ROLE

    def test_an_agent_that_reports_nothing_adds_no_phantom_row(
        self,
        tmp_path: Path,
    ) -> None:
        """Zero calls means the architect never ran, and an empty table
        says the opposite. ``MockDecomposeAgent`` has no ``usage_records``
        at all, which is also every pre-R3.1 third-party agent."""
        console = io.StringIO()
        _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT), console)
        assert "Architect usage" not in console.getvalue()
        state, _ = load_run_state(tmp_path)
        assert state.usage_calls == 0


class TestArchitectUsageIsPrinted:
    """The second half of #257: the number has to reach the operator, not
    just the event stream."""

    def test_the_rollup_prints_on_the_halt_path(self, tmp_path: Path) -> None:
        console = io.StringIO()
        with pytest.raises(SpecBlockerError):
            _decompose(tmp_path, MeteringAgent(BLOCKER_OUTPUT), console)
        printed = console.getvalue()
        assert "Architect usage" in printed
        rows = [line for line in printed.splitlines() if "architect" in line and "0.5000" in line]
        assert len(rows) == 1, printed
        assert "120" in rows[0]

    def test_the_rollup_prints_on_the_success_path(
        self,
        tmp_path: Path,
    ) -> None:
        console = io.StringIO()
        _decompose(tmp_path, MeteringAgent(MINOR_ISSUE_OUTPUT), console)
        assert "Architect usage" in console.getvalue()
        assert "0.5000" in console.getvalue()
        # The summary of the written plan (two components, one story each).
        # In the same test so that the test count stays (#776 L11): without
        # it, a summary that lost its lines left the suite green (#776 B3D3 plants).
        assert "Components:   2" in console.getvalue()
        assert "Total stories: 2" in console.getvalue()

    def test_the_heading_is_not_the_factory_run_heading(
        self,
        tmp_path: Path,
    ) -> None:
        """`ks factory` decomposes and then prints its own "Usage rollup",
        whose total EXCLUDES the architect until piece B. Two tables under
        one heading would invite reading the later one as the whole
        spend."""
        console = io.StringIO()
        _decompose(tmp_path, MeteringAgent(MINOR_ISSUE_OUTPUT), console)
        assert "Usage rollup" not in console.getvalue()

    def test_an_unpriced_call_gets_the_coverage_warning(
        self,
        tmp_path: Path,
    ) -> None:
        """The codex case: tokens reported, no price. Without the footer
        the operator reads a $0.00 total as "it was free"."""
        console = io.StringIO()
        _decompose(tmp_path, MeteringAgent(MINOR_ISSUE_OUTPUT, cost=None), console)
        printed = console.getvalue()
        assert "cost coverage is EMPTY" in printed
        assert "0 of 1 metered call(s) reported a cost" in printed
        assert "architect (1 of 1 call(s), 120 token(s) unpriced)" in printed
        assert "lower bound" in printed
        # No price is inferred for an uncovered call.
        assert "0.0000" not in printed

    def test_a_priced_call_gets_no_coverage_warning(
        self,
        tmp_path: Path,
    ) -> None:
        console = io.StringIO()
        _decompose(tmp_path, MeteringAgent(MINOR_ISSUE_OUTPUT), console)
        # Anchored on a printed table, so this cannot pass by printing
        # nothing at all.
        assert "Architect usage" in console.getvalue()
        assert "coverage is" not in console.getvalue()


class BrokenPipeUI(PlainUI):
    """A terminal that has gone away, which is what `| head -5` leaves."""

    def subsection(self, text: str) -> None:
        raise BrokenPipeError(32, "Broken pipe")


class TestReportingNeverReplacesTheHalt:
    """#257 review: a ``finally`` that raises REPLACES the exception
    propagating through it, so the usage report could destroy the very
    halt it was written to cover."""

    def test_a_broken_pipe_does_not_swallow_the_blocker_halt(
        self,
        tmp_path: Path,
    ) -> None:
        """`ks decompose | head -5` on a spec with blockers: the pipe
        closes, the rollup's write raises, and without isolation the
        BrokenPipeError displaces SpecBlockerError - turning the
        documented exit code 2 into a traceback."""
        spec_file = _spec_root(tmp_path)
        with pytest.raises(SpecBlockerError):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MeteringAgent(BLOCKER_OUTPUT),  # type: ignore[arg-type]
                ui=BrokenPipeUI(no_color=True, file=io.StringIO()),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

    def test_a_broken_pipe_does_not_swallow_a_success(
        self,
        tmp_path: Path,
    ) -> None:
        """The other direction: a dead terminal must not turn a
        successful decomposition into a failure either."""
        spec_file = _spec_root(tmp_path)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=MeteringAgent(MINOR_ISSUE_OUTPUT),  # type: ignore[arg-type]
            ui=BrokenPipeUI(no_color=True, file=io.StringIO()),
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )
        assert len(manifest.components) == 2


class TestUsageIsPerCallNotCumulative:
    """#257 review: ``usage_records`` is cumulative for the life of the
    agent instance. Reading it at a call boundary is correct only while
    every caller passes a fresh agent, and ``decompose_spec`` is public,
    so that invariant is invisible to the caller who would break it."""

    def test_a_reused_agent_reports_only_the_second_run(
        self,
        tmp_path: Path,
    ) -> None:
        """Re-running decompose after editing the spec, holding the
        agent. Run 2 must report run 2, not run 1 plus run 2."""
        agent = MeteringAgent(MINOR_ISSUE_OUTPUT)
        first, second = io.StringIO(), io.StringIO()
        _decompose(tmp_path / "a", agent, first)
        _decompose(tmp_path / "b", agent, second)

        # The agent really did accumulate; the report is what must not.
        assert len(agent.usage_records) == 2
        for console in (first, second):
            printed = console.getvalue()
            assert "0.5000" in printed
            assert "1.0000" not in printed, printed

    def test_the_event_is_per_call_too(self, tmp_path: Path) -> None:
        """Not just the printed table: the ComponentUsage payload the
        reducer and `serve.read_run_spend` fold must be per-call."""
        agent = MeteringAgent(MINOR_ISSUE_OUTPUT)
        _decompose(tmp_path / "a", agent)
        _decompose(tmp_path / "b", agent)
        state, _ = load_run_state(tmp_path / "b")
        assert state.usage_calls == 1
        assert state.cost_usd == pytest.approx(0.5)


class _SizedFirstAttemptAgent:
    """First attempt: ``kind`` "oversize" (over the byte cap) or "timeout" (a recorded kill)."""

    name = "sized-first"

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._calls = 0
        self.final_message: str | None = None
        self.usage_records: list[UsageRecord] = []
        self.read_past_cap = False
        self.prompts: list[str] = []

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self._calls += 1
        self.prompts.append(prompt)
        if self._calls == 1:
            if self._kind == "oversize":
                yield "x" * 4096
                self.read_past_cap = True
                yield "never read"
            else:
                self.usage_records.append(UsageRecord(source="timeout"))
                yield VALID_DECOMPOSE_OUTPUT
            return
        yield from VALID_DECOMPOSE_OUTPUT.splitlines()
        self.final_message = VALID_DECOMPOSE_OUTPUT.splitlines()[-1]


def _write_linear_config(root: Path, api_url: str = "", dry_run: bool = True) -> None:
    url = f'api_url = "{api_url}"\nmin_request_interval = 0.0\n' if api_url else ""
    body = f'[linear]\nenabled = true\nteam_id = "t"\ndry_run = {str(dry_run).lower()}\n{url}'
    (root / "kstrl.toml").write_text(body)


def _serve_linear(identifier: str | None, status: int = 200) -> http.server.HTTPServer:
    """A Linear GraphQL stand-in whose created issues all carry ``identifier``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            input_ = body.get("variables", {}).get("input", {})
            if "projectCreate" in body["query"]:
                data: dict[str, object] = {
                    "projectCreate": {"success": True, "project": {"id": "proj-1"}}
                }
            else:
                issue = {"id": input_.get("id", "i"), "identifier": identifier}
                data = {"issueCreate": {"success": True, "issue": issue}}
            payload = json.dumps({"data": data}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TestDecomposeUncoveredBranches:
    """Branches of ``_decompose_spec_impl`` that no other test operates (#776 G1)."""

    def test_oversize_reply_is_one_failed_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kstrl.decompose.MAX_AGENT_OUTPUT_BYTES", 2048)
        console = io.StringIO()
        agent = _SizedFirstAttemptAgent("oversize")
        _decompose(tmp_path, agent, console)
        assert "aborting this attempt" in console.getvalue()
        assert agent.read_past_cap is False
        assert "agent output exceeded size cap" in agent.prompts[1]
        state, _ = load_run_state(tmp_path)
        architect = state.components[ARCHITECT_COMPONENT]
        decompose_phases = [p for p in architect.phase_history if p["phase"] == "decompose"]
        assert [p["passed"] for p in decompose_phases] == [False, True]
        assert architect.status == "completed"

    def test_timed_out_reply_is_not_parsed(self, tmp_path: Path) -> None:
        agent = _SizedFirstAttemptAgent("timeout")
        _decompose(tmp_path, agent)
        assert agent._calls == 2
        assert "timed out" in agent.prompts[1]
        state, _ = load_run_state(tmp_path)
        architect = state.components[ARCHITECT_COMPONENT]
        decompose_phases = [p for p in architect.phase_history if p["phase"] == "decompose"]
        assert [p["passed"] for p in decompose_phases] == [False, True]

    def test_linear_dry_run_names_the_branches_after_the_issues(self, tmp_path: Path) -> None:
        _write_linear_config(tmp_path)
        console = io.StringIO()
        manifest = _decompose(tmp_path, MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT), console)
        assert "Linear project created with 2 component issue(s)" in console.getvalue()
        branches = {c.id: c.branch_name for c in manifest.components}  # type: ignore[attr-defined]
        assert branches == {
            "database": "kstrl/factory/dry-1-database",
            "api": "kstrl/factory/dry-2-api",
        }

    def test_linear_single_pr_warns_that_branches_are_not_linked(self, tmp_path: Path) -> None:
        _write_linear_config(tmp_path)
        console = io.StringIO()
        _decompose(tmp_path, MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT), console, single_pr=True)
        assert (
            "single_pr mode - branch names and PR bodies are not issue-linked" in console.getvalue()
        )

    def test_linear_identifier_that_breaks_branch_validation_keeps_default_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = _serve_linear("BAD ID")
        try:
            monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_test")
            _write_linear_config(
                tmp_path, f"http://127.0.0.1:{server.server_address[1]}", dry_run=False
            )
            console = io.StringIO()
            manifest = _decompose(tmp_path, MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT), console)
        finally:
            server.shutdown()
        assert "does not form a valid branch" in console.getvalue()
        assert {c.id: c.branch_name for c in manifest.components} == {  # type: ignore[attr-defined]
            "database": "kstrl/factory/database",
            "api": "kstrl/factory/api",
        }

    def test_linear_project_failure_decomposes_without_linear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = _serve_linear(None, status=500)
        try:
            monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_test")
            _write_linear_config(
                tmp_path, f"http://127.0.0.1:{server.server_address[1]}", dry_run=False
            )
            console = io.StringIO()
            manifest = _decompose(tmp_path, MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT), console)
        finally:
            server.shutdown()
        assert "project creation failed, continuing without" in console.getvalue()
        assert "Linear project created" not in console.getvalue()
        assert len(manifest.components) == 2  # type: ignore[attr-defined]


class TestDecomposeDagAndRollback:
    def test_dependency_cycle_warns_but_writes_the_manifest(self, tmp_path: Path) -> None:
        payload = json.loads(VALID_DECOMPOSE_OUTPUT)
        payload["components"][0]["dependencies"] = ["api"]
        console = io.StringIO()
        manifest = _decompose(tmp_path, MockDecomposeAgent(json.dumps(payload)), console)
        assert "DAG validation warnings:" in console.getvalue()
        assert "cycle" in console.getvalue().lower()
        assert (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        assert len(manifest.components) == 2  # type: ignore[attr-defined]

    @skip_as_root
    def test_rollback_that_cannot_remove_a_prd_still_raises_the_save_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked: list[Path] = []

        def failing_save(self: object, path: Path) -> None:
            for prd in tmp_path.rglob("prd.json"):
                prd.parent.chmod(0o555)
                locked.append(prd.parent)
            raise OSError("manifest disk full")

        monkeypatch.setattr("kstrl.decompose.Manifest.save", failing_save)
        try:
            with pytest.raises(OSError, match="manifest disk full"):
                _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT))
            assert locked
            assert all((d / "prd.json").exists() for d in locked)
        finally:
            for d in locked:
                d.chmod(0o755)
