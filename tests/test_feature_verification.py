"""#288: `ks feature` reports what the mechanical checks find.

Behaviour, not presence. Every test here drives ``run_feature`` with a
stubbed ``run_loop`` (no agent) but the REAL
``run_mechanical_verification``, against a project whose ``[verify]``
commands are real subprocesses. The failing-lint tests would go green
against a report that never ran, so they measure the report rather than
the code path that reaches it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import events as ev
from kstrl.commandrun import CommandRun
from kstrl.config import KstrlConfig
from kstrl.feature_cmd import (
    FeatureParams,
    resolve_feature_verify_config,
    run_feature,
)
from kstrl.loop import STOP_EXIT_CODE, LoopResult
from kstrl.verify import DIFF_DEPENDENT_CHECKS, VerifyConfig
from tests.test_feature_cmd import (
    NOOP_VERIFY_COMMAND,
    ScriptedChannel,
    StubAgent,
    _loop_results,
    _params,
    _ui,
)

#: The only checks ``run_mechanical_verification`` may produce on this
#: path. Pinned rather than derived: this tuple going stale IS the
#: regression it guards against, because a new check that reads
#: ``git diff <base>...HEAD`` would otherwise start reporting a pass over
#: an empty diff and nothing would say so.
HONEST_CHECKS = ("test_suite", "typecheck", "linter")


SABOTAGE_LINE = "SABOTAGE: 3 findings"
#: A gate command that fails and says why, as a shell one-liner: the
#: gates run through ``verify.run_scrubbed``, which hands a string to
#: ``/bin/sh``, so no interpreter is exec'd and ``echo`` is a builtin.
#: POSIX-only, like the rest of this suite.
FAILING_VERIFY_COMMAND = f"echo '{SABOTAGE_LINE}'; exit 1"


def _write_kstrl_toml(root: Path, *, failing: str = "", extra: str = "") -> dict[str, str]:
    """Write a ``[verify]`` section whose commands really run.

    ``failing`` names the one gate whose command exits 1 ("test",
    "typecheck" or "lint"); the rest are no-ops. Returns the commands so
    a test can assert the project's own values were the ones read.

    Written BEFORE ``_params`` is called, and ``_write_fast_verify_toml``
    leaves an existing file alone, so this wins.
    """
    root.mkdir(parents=True, exist_ok=True)
    commands = {
        gate: FAILING_VERIFY_COMMAND if gate == failing else NOOP_VERIFY_COMMAND
        for gate in ("test", "typecheck", "lint")
    }
    body = "[verify]\n" + "".join(
        f"{gate}_command = {json.dumps(command)}\n" for gate, command in commands.items()
    )
    (root / "kstrl.toml").write_text(body + extra, encoding="utf-8")
    return commands


#: Every opt-in check turned on, to prove the narrowing wins regardless.
ALL_CHECKS_ON = (
    "check_diff_scope = true\n"
    "check_bad_patterns = true\n"
    "dead_code_cleanup = true\n"
    "mutation_testing = true\n"
)
ALL_CHECKS_ON_WITH_PHASES = (
    ALL_CHECKS_ON + "\n[policy]\nenabled = true\n\n[adequacy]\nenabled = true\n"
)


def _feature_params(tmp_path: Path, **kwargs: Any) -> FeatureParams:
    return _params(tmp_path, implementation_auto_run=True, **kwargs)


def _drive(
    tmp_path: Path,
    *,
    codes: tuple[int, ...] = (0, 0),
    iterations: int = 1,
    params: FeatureParams | None = None,
    channel: ScriptedChannel | None = None,
    loop: Any = None,
) -> tuple[int, list[ev.Event], str]:
    """Run the flow, returning (exit code, emitted events, UI text)."""
    ui, stream = _ui()
    captured: list[ev.Event] = []
    run = CommandRun(
        run_id="test-run",
        kind="feature",
        bus=ev.EventBus(ev.CallbackSink(captured.append), run_id="test-run", component="demo"),
        paths=None,
    )
    with (
        patch("kstrl.feature_cmd.run_loop", loop or _loop_results(*codes, iterations=iterations)),
        patch("kstrl.feature_cmd.get_agent", return_value=StubAgent()),
    ):
        code = run_feature(
            params if params is not None else _feature_params(tmp_path),
            KstrlConfig(),
            StubAgent(),
            ui,
            tmp_path,
            interaction=channel,
            run=run,
        )
    return code, captured, stream.getvalue()


def _verifications(captured: list[ev.Event]) -> list[ev.VerificationResultEvent]:
    return [e for e in captured if isinstance(e, ev.VerificationResultEvent)]


class TestTheCheckActuallyRuns:
    def test_a_failing_lint_is_reported_to_the_terminal(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, _, text = _drive(tmp_path)

        assert "Verification report (implement)" in text
        assert "linter" in text and "FAIL" in text
        assert "verification: FAIL (1 of 3 checks failed)" in text
        # The linter's own output, not just a verdict: a report that
        # cannot say WHAT failed is not a report.
        assert SABOTAGE_LINE in text
        # Report only. The flow's exit code is what it always was.
        assert code == 0

    def test_a_failing_lint_is_reported_to_the_event_stream(self, tmp_path: Path) -> None:
        """Under --implementation-auto-run nobody is at the screen, so
        events.jsonl is the report that counts."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path)

        results = _verifications(captured)
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].checks == HONEST_CHECKS
        assert results[0].failures == ("Linter failed (exit code 1)",)
        assert results[0].component == "demo"
        # #288: which loop was measured, and that nothing gated on it.
        # Without these a consumer filtering events.jsonl by type reads
        # this next to phase_completed(implement, passed=True) as a
        # contradiction.
        assert results[0].phase == "implement"
        assert results[0].advisory is True

    def test_the_report_does_not_change_the_phase_verdict(self, tmp_path: Path) -> None:
        """A failing check reports; it does not halt, and it does not
        rewrite what the implement phase said about itself."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path)

        implement = [
            e for e in captured if isinstance(e, ev.PhaseCompleted) and e.phase == "implement"
        ]
        assert len(implement) == 1
        assert implement[0].passed is True
        assert any(isinstance(e, ev.ComponentCompleted) for e in captured)

    def test_green_commands_report_a_pass(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path)
        code, captured, text = _drive(tmp_path)

        assert "verification: PASS (3 checks)" in text
        results = _verifications(captured)
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].checks == HONEST_CHECKS
        assert results[0].failures == ()
        assert code == 0

    def test_the_commands_that_run_are_the_project_s_own(self, tmp_path: Path) -> None:
        """Not a default: moving the sabotage onto the test command must
        move which gate fails, which is only true if the project's
        [verify] section is what ran."""
        _write_kstrl_toml(tmp_path, failing="test")
        _, captured, text = _drive(tmp_path)

        assert _verifications(captured)[0].failures == ("Tests failed (exit code 1)",)
        assert SABOTAGE_LINE in text


class TestOnlyHonestChecksRun:
    def test_every_diff_based_check_stays_off_however_kstrl_toml_is_written(
        self,
        tmp_path: Path,
    ) -> None:
        """The anti-staleness pin. tmp_path is not a git repository, so a
        diff-based check that DID run here would read an empty file list
        and report a pass over nothing measured. Turning all of them on in
        kstrl.toml must not reach the report.
        """
        _write_kstrl_toml(tmp_path, extra=ALL_CHECKS_ON_WITH_PHASES)
        _, captured, _ = _drive(tmp_path)

        assert _verifications(captured)[0].checks == HONEST_CHECKS

    def test_a_verify_toggle_this_test_has_never_heard_of_cannot_reach_the_report(
        self,
        tmp_path: Path,
    ) -> None:
        """The fail-OPEN hole, closed by introspection rather than by a
        list somebody has to remember to extend.

        The test above enables the four toggles this change knows about.
        A seventh diff-reading check added later, gated by a NEW
        ``[verify]`` bool that defaults False, would sail past it: the
        narrowing does not know the field, the operator turns it on, and
        it reports a pass over an empty diff. So enable EVERY boolean
        ``[verify]`` field there is, and require the check set to still
        be exactly the three that read no diff.
        """
        toggles = [
            f.name
            for f in dataclasses.fields(VerifyConfig)
            if f.type in ("bool", bool) or isinstance(getattr(VerifyConfig(), f.name), bool)
        ]
        assert "check_diff_scope" in toggles, toggles
        _write_kstrl_toml(
            tmp_path,
            extra="".join(f"{name} = true\n" for name in toggles)
            + "\n[policy]\nenabled = true\n\n[adequacy]\nenabled = true\n",
        )
        _, captured, _ = _drive(tmp_path)

        assert _verifications(captured)[0].checks == HONEST_CHECKS

    def test_the_narration_names_what_was_not_measured(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path)
        _, _, text = _drive(tmp_path)

        for name in DIFF_DEPENDENT_CHECKS:
            assert name in text

    def test_resolve_keeps_the_commands_and_drops_the_diff_checks(
        self,
        tmp_path: Path,
    ) -> None:
        commands = _write_kstrl_toml(tmp_path, extra=ALL_CHECKS_ON)
        config = resolve_feature_verify_config(tmp_path)

        assert config.check_diff_scope is False
        assert config.check_bad_patterns is False
        assert config.dead_code_cleanup is False
        assert config.mutation_testing is False
        # Untouched: these three are what the engineer prompt states and
        # what the report runs, and they must agree.
        assert config.test_command == commands["test"]
        assert config.typecheck_command == commands["typecheck"]
        assert config.lint_command == commands["lint"]


class TestExitPathRule:
    """A path where no production code was written gets no report."""

    def test_no_report_when_understand_fails(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(tmp_path, codes=(1,))
        assert code == 1
        assert _verifications(captured) == []

    def test_no_report_when_the_operator_quits_to_amend(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(
            tmp_path,
            codes=(0,),
            params=_params(tmp_path),
            channel=ScriptedChannel(choice=1),
        )
        assert code == 0
        assert _verifications(captured) == []

    def test_no_report_when_the_gate_cannot_prompt(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(
            tmp_path,
            codes=(0,),
            params=_params(tmp_path),
            channel=ScriptedChannel(choice=0, promptable=False),
        )
        assert code == 2
        assert _verifications(captured) == []

    def test_no_report_when_the_prd_has_no_stories(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path, failing="lint")
        code, captured, _ = _drive(
            tmp_path, codes=(0,), params=_feature_params(tmp_path, stories=0)
        )
        assert code == 0
        assert _verifications(captured) == []

    def test_no_report_when_the_engineer_never_ran_an_iteration(self, tmp_path: Path) -> None:
        """iterations == 0 is the loop halting in preflight (a failed
        branch checkout, a stop request before iteration 1). There is no
        agent output to measure."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, text = _drive(tmp_path, codes=(0, 1), iterations=0)
        assert _verifications(captured) == []
        assert "Verification report" not in text

    def test_no_report_when_the_operator_asked_the_loop_to_stop(
        self,
        tmp_path: Path,
    ) -> None:
        """Exit 130 is a stop request honoured mid-loop, so iterations is
        non-zero and the iterations guard alone would let the report
        through. Somebody who pressed stop must not then be made to wait
        out a test suite: measured on this repo at 246s, and bounded only
        by 3 x subprocess_timeout (900s at the default) in general."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, text = _drive(tmp_path, codes=(0, STOP_EXIT_CODE))
        assert _verifications(captured) == []
        assert "Verification report" not in text

    def test_every_repair_attempt_reports_and_says_which(self, tmp_path: Path) -> None:
        """One report per engineer loop, each naming its own loop. Without
        the phase field a consumer filtering events.jsonl by type could
        count three and not tell them apart."""
        _write_kstrl_toml(tmp_path, failing="lint")
        params = _feature_params(tmp_path, repair_max_runs=2)
        _, captured, _ = _drive(tmp_path, codes=(0, 1, 1, 1), params=params)

        results = _verifications(captured)
        assert [r.phase for r in results] == ["implement", "repair-1", "repair-2"]
        assert all(r.passed is False for r in results)
        assert all(r.advisory is True for r in results)


class TestTheReportDoesNotDistortTheRunRecord:
    def test_the_phase_duration_excludes_the_report(self, tmp_path: Path) -> None:
        """The implement phase's duration is the engineer loop's own, so
        it stays comparable to a pre-#288 run. Asserted against the
        report's own duration rather than a threshold: the gate commands
        here are near-instant, so a wall-clock bound would measure the
        machine. The report is emitted first, so a phase duration that
        included it would have to be at least as large."""
        _write_kstrl_toml(tmp_path)
        _, captured, _ = _drive(tmp_path)

        implement = next(
            e for e in captured if isinstance(e, ev.PhaseCompleted) and e.phase == "implement"
        )
        report = _verifications(captured)[0]
        # The report lands INSIDE the phase bracket, which is what would
        # have folded its wall clock into the phase's duration.
        assert report.seq < implement.seq
        # The loop is a stub that returns instantly, so the phase's own
        # work rounds to 0.0s. Anything larger is the report leaking in.
        assert implement.duration_seconds == 0.0

    def test_the_terminal_says_what_it_is_running_before_it_blocks(
        self,
        tmp_path: Path,
    ) -> None:
        """The gates capture their output, so without this the terminal
        is dead for the length of the project's test suite with nothing
        said about why."""
        commands = _write_kstrl_toml(tmp_path)
        _, _, text = _drive(tmp_path)

        running = [line for line in text.splitlines() if "running:" in line]
        assert len(running) == 3
        for command in commands.values():
            assert any(command in line for line in running)
        # Before, not after: the announcement has to precede the verdict
        # it is warning the operator to wait for.
        assert text.index("running:") < text.index("verification: PASS")


class TestVerifyConfigThreading:
    def test_understand_states_nothing_and_the_engineer_loops_state_the_gate(
        self,
        tmp_path: Path,
    ) -> None:
        """#261 is preserved, not reversed: None still means "no gate
        runs", and it is still true for the understand loop, because no
        gate runs on an understand file."""
        commands = _write_kstrl_toml(tmp_path)
        seen: list[VerifyConfig | None] = []

        def fake(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
            seen.append(kwargs.get("verify_config"))
            code = 0 if len(seen) != 2 else 1
            return LoopResult(completed=code == 0, iterations=1, exit_code=code)

        params = _feature_params(tmp_path, repair_max_runs=1)
        _drive(tmp_path, params=params, loop=fake)

        assert len(seen) == 3  # understand, implement, repair-1
        assert seen[0] is None
        assert seen[1] is not None
        assert seen[1].lint_command == commands["lint"]
        # ONE object, so the commands the engineer is told about cannot
        # drift from the commands the report runs.
        assert seen[2] is seen[1]
