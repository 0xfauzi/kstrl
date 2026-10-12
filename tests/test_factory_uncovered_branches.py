"""Branches of ``run_factory`` that no other end-to-end test operates (#776 G1).

``kstrl/factory.py::_run_factory_locked`` is divided in #776 B3. Each test
here drives the real ``run_factory`` through one branch that a coverage
measurement on main showed no test enters, so a change on that branch
shows as a red test. The worker is a stub, as in
``tests/test_phase_c_coverage.py``, because the subject is the code that
surrounds it.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.autonomy import AutonomyState
from kstrl.contract import ContractConfig, ContractMode, ContractResult
from kstrl.factory import run_factory
from kstrl.factory_config import FactoryConfig
from kstrl.factory_result import ComponentResult, FactoryResult
from kstrl.linear import LinearConfig
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from tests.helpers.plan_approval import approve_plan
from tests.helpers.rootperms import skip_as_root
from tests.test_phase_c_coverage import (
    _PASSING_STACK,
    _base_config,
    _component,
    _make_manifest,
    _setup_project,
    _verify_passing,
)


def _config(**overrides: Any) -> FactoryConfig:
    base: dict[str, Any] = {
        "use_worktrees": False,
        "create_prs": False,
        "max_parallel": 1,
        "max_retries": 0,
        "retry_delay": 0,
        "review_mode": "skip",
        "project_stack": _PASSING_STACK,
        "verify_config": _verify_passing(),
    }
    base.update(overrides)
    return FactoryConfig(**base)


def _ok(comp_id: str) -> ComponentResult:
    return ComponentResult(comp_id, success=True, iterations=1)


def _drive(
    tmp_path: Path,
    manifest: Manifest,
    config: FactoryConfig,
    *,
    worker: Callable[..., ComponentResult] | list[ComponentResult] | None = None,
    contract: Callable[..., list[ContractResult]] | None = None,
    run_id: str | None = None,
    before_run: Callable[[Path, Manifest], None] | None = None,
) -> tuple[FactoryResult, str]:
    """Run the real ``run_factory``; return its result and its narration."""
    root = _setup_project(tmp_path, [c.id for c in manifest.components])
    if before_run is not None:
        before_run(root, manifest)
    narration = io.StringIO()
    if worker is None:
        worker = lambda comp_id, *a, **k: _ok(comp_id)  # noqa: E731
    stubs = [
        patch("kstrl.factory._run_component", side_effect=worker),
        patch("kstrl.git_diff.get_diff_content", return_value=""),
    ]
    if contract is not None:
        stubs.append(patch("kstrl.factory.run_contract_testing", side_effect=contract))
    with contextlib.ExitStack() as stack:
        for stub in stubs:
            stack.enter_context(stub)
        result = run_factory(
            manifest,
            config,
            _base_config(root),
            PlainUI(no_color=True, file=narration),
            root,
            run_id=run_id,
        )
    return result, narration.getvalue()


def _fake_linear(requests: list[dict[str, Any]]) -> http.server.HTTPServer:
    """A Linear GraphQL stand-in that records each request body."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            payload = json.dumps({"data": {"commentCreate": {"success": True}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TestRunStart:
    def test_linear_sink_mirrors_a_component_failure_onto_its_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[dict[str, Any]] = []
        server = _fake_linear(requests)
        try:
            monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_test")
            comp = _component("comp-a")
            comp.linear_issue_id = "issue-uuid-1"
            config = _config(
                linear_config=LinearConfig(
                    enabled=True,
                    team_id="team-uuid",
                    api_url=f"http://127.0.0.1:{server.server_address[1]}",
                    min_request_interval=0.0,
                )
            )
            result, _ = _drive(
                tmp_path,
                _make_manifest([comp]),
                config,
                worker=[ComponentResult("comp-a", success=False, error="engineer gave up")],
            )
        finally:
            server.shutdown()
        assert result.failed == ["comp-a"]
        comments = [r for r in requests if "commentCreate" in r["query"]]
        assert len(comments) == 1
        assert comments[0]["variables"]["input"]["issueId"] == "issue-uuid-1"
        assert "component failed" in comments[0]["variables"]["input"]["body"]

    def test_claim_gate_that_cannot_fire_is_announced_at_start(self, tmp_path: Path) -> None:
        _, narration = _drive(
            tmp_path,
            _make_manifest([_component("comp-a")]),
            _config(claim_agreement="block", review_mode="skip"),
        )
        assert "never fires" in narration


class TestScheduling:
    def test_an_undeletable_usage_snapshot_disables_salvage_for_the_attempt(
        self, tmp_path: Path
    ) -> None:
        run_id = "factory-20260101-000000.000000-aaaa"
        stale = (
            tmp_path / ".kstrl" / "runs" / run_id / "components" / "comp-a" / "engineer_usage.json"
        )
        # A directory with content cannot be unlinked as a file.
        (stale / "keep").mkdir(parents=True)
        result, narration = _drive(
            tmp_path,
            _make_manifest([_component("comp-a")]),
            _config(),
            run_id=run_id,
        )
        assert "Could not retire the previous usage snapshot for comp-a" in narration
        assert result.completed == ["comp-a"]

    def test_a_worker_that_raises_fails_its_component_not_the_run(self, tmp_path: Path) -> None:
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])

        def worker(comp_id: str, *a: Any, **k: Any) -> ComponentResult:
            if comp_id == "comp-a":
                raise RuntimeError("worker exploded")
            return _ok(comp_id)

        result, _ = _drive(tmp_path, manifest, _config(), worker=worker)
        assert result.failed == ["comp-a"]
        assert result.completed == ["comp-b"]
        failed = manifest.get_component("comp-a")
        assert failed is not None
        assert "worker exploded" in failed.error
        assert result.exit_code != 0


class TestContractPhase:
    @staticmethod
    def _contract_config() -> dict[str, Any]:
        return {"contract_config": ContractConfig(mode=ContractMode.TIER.value)}

    def test_a_failed_journal_write_is_a_warning_not_a_failure(self, tmp_path: Path) -> None:
        def contract(*a: Any, **k: Any) -> list[ContractResult]:
            return [ContractResult(passed=True, tier=0, components_tested=["comp-a"])]

        with patch(
            "kstrl.evolution.EvolutionJournal.append_entries",
            side_effect=OSError("journal disk full"),
        ):
            result, narration = _drive(
                tmp_path,
                _make_manifest([_component("comp-a")]),
                _config(**self._contract_config()),
                contract=contract,
            )
        assert "Evolution journal write failed (non-fatal): journal disk full" in narration
        assert result.exit_code == 0
        assert result.completed == ["comp-a"]

    def test_a_breaker_completed_in_an_earlier_run_is_retried_in_this_one(
        self, tmp_path: Path
    ) -> None:
        comp_a = _component("comp-a")
        comp_a.status = ComponentStatus.COMPLETED.value
        manifest = _make_manifest([comp_a])
        calls = {"contract": 0}

        def contract(*a: Any, **k: Any) -> list[ContractResult]:
            calls["contract"] += 1
            return [
                ContractResult(
                    passed=calls["contract"] > 1,
                    tier=0,
                    components_tested=["comp-a"],
                    breaker="comp-a" if calls["contract"] == 1 else "",
                    test_output="planted failure",
                )
            ]

        result, narration = _drive(
            tmp_path,
            manifest,
            _config(max_retries=1, **self._contract_config()),
            contract=contract,
        )
        assert "Contract breaker 'comp-a' sent back for retry" in narration
        assert calls["contract"] == 2
        assert comp_a.retries == 1
        assert result.completed == ["comp-a"]

    def test_a_terminal_failure_naming_a_failed_component_is_listed_once(
        self, tmp_path: Path
    ) -> None:
        manifest = _make_manifest([_component("comp-a"), _component("comp-b")])

        def worker(comp_id: str, *a: Any, **k: Any) -> ComponentResult:
            if comp_id == "comp-a":
                return ComponentResult(comp_id, success=False, error="engineer gave up")
            return _ok(comp_id)

        def contract(*a: Any, **k: Any) -> list[ContractResult]:
            return [
                ContractResult(
                    passed=False,
                    tier=0,
                    components_tested=["comp-a"],
                    breaker="comp-a",
                    test_output="planted failure",
                )
            ]

        result, narration = _drive(
            tmp_path, manifest, _config(**self._contract_config()), worker=worker, contract=contract
        )
        assert result.failed.count("comp-a") == 1
        assert any("breaker 'comp-a'" in line for line in result.contract_failures)
        assert "Contract failure recorded for tier 0" in narration

    def test_a_terminal_failure_naming_an_unknown_component_is_still_recorded(
        self, tmp_path: Path
    ) -> None:
        def contract(*a: Any, **k: Any) -> list[ContractResult]:
            return [
                ContractResult(
                    passed=False,
                    tier=0,
                    components_tested=["comp-a"],
                    breaker="ghost",
                    test_output="planted failure",
                )
            ]

        result, _ = _drive(
            tmp_path,
            _make_manifest([_component("comp-a")]),
            _config(**self._contract_config()),
            contract=contract,
        )
        assert "ghost" in result.failed
        assert any("breaker 'ghost'" in line for line in result.contract_failures)
        assert result.exit_code != 0


class TestRunEnd:
    def test_single_pr_url_is_recorded_on_the_result(self, tmp_path: Path) -> None:
        manifest = _make_manifest([_component("comp-a")])
        manifest.single_pr = True
        with patch(
            "kstrl.factory_summary.create_single_pr",
            return_value=("kstrl/factory/test", "https://example.invalid/pr/1"),
        ):
            result, _ = _drive(tmp_path, manifest, _config(create_prs=True, single_pr=True))
        assert result.pr_urls == ["https://example.invalid/pr/1"]

    @skip_as_root
    def test_an_autonomy_state_that_cannot_be_saved_does_not_fail_the_run(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "kstrl.toml").write_text("[autonomy]\nenabled = true\n", encoding="utf-8")
        state_dir = AutonomyState.path_for(tmp_path).parent

        def worker(comp_id: str, *a: Any, **k: Any) -> ComponentResult:
            # The directory that holds the ladder state turns read-only mid-run.
            state_dir.chmod(0o555)
            return _ok(comp_id)

        manifest = _make_manifest([_component("comp-a")])
        try:
            result, narration = _drive(
                tmp_path, manifest, _config(), worker=worker, before_run=approve_plan
            )
        finally:
            state_dir.chmod(0o755)
        assert "Autonomy state update failed (non-fatal)" in narration
        assert manifest.completed_at
        assert result.exit_code == 1

    def test_a_disabled_evolution_journal_writes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[evolution]\nenabled = false\n", encoding="utf-8")
        result, _ = _drive(tmp_path, _make_manifest([_component("comp-a")]), _config())
        assert result.completed == ["comp-a"]
        assert not (tmp_path / ".kstrl" / "evolution.jsonl").exists()
