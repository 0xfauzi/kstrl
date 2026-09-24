"""An unset time or spend limit means no limit (#467).

Owner decision, 2026-09-24: when a limit on how long or how expensive
kstrl's work may be is not set, there is no limit. Before this, the
engineer's component had a 2 h wall clock and each iteration 30 min, the
reviewers 600 s and 300 s, and the verify, contract and mutation runs
300 s and 600 s, all by default. And 0, the value every one of those keys
documents as "disabled", reached ``subprocess`` as ``timeout=0``, which
means "already expired": a gate configured with no limit failed at once.

The hang guards (``[timeout] git_operation`` and ``subprocess_default``,
``[factory] merge_timeout``) keep their defaults and are pinned here too.

Every test drives a real entry point: the engineer loop, the real
``run_mechanical_verification`` over a real git repository, the real
contract test run, ``ks init`` and ``ks config show`` in a subprocess, or
a real ``run_factory``.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl import loop as loop_mod
from kstrl.agents.base import UsageRecord, UsageTotals
from kstrl.config import KstrlConfig
from kstrl.contract import ContractConfig, run_contract_testing
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.knowledge import KnowledgeConfig, distill_facts
from kstrl.loop import COMPLETION_MARKER, LoopResult, run_loop
from kstrl.manifest import Component, Manifest
from kstrl.review import ReviewMode, run_review
from kstrl.security import SecurityConfig, SecurityMode, run_security_review
from kstrl.serve import ServeConfig, SpendLedger, check_budget
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.conftest import make_review_repo
from tests.helpers.adequacy_fixture import (
    EMPTY_CONFTEST_BASE_FILES,
    FEAT_FILES,
    only_row,
    repo_builder,
    run_adequacy,
)
from tests.helpers.fakemutmut import junit, put_mutmut_on_path

#: Real seconds the loop test may take before the fuse fails it. The loop
#: runs on a mocked clock; this bound is on the REAL one, in the test
#: thread, where no change to ``kstrl.loop`` can reach it.
FUSE_SECONDS = 60.0

#: One simulated engineer iteration: an hour, so iteration 3 ends at 3 h,
#: past the old 2 h ``component_total`` default and 6x the old 30 min
#: ``agent_iteration`` default.
HOUR = 3600.0


def _scrub_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable that could set a limit, so "unset" is true."""
    for name in list(os.environ):
        if name.startswith(("KSTRL_", "FACTORY_")):
            monkeypatch.delenv(name, raising=False)


class _Clock:
    """The loop's monotonic clock, advanced only by the fake engineer."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _HourLongEngineer:
    """Spends one simulated hour per iteration; finishes on the third."""

    name = "hour-long"
    final_message: str | None = None

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.timeouts: list[float | None] = []

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.timeouts.append(timeout)
        self._clock.now += HOUR
        yield COMPLETION_MARKER if len(self.timeouts) == 3 else "working"


def _run_three_hour_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str
) -> tuple[LoopResult, _HourLongEngineer, str]:
    """The real ``run_loop`` under limits loaded from a real kstrl.toml.

    Returns the result, the engineer, and everything the loop printed.
    """
    _scrub_limit_env(monkeypatch)
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("p", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    (tmp_path / "kstrl.toml").write_text(toml, encoding="utf-8")
    clock = _Clock()
    monkeypatch.setattr(
        loop_mod, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=lambda _s: None)
    )
    engineer = _HourLongEngineer(clock)
    config = KstrlConfig(
        max_iterations=5,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )
    box: dict[str, LoopResult] = {}
    printed = io.StringIO()

    def _drive() -> None:
        box["result"] = run_loop(
            config,
            PlainUI(no_color=True, file=printed),
            engineer,
            tmp_path,
            timeouts=TimeoutConfig.load(tmp_path),
        )

    worker = threading.Thread(target=_drive, daemon=True)
    worker.start()
    worker.join(FUSE_SECONDS)
    assert not worker.is_alive(), f"run_loop still running after {FUSE_SECONDS}s of real time"
    return box["result"], engineer, printed.getvalue()


class TestAComponentHasNoWallClockUnlessOneIsSet:
    def test_an_engineer_past_two_hours_is_not_stopped_when_no_limit_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, engineer, printed = _run_three_hour_component(
            tmp_path, monkeypatch, "[factory]\nmax_parallel = 1\n"
        )
        assert result.timeout_limit is None
        assert result.completed is True
        assert result.iterations == 3
        assert engineer.timeouts == [None, None, None]
        assert re.search(r"Agent timeout:\s*no limit\n", printed), printed
        assert re.search(r"Component timeout:\s*no limit\n", printed), printed

    def test_an_explicit_component_total_still_stops_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, engineer, printed = _run_three_hour_component(
            tmp_path, monkeypatch, "[timeout]\ncomponent_total = 60\n"
        )
        assert result.timeout_limit == "component"
        assert result.completed is False
        assert result.iterations == 1
        assert engineer.timeouts == [60.0]
        assert re.search(r"Component timeout:\s*60\.0s\n", printed), printed


class _TimeoutRecordingReviewer:
    """A reviewer that records the deadline each call was handed."""

    name = "timeout-recording"
    final_message: str | None = None

    def __init__(self, output: str) -> None:
        self._output = output
        self.timeouts: list[float | None] = []

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.timeouts.append(timeout)
        yield from self._output.splitlines()


class TestReviewerCallsHaveNoLimitUnlessOneIsSet:
    """Phase 2, Phase 2.5 and the distiller, each through its real entry."""

    def test_every_reviewer_call_gets_no_deadline_when_none_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scrub_limit_env(monkeypatch)
        repo = make_review_repo(tmp_path / "repo")
        ui = PlainUI(no_color=True, file=io.StringIO())

        reviewer = _TimeoutRecordingReviewer(repo.review_json())
        run_review(
            reviewer,
            repo.prd_path,
            repo.path,
            repo.base_branch,
            VerificationResult(passed=True, checks=[CheckResult("test_suite", True, "ok")]),
            ReviewMode.ADVISORY,
            ui,
        )

        security = SecurityConfig.load(tmp_path)
        security.mode = SecurityMode.ADVISORY.value
        auditor = _TimeoutRecordingReviewer(repo.security_json())
        run_security_review(auditor, repo.prd_path, repo.path, repo.base_branch, security, ui)

        distiller = _TimeoutRecordingReviewer('{"facts": []}')
        distill_facts(
            distiller,
            Component("comp-a", "A", "", [], "prd.json", "kstrl/comp-a"),
            "diff text",
            repo.prd_path,
            1,
            "factory-20260101-120000",
            tmp_path / "knowledge",
            KnowledgeConfig.load(tmp_path),
            repo.path,
            review_passed=True,
        )

        assert (reviewer.timeouts, auditor.timeouts, distiller.timeouts) == (
            [None],
            [None],
            [None],
        )


_mutation_repo = repo_builder(EMPTY_CONFTEST_BASE_FILES, FEAT_FILES)


class TestZeroMeansNoLimitAtEveryWait:
    """0 is the documented "no limit"; it must never reach a wait as 0."""

    @pytest.mark.parametrize("unset", [0.0, -1.0])
    def test_a_zero_verify_timeout_lets_every_gate_run(self, tmp_path: Path, unset: float) -> None:
        _mutation_repo(tmp_path)
        result = run_adequacy(tmp_path, subprocess_timeout=unset)
        for check in ("test_suite", "typecheck", "linter", "patch_coverage"):
            row = only_row(result, check)
            assert row.passed is True, (check, row.message)

    def test_a_zero_mutation_timeout_lets_both_mutation_checks_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mutation_repo(tmp_path)
        recdir = put_mutmut_on_path(
            tmp_path,
            monkeypatch,
            junit=junit(
                (1, "mod.py", 2, "killed"), (2, "mod.py", 6, "killed"), (3, "mod.py", 9, "killed")
            ),
        )
        result = run_adequacy(
            tmp_path,
            mutation_testing=True,
            diff_mutation=True,
            mutation_timeout=0.0,
        )
        only_row(result, "diff_mutation")
        only_row(result, "mutation_testing")
        argv = (recdir / "argv-run.txt").read_text(encoding="utf-8")
        assert argv.count("--paths-to-mutate=") == 2

    def test_a_zero_contract_timeout_lets_the_contract_suite_run(self, tmp_path: Path) -> None:
        root = make_review_repo(tmp_path / "repo").path
        component = Component("comp-a", "A", "", [], "prd.json", "kstrl/comp-a")
        component.status = "completed"
        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[component],
        )
        results = run_contract_testing(
            manifest,
            root,
            ContractConfig(
                test_command=f"{sys.executable} -c 'import time; time.sleep(0.2)'",
                timeout=0.0,
            ),
            PlainUI(no_color=True, file=io.StringIO()),
            components_merged=True,
        )
        assert [r.passed for r in results] == [True], [r.test_output for r in results]

    def test_a_zero_contract_timeout_lets_the_tier_check_run(self, tmp_path: Path) -> None:
        root = make_review_repo(tmp_path / "repo").path
        component = Component("comp-a", "A", "", [], "prd.json", "feature")
        component.status = "completed"
        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[component],
        )
        results = run_contract_testing(
            manifest,
            root,
            ContractConfig(
                test_command=f"{sys.executable} -c 'import time; time.sleep(0.2)'",
                timeout=0.0,
            ),
            PlainUI(no_color=True, file=io.StringIO()),
        )
        assert [r.passed for r in results] == [True], [r.test_output for r in results]


def _ks(root: Path, *args: str) -> str:
    """Run the ``ks`` CLI in a subprocess with no limit set in the env."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    done = subprocess.run(
        [sys.executable, "-m", "kstrl", *args],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _config_show(root: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """``ks config show``: ``{(section, key): (value, source)}``."""
    rows: dict[tuple[str, str], tuple[str, str]] = {}
    section = ""
    for line in _ks(root, "config", "show", "--root", str(root)).splitlines():
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line.startswith("  ") and " = " in line:
            key, _, rest = line.strip().partition(" = ")
            value, _, source = rest.rpartition("  (")
            rows[(section, key)] = (value, source.removesuffix(")"))
    return rows


#: Every work limit and spend ceiling ``ks config show`` reports.
UNSET_LIMITS = (
    ("factory", "max_adversarial_calls"),
    ("factory", "max_total_tokens"),
    ("factory", "max_cost_usd"),
    ("verify", "mutation_timeout"),
    ("verify", "subprocess_timeout"),
    ("security", "timeout_seconds"),
    ("contract", "timeout"),
    ("knowledge", "distill_timeout_seconds"),
    ("timeout", "agent_iteration"),
    ("timeout", "component_total"),
    ("timeout", "verification_check"),
    ("timeout", "review_agent"),
    ("timeout", "contract_test"),
)

#: The hang guards, which keep their defaults.
HANG_GUARDS = {
    ("timeout", "git_operation"): "30.0",
    ("timeout", "subprocess_default"): "60.0",
    ("factory", "merge_timeout"): "300.0",
}


def _uncomment_keys(text: str) -> str:
    """A kstrl.toml with every commented ``# key = value`` line made live."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        commented_key = stripped.startswith("# ") and " = " in stripped
        lines.append(stripped[2:] if commented_key and not stripped[2:3].isupper() else line)
    return "\n".join(lines) + "\n"


class TestEverySurfaceSaysNoLimit:
    def test_the_scaffolded_kstrl_toml_states_no_limit_for_every_limit(
        self, tmp_path: Path
    ) -> None:
        # `ks init` writes the scaffold; every commented key is made live;
        # `ks config show` must then read each limit FROM the file ("toml")
        # and print "no limit". A scaffold line that is missing reads as
        # "default", and one that still says 7200.0 prints 7200.0.
        _ks(tmp_path, "init", str(tmp_path), "--ui", "plain")
        scaffold = tmp_path / "kstrl.toml"
        scaffold.write_text(_uncomment_keys(scaffold.read_text(encoding="utf-8")), encoding="utf-8")
        rows = _config_show(tmp_path)
        assert {key: rows.get(key) for key in UNSET_LIMITS} == dict.fromkeys(
            UNSET_LIMITS, ("no limit", "toml")
        )
        assert {key: rows.get(key) for key in HANG_GUARDS} == {
            key: (value, "toml") for key, value in HANG_GUARDS.items()
        }

    def test_ks_config_show_says_no_limit_for_every_unset_limit(self, tmp_path: Path) -> None:
        rows = _config_show(tmp_path)
        assert {key: rows.get(key) for key in UNSET_LIMITS} == dict.fromkeys(
            UNSET_LIMITS, ("no limit", "default")
        )
        assert {key: rows.get(key) for key in HANG_GUARDS} == {
            key: (value, "default") for key, value in HANG_GUARDS.items()
        }

    def test_the_execution_header_says_no_limit_for_every_unset_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scrub_limit_env(monkeypatch)
        header = _factory_run(tmp_path, FactoryConfig.load(tmp_path), usage=None)
        for label in (
            "Agent timeout",
            "Component timeout",
            "Token ceiling",
            "Cost ceiling",
            "Adversarial calls",
        ):
            assert re.search(rf"{label}:\s*no limit\n", header), header


def _factory_run(root: Path, config: FactoryConfig, usage: UsageTotals | None) -> str:
    """One real ``run_factory`` over one component; returns what it printed.

    ``_run_component`` is the one seam: it returns a successful result
    carrying ``usage``, so the ceiling checks see real reported spend.
    """
    kstrl_dir = root / "scripts" / "kstrl"
    feature = kstrl_dir / "feature" / "comp-a"
    feature.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("p", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    story = {
        "id": "US-001",
        "title": "t",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    (feature / "prd.json").write_text(
        json.dumps({"branchName": "t", "userStories": [story]}), encoding="utf-8"
    )
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n", encoding="utf-8")
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a", "A", "", [], "scripts/kstrl/feature/comp-a/prd.json", "kstrl/comp-a"
            )
        ],
    )
    config.use_worktrees = False
    config.create_prs = False
    config.max_parallel = 1
    config.max_retries = 0
    config.retry_delay = 0
    config.review_mode = "skip"
    config.verify_config = VerifyConfig(
        test_command="true",
        typecheck_command="true",
        lint_command="true",
        check_diff_scope=False,
        check_bad_patterns=False,
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    success = ComponentResult("comp-a", success=True, iterations=1, usage=usage or UsageTotals())
    buffer = io.StringIO()
    with (
        patch("kstrl.factory._run_component", return_value=success),
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        result = run_factory(manifest, config, base, PlainUI(no_color=True, file=buffer), root)
    comp = manifest.get_component("comp-a")
    assert comp is not None
    assert result.exit_code == 0, (comp.status, comp.error, buffer.getvalue())
    return buffer.getvalue()


class TestUnsetSpendCeilingsAreUnlimited:
    def test_a_run_that_reports_huge_spend_is_not_halted_when_no_ceiling_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scrub_limit_env(monkeypatch)
        config = FactoryConfig.load(tmp_path)
        assert (config.max_total_tokens, config.max_cost_usd, config.max_adversarial_calls) == (
            0,
            0.0,
            0,
        )
        usage = UsageTotals()
        usage.add_record(
            UsageRecord(
                input_tokens=10**9,
                output_tokens=10**9,
                total_tokens=2 * 10**9,
                cost_usd=10.0**6,
                duration_seconds=1.0,
            )
        )
        out = _factory_run(tmp_path, config, usage=usage)
        assert "budget exceeded" not in out

    def test_an_unset_daily_budget_admits_after_any_spend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scrub_limit_env(monkeypatch)
        config = ServeConfig.load(tmp_path)
        assert config.daily_budget_usd == 0.0
        ledger = SpendLedger(tmp_path)
        ledger.charge(10.0**6, covered_calls=1, total_calls=1)
        assert check_budget(ledger, config).allowed is True
