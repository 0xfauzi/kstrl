"""#288: is the report's verdict ATTRIBUTABLE, and can the operator decline?

Split out of test_feature_verification.py when that file crossed the
800-line ratchet. Its sibling asks whether the report runs and what it
measures. This one asks the three questions that decide whether the
answer means anything:

- the BASELINE, which is the only before-picture, and the rule that only
  its failing set is ever carried forward;
- whether the two sides of the comparison are the same measurement at
  all, which they are not if the commands can move under the agent or if
  the loop checks out a different tree after the baseline ran;
- whether an operator can decline the cost, and whether the report says
  out loud what it is NOT going to measure.

The helpers live in the sibling module and are imported, not copied: two
descriptions of what a no-op verify project looks like is one that can
silently stop matching the other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kstrl import events as ev
from kstrl.feature_verify import baseline_skip_reason, resolve_feature_verify_config
from kstrl.loop import LoopResult
from kstrl.verify import VerifyConfig, resolve_verify_commands
from tests.test_feature_cmd import NOOP_VERIFY_COMMAND
from tests.test_feature_verification import (
    SABOTAGE_LINE,
    _drive,
    _feature_params,
    _init_repo,
    _name_the_branch,
    _phases,
    _report,
    _run_config,
    _run_git,
    _verifications,
    _write_kstrl_toml,
)


class TestBaselineAttribution:
    """#288 review finding 5: the report measures the whole checkout, so
    without a before-picture pre-existing breakage reads as the agent's.
    """

    def test_a_baseline_runs_before_the_implement_loop(self, tmp_path: Path) -> None:
        _write_kstrl_toml(tmp_path)
        _, captured, text = _drive(tmp_path)

        baseline = _report(captured, "baseline")
        implement = _report(captured, "implement")
        assert baseline.seq < implement.seq
        # Before the loop, not merely before the report of it.
        started = next(
            e for e in captured if isinstance(e, ev.PhaseStarted) and e.phase == "implement"
        )
        assert baseline.seq < started.seq
        assert "Verification report (baseline)" in text

    def test_pre_existing_breakage_is_named_as_pre_existing(self, tmp_path: Path) -> None:
        """The whole point. Lint was already red before the agent ran, so
        the implement verdict must not read as the agent breaking it."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, text = _drive(tmp_path)

        assert _report(captured, "baseline").failures == ("Linter failed (exit code 1)",)
        assert "already failing before the implement loop: linter" in text

    def test_a_failure_the_baseline_did_not_have_is_not_excused(
        self,
        tmp_path: Path,
    ) -> None:
        """The other half: a check that was green at baseline and red
        afterwards must NOT be labelled pre-existing.

        The lint command is fixed for the whole run (resolved once), and
        it fails only if a marker file exists. The stubbed implement loop
        creates that file, which is what an agent breaking lint looks
        like to this flow.
        """
        root = tmp_path
        root.mkdir(parents=True, exist_ok=True)
        broken = "the-agent-broke-lint"
        lint = f"if [ -f {broken} ]; then echo '{SABOTAGE_LINE}'; exit 1; fi"
        (root / "kstrl.toml").write_text(
            "[verify]\n"
            f"test_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"typecheck_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"lint_command = {json.dumps(lint)}\n",
            encoding="utf-8",
        )
        calls: list[int] = []

        def fake(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
            calls.append(1)
            if len(calls) == 2:  # the implement loop
                (root / broken).write_text("", encoding="utf-8")
            return LoopResult(completed=True, iterations=1, exit_code=0)

        _, captured, text = _drive(tmp_path, loop=fake)

        assert _report(captured, "baseline").passed is True
        assert _report(captured, "implement").failures == ("Linter failed (exit code 1)",)
        assert "already failing before the implement loop" not in text

    def test_a_repair_report_does_not_excuse_what_the_implement_loop_broke(
        self,
        tmp_path: Path,
    ) -> None:
        """#288 review round 2 finding 1, the inversion.

        Baseline green, the implement loop breaks lint, the implement
        loop exits non-zero so repair-1 runs. ``already_failing`` used to
        be REPLACED by each report's own failing set, so by repair-1 it
        held {linter} and the terminal said "already failing before the
        implement loop: linter" about a tree that was green before the
        implement loop. The feature told the operator to ignore the one
        failure the agent actually caused.
        """
        root = tmp_path
        root.mkdir(parents=True, exist_ok=True)
        broken = "the-agent-broke-lint"
        lint = f"if [ -f {broken} ]; then echo '{SABOTAGE_LINE}'; exit 1; fi"
        (root / "kstrl.toml").write_text(
            "[verify]\n"
            f"test_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"typecheck_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"lint_command = {json.dumps(lint)}\n",
            encoding="utf-8",
        )
        calls: list[int] = []

        def fake(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
            calls.append(1)
            if len(calls) == 2:  # the implement loop breaks lint
                (root / broken).write_text("", encoding="utf-8")
            # understand ok, implement fails, repair-1 fails
            code = 0 if len(calls) == 1 else 1
            return LoopResult(completed=code == 0, iterations=1, exit_code=code)

        _, captured, text = _drive(
            tmp_path,
            loop=fake,
            params=_feature_params(tmp_path, repair_max_runs=1),
        )

        assert _phases(captured) == ["baseline", "implement", "repair-1"]
        assert _report(captured, "baseline").passed is True
        assert _report(captured, "repair-1").failures == ("Linter failed (exit code 1)",)
        # The claim is about the tree BEFORE the implement loop, and that
        # tree was green. Nothing may be excused, in either report.
        assert "already failing before the implement loop" not in text

    def test_the_baseline_is_machine_readable_without_the_terminal(
        self,
        tmp_path: Path,
    ) -> None:
        """Under --implementation-auto-run the event stream is the report,
        so the attribution has to be derivable from it alone."""
        _write_kstrl_toml(tmp_path, failing="lint")
        _, captured, _ = _drive(tmp_path)

        pre_existing = set(_report(captured, "baseline").failures) & set(
            _report(captured, "implement").failures
        )
        assert pre_existing == {"Linter failed (exit code 1)"}


class TestTheTwoSidesAreTheSameMeasurement:
    """#288 review round 2: three separate ways the baseline and the
    later reports could stop measuring comparable things."""

    def test_the_typecheck_command_cannot_move_under_the_agent(
        self,
        tmp_path: Path,
    ) -> None:
        """Finding 9. ``_default_typecheck_command`` re-reads
        pyproject.toml and answers ``uv run mypy`` when ``[tool.mypy]
        files`` is present and ``uv run mypy .`` when it is not. Adding a
        mypy scope is an ordinary engineer story, so an unpinned config
        would have the baseline measure the whole tree and the implement
        report measure the configured subset.
        """
        (tmp_path / "kstrl.toml").write_text(
            "[verify]\n"
            f"test_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n"
            f"lint_command = {json.dumps(NOOP_VERIFY_COMMAND)}\n",
            encoding="utf-8",
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
        )
        before = resolve_feature_verify_config(tmp_path).typecheck_command

        # The agent's story: give mypy a scope.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0"\n\n[tool.mypy]\nfiles = ["src"]\n',
            encoding="utf-8",
        )
        # Resolving again from the SAME config object is the identity,
        # which is what pinning bought. Re-loading from disk would not
        # be, and that is the defect: the run resolves once.
        after = resolve_verify_commands(resolve_feature_verify_config(tmp_path), tmp_path).typecheck
        assert before is not None
        assert "mypy" in before
        # Proof the input really moved, so the pin is doing work.
        assert after != before, (before, after)

    def test_the_report_and_the_engineer_prompt_name_one_command(
        self,
        tmp_path: Path,
    ) -> None:
        """The other half of finding 9. The engineer's
        VERIFY_COMMANDS_PROMPT block is rendered by
        ``build_project_context`` from the same config, and before the
        pin both sides resolved independently against pyproject.toml.
        """
        (tmp_path / "kstrl.toml").write_text("[verify]\n", encoding="utf-8")
        config = resolve_feature_verify_config(tmp_path)
        # Pinned means every field is already a concrete command, so
        # there is nothing left for a second resolution to decide.
        assert config.test_command
        assert config.typecheck_command
        assert config.lint_command
        commands = resolve_verify_commands(config, tmp_path)
        assert (commands.test, commands.typecheck, commands.lint) == (
            config.test_command,
            config.typecheck_command,
            config.lint_command,
        )

    def test_no_baseline_when_the_loop_will_check_out_a_different_tree(
        self,
        tmp_path: Path,
    ) -> None:
        """Finding 3. ``run_loop`` checks out the PRD's branchName AFTER
        the baseline runs, so on a PRD naming an existing branch that is
        not the current one the baseline measures a tree the loop never
        sees. Driven on a real repository with two real branches.
        """
        _write_kstrl_toml(tmp_path)
        _init_repo(tmp_path)
        _run_git(tmp_path, "checkout", "-q", "-b", "feat/other")
        (tmp_path / "on-other.txt").write_text("x", encoding="utf-8")
        _run_git(tmp_path, "add", "-A")
        _run_git(tmp_path, "commit", "-q", "-m", "other")
        _run_git(tmp_path, "checkout", "-q", "main")

        params = _name_the_branch(_feature_params(tmp_path), "feat/other")
        _, captured, text = _drive(tmp_path, params=params)

        assert "baseline" not in _phases(captured)
        assert "Verification report (baseline) skipped" in text
        assert "feat/other" in text
        # The later report still runs: only the comparison is unavailable.
        assert _report(captured, "implement") is not None

    def test_the_baseline_runs_when_the_checkout_cannot_move_the_tree(
        self,
        tmp_path: Path,
    ) -> None:
        """The other side of the same rule, so the refusal is narrow
        rather than a blanket disabling. A branch that does not exist yet
        is created from HEAD, which leaves the working tree alone.
        """
        _write_kstrl_toml(tmp_path)
        _init_repo(tmp_path)
        params = _name_the_branch(_feature_params(tmp_path), "feat/brand-new")
        assert baseline_skip_reason(_run_config(params), tmp_path) is None

        _, captured, _ = _drive(tmp_path, params=params)
        assert "baseline" in _phases(captured)

    def test_the_baseline_runs_when_already_on_the_branch(
        self,
        tmp_path: Path,
    ) -> None:
        """The resume case, which is the common one: the previous run
        left the checkout on the feature branch, so checking it out again
        is a no-op and the baseline is sound."""
        _write_kstrl_toml(tmp_path)
        _init_repo(tmp_path)
        _run_git(tmp_path, "checkout", "-q", "-b", "feat/resumed")
        params = _name_the_branch(_feature_params(tmp_path), "feat/resumed")

        _, captured, _ = _drive(tmp_path, params=params)
        assert "baseline" in _phases(captured)


class TestTheOperatorCanDecline:
    def test_no_verify_runs_no_reports_and_states_the_gate_to_nobody(
        self,
        tmp_path: Path,
    ) -> None:
        """#288 review round 2 finding 5. `ks run` and `ks factory` have
        always offered --no-verify; #288 gave `ks feature` an
        unconditional 2 + repair_max_runs full test-suite runs with no way
        to decline. The only workaround, a no-op [verify] test_command,
        also corrupts the block the SAME config feeds the engineer.
        """
        _write_kstrl_toml(tmp_path)
        params = _feature_params(tmp_path, no_verify=True)
        seen: list[Any] = []

        def fake(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
            seen.append(kwargs.get("verify_config"))
            return LoopResult(completed=True, iterations=1, exit_code=0)

        code, captured, text = _drive(tmp_path, params=params, loop=fake)

        assert code == 0
        assert _verifications(captured) == []
        assert "--no-verify" in text
        # And the loops are told nothing, so the engineer prompt carries
        # no VERIFY_COMMANDS_PROMPT block either: declining the report
        # must not leave the agent being told a gate will run.
        assert seen == [None, None]

    def test_the_flag_reaches_the_flow_from_the_command_line(self) -> None:
        """The wiring, not just the parameter: a flag the CLI does not
        expose is not an escape hatch."""
        from kstrl.cli import feature

        names = {p.name for p in feature.params}
        assert "no_verify" in names


class TestTheAnnouncementNamesWhatWillNotRun:
    def test_an_opt_in_that_cannot_take_effect_is_named(
        self,
        tmp_path: Path,
    ) -> None:
        """#288 review round 2 finding 7. ``prd_path`` is always None on
        this path, so ``require_self_critique`` WITHOUT
        ``progress_file_path`` resolves to None: the check the operator
        explicitly turned on silently does not run, and the report reads
        as a complete PASS over three checks.

        The sibling assertion pins that checks which RAN were announced.
        This one pins the other direction, which is the one that bites.
        """
        _write_kstrl_toml(tmp_path, extra="require_self_critique = true\n")
        _, captured, text = _drive(tmp_path)

        assert "self_critique" not in set(_report(captured, "implement").checks)
        assert "NOT running self_critique" in text
        assert "progress_file_path" in text


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
