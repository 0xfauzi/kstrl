"""The engineer's progress file must live inside the component's scope.

Regression net for a defect found by a real paid factory run: the
harness told the engineer to write ``scripts/kstrl/progress.txt`` while
DECOMPOSE_PROMPT told the architect that a component may only write
under ``scripts/kstrl/feature/<component-id>/``. The engineer obeyed,
Phase 1 ``diff_scope`` reported "files outside allowed scope", and the
component was failed and retried from base. Measured cost of one such
retry: $12.93.

The property under test is a containment invariant, not a string:
the DEFAULT progress path for a factory component is inside that
component's own ``allowedPaths``, judged by the same
``guards.path_is_allowed`` the diff-scope check uses. Explicit
configuration still wins, and the single-component layout used by
``ks run`` is unchanged.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig, component_progress_path
from kstrl.decompose import _generate_component_prd
from kstrl.events import EventBus, V1CompatSink
from kstrl.factory import (
    AdversarialAgentSelection,
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    run_factory,
)
from kstrl.guards import path_is_allowed
from kstrl.knowledge import KnowledgeConfig
from kstrl.manifest import Component, Manifest
from kstrl.observability import NotifyConfig, NotifyHooks, ProgressLog
from kstrl.pipeline import ComponentPipeline, PipelineHooks
from kstrl.prd import PRD
from kstrl.review import ReviewResult
from kstrl.security import SecurityResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerificationResult, VerifyConfig, run_mechanical_verification

COMPONENT_ID = "hmac-sign-verify"
FEATURE_DIR = f"scripts/kstrl/feature/{COMPONENT_ID}"

# Verbatim shape of the architect output that triggered the failure:
# a source root, a test root, and the component's own feature subtree.
ARCHITECT_ALLOWED_PATHS = [
    "hmac_signer/",
    "tests/",
    f"{FEATURE_DIR}/",
]


def _architect_component(comp_id: str = COMPONENT_ID) -> dict[str, Any]:
    """One component exactly as DECOMPOSE_PROMPT tells the architect to
    emit it (rule 12: allowedPaths carries the feature subtree)."""
    return {
        "id": comp_id,
        "title": "HMAC sign/verify",
        "description": "Sign and verify payloads",
        "dependencies": [],
        "allowedPaths": [
            "hmac_signer/", "tests/", f"scripts/kstrl/feature/{comp_id}/",
        ],
        "userStories": [
            {
                "id": "US-001",
                "title": "Sign a payload",
                "acceptanceCriteria": [
                    "WHEN a payload is signed THE SYSTEM SHALL return a digest",
                    "WHEN the key is empty THE SYSTEM SHALL raise ValueError",
                ],
                "priority": 1,
                "passes": False,
                "notes": "",
            },
        ],
    }


def _component(comp_id: str = COMPONENT_ID) -> Component:
    return Component(
        comp_id, comp_id.title(), "Desc", [],
        f"scripts/kstrl/feature/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def _manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="test",
        base_branch="main", single_pr=False, components=components,
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        progress_file=root / "scripts" / "kstrl" / "progress.txt",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _setup_project(root: Path, comp_ids: list[str]) -> None:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n")
    for comp_id in comp_ids:
        feature_dir = kstrl_dir / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))


# ---------------------------------------------------------------------------
# The invariant that was violated
# ---------------------------------------------------------------------------


class TestProgressPathInsideAllowedPaths:
    def test_default_progress_path_is_inside_component_allowed_paths(
        self, tmp_path: Path,
    ) -> None:
        """THE regression: the default progress path for a factory
        component passes the same guard the diff-scope check applies.

        Both sides come from production code - the PRD is written by
        decompose's real writer and re-read by PRD.load (so allowedPaths
        is what the factory would actually enforce), and the progress
        path is resolved by the factory's own resolver.
        """
        prd_path = _generate_component_prd(
            _architect_component(), tmp_path, "kstrl/factory/hmac",
        )
        rel_prd = prd_path.relative_to(tmp_path).as_posix()
        allowed = PRD.load(prd_path).allowed_paths
        assert allowed == ARCHITECT_ALLOWED_PATHS

        progress_rel = _base_config(tmp_path).component_progress_file(
            rel_prd, tmp_path,
        )

        assert path_is_allowed(progress_rel, allowed), (
            f"{progress_rel} is outside the component's allowedPaths "
            f"{allowed}; Phase 1 diff_scope would fail the component"
        )

    def test_progress_path_is_a_sibling_of_the_component_prd(
        self, tmp_path: Path,
    ) -> None:
        """Derived from prdPath, the same source of truth the architect
        was told about - not a second string-built convention."""
        assert _base_config(tmp_path).component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == f"{FEATURE_DIR}/progress.txt"

    def test_single_component_layout_is_unchanged(self, tmp_path: Path) -> None:
        """``ks run`` has no feature subtree: a PRD at
        scripts/kstrl/prd.json still yields scripts/kstrl/progress.txt,
        so the standalone loop keeps its historical path."""
        assert component_progress_path(
            "scripts/kstrl/prd.json",
        ) == Path("scripts/kstrl/progress.txt")
        assert KstrlConfig().progress_file == Path("scripts/kstrl/progress.txt")


# ---------------------------------------------------------------------------
# Explicit configuration still wins
# ---------------------------------------------------------------------------


class TestExplicitConfigurationWins:
    def test_toml_progress_path_wins_for_every_component(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[paths]\nprogress = "docs/agent-progress.md"\n'
        )
        config = KstrlConfig.load(tmp_path)
        assert config.progress_file_explicit
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "docs/agent-progress.md"

    def test_env_progress_path_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PROGRESS_FILE", "docs/env-progress.md")
        config = KstrlConfig.load(tmp_path)
        assert config.progress_file_explicit
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "docs/env-progress.md"

    def test_explicit_value_equal_to_the_default_is_still_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicitness is tracked, not inferred by comparing against
        the default - an operator who pins the historical path gets it.
        """
        monkeypatch.setenv("PROGRESS_FILE", "scripts/kstrl/progress.txt")
        config = KstrlConfig.load(tmp_path)
        assert config.progress_file_explicit
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "scripts/kstrl/progress.txt"

    def test_unset_config_is_not_explicit(self, tmp_path: Path) -> None:
        assert not KstrlConfig.load(tmp_path).progress_file_explicit
        assert not KstrlConfig.from_env(tmp_path).progress_file_explicit


# ---------------------------------------------------------------------------
# The factory actually points the engineer there
# ---------------------------------------------------------------------------


class TestFactoryWiring:
    def test_worker_gets_the_feature_subtree_path(self, tmp_path: Path) -> None:
        """run_factory's worker args carry the per-component progress
        path, not the run-wide repo-root one."""
        _setup_project(tmp_path, [COMPONENT_ID])
        captured: list[tuple[Any, ...]] = []

        def fake_component(*args: Any, **kwargs: Any) -> ComponentResult:
            captured.append(args)
            return ComponentResult(
                COMPONENT_ID, success=True, iterations=1,
                duration_seconds=1.0,
            )

        with patch(
            "kstrl.factory._run_component", side_effect=fake_component,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                _manifest([_component()]),
                FactoryConfig(
                    use_worktrees=False, create_prs=False, max_parallel=1,
                    max_retries=0, retry_delay=0, review_mode="skip",
                    verify_config=None,
                    progress_log_path=tmp_path / "progress.jsonl",
                ),
                _base_config(tmp_path),
                PlainUI(no_color=True, file=io.StringIO()),
                tmp_path,
            )

        assert captured, "worker was never invoked"
        args = captured[0]
        assert f"{FEATURE_DIR}/progress.txt" in args
        assert "scripts/kstrl/progress.txt" not in args

    def test_run_component_default_derives_from_the_prd(
        self, tmp_path: Path,
    ) -> None:
        """A direct caller that omits progress_file_str gets the
        in-scope path too: the safe path is the default, not an opt-in.
        """
        from kstrl.factory import _run_component
        from kstrl.loop import LoopResult

        _setup_project(tmp_path, [COMPONENT_ID])
        seen: list[KstrlConfig] = []

        def fake_run_loop(
            config: KstrlConfig, *args: Any, **kwargs: Any,
        ) -> LoopResult:
            seen.append(config)
            return LoopResult(
                completed=True, iterations=1, exit_code=0,
                duration_seconds=0.0,
            )

        with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
            _run_component(
                component_id=COMPONENT_ID,
                prd_path_str=f"{FEATURE_DIR}/prd.json",
                worktree_path_str=str(tmp_path),
                root_dir_str=str(tmp_path),
                prompt_file_str="scripts/kstrl/prompt.md",
                agent_cmd="echo test",
                model=None, reasoning=None, agent_type=None,
                sleep_seconds=0.0,
                redirect_output=False,
            )

        assert seen, "run_loop was never called"
        assert seen[0].progress_file == tmp_path / FEATURE_DIR / "progress.txt"


# ---------------------------------------------------------------------------
# The mechanical self-critique check reads the same file
# ---------------------------------------------------------------------------


_SELF_CRITIQUE_ENTRY = """## [2026-07-27] - US-001
- Implemented signing

## Self-Critique
- If the key is empty, sign() raises, which is wrong because callers expect None.
- If the payload is huge, memory spikes, which is wrong because we buffer it.
- If the clock skews, verification fails, which is wrong because we compare ts.
"""


def _verify_config(**overrides: Any) -> VerifyConfig:
    defaults: dict[str, Any] = dict(
        test_command="true", typecheck_command="true", lint_command="true",
        check_diff_scope=False, check_bad_patterns=False,
        subprocess_timeout=10.0, require_self_critique=True,
    )
    defaults.update(overrides)
    return VerifyConfig(**defaults)


def _self_critique_check(result: VerificationResult) -> Any:
    matches = [c for c in result.checks if c.name == "self_critique"]
    assert matches, "self_critique check did not run"
    return matches[0]


class TestSelfCritiqueReadsTheComponentLog:
    def _worktree(self, tmp_path: Path) -> Path:
        feature_dir = tmp_path / FEATURE_DIR
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "b", "userStories": [],
        }))
        return feature_dir

    def test_reads_the_log_next_to_the_component_prd(
        self, tmp_path: Path,
    ) -> None:
        feature_dir = self._worktree(tmp_path)
        (feature_dir / "progress.txt").write_text(_SELF_CRITIQUE_ENTRY)

        result = run_mechanical_verification(
            tmp_path, feature_dir / "prd.json", "main", None, _verify_config(),
        )
        check = _self_critique_check(result)
        assert check.passed, check.message

    def test_explicit_progress_file_path_still_wins(
        self, tmp_path: Path,
    ) -> None:
        feature_dir = self._worktree(tmp_path)
        (feature_dir / "progress.txt").write_text("no critique here\n")
        custom = tmp_path / "docs"
        custom.mkdir()
        (custom / "progress.md").write_text(_SELF_CRITIQUE_ENTRY)

        result = run_mechanical_verification(
            tmp_path, feature_dir / "prd.json", "main", None,
            _verify_config(progress_file_path="docs/progress.md"),
        )
        check = _self_critique_check(result)
        assert check.passed, check.message

    def test_verify_progress_path_is_unset_until_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset means "derive from the component PRD"; toml and env
        both still pin an explicit file."""
        assert VerifyConfig.load(tmp_path).progress_file_path is None
        (tmp_path / "kstrl.toml").write_text(
            '[verify]\nprogress_file_path = "docs/progress.md"\n'
        )
        assert VerifyConfig.load(tmp_path).progress_file_path == "docs/progress.md"

        monkeypatch.setenv("KSTRL_VERIFY_PROGRESS_FILE", "docs/other.md")
        assert VerifyConfig.load(tmp_path).progress_file_path == "docs/other.md"
        assert VerifyConfig.from_env().progress_file_path == "docs/other.md"


# ---------------------------------------------------------------------------
# The knowledge fact-utilization metric reads the same file
# ---------------------------------------------------------------------------


class TestFactUtilizationReadsTheComponentLog:
    def test_metric_reads_the_component_progress_log(
        self, tmp_path: Path,
    ) -> None:
        """The metric's progress path was a second hardcoded copy of the
        out-of-scope default, so it silently read nothing (or a stale
        sibling) for every decomposed component."""
        comp = _component()
        wt_path = tmp_path / "wt"
        (wt_path / FEATURE_DIR).mkdir(parents=True)
        (wt_path / FEATURE_DIR / "progress.txt").write_text(
            "component log: fact-alpha referenced\n"
        )
        (wt_path / "scripts" / "kstrl" / "progress.txt").write_text(
            "decoy root log\n"
        )

        seen: list[str] = []

        def fake_measure(
            prefix: str, diff: str, progress: str,
        ) -> dict[str, int]:
            seen.append(progress)
            return {"injected": 1, "referenced": 1}

        ui = PlainUI(no_color=True, file=io.StringIO())
        pipeline = ComponentPipeline(
            manifest=_manifest([comp]),
            manifest_path=tmp_path / "manifest.json",
            factory_config=FactoryConfig(
                use_worktrees=False, create_prs=False, max_parallel=1,
                max_retries=0, retry_delay=0, review_mode="skip",
            ),
            base_config=_base_config(tmp_path),
            ui=ui,
            root_dir=tmp_path,
            run_id="run-test",
            bus=EventBus(
                V1CompatSink(ProgressLog(
                    tmp_path / "progress.jsonl", run_id="run-test",
                )),
                run_id="run-test",
            ),
            journal_path=None,
            notify=NotifyHooks(
                NotifyConfig(), run_id="run-test", project="t", warn=ui.warn,
            ),
            review_selection=AdversarialAgentSelection(
                phase="review", agent_cmd=None, agent_type=None, model=None,
                reasoning=None, source="explicit", identity="test-review",
            ),
            security_selection=None,
            knowledge_config=KnowledgeConfig(enabled=True),
            factory_result=FactoryResult(),
            hooks=PipelineHooks(
                run_mechanical_verification=(
                    lambda *a, **k: VerificationResult(passed=True, checks=[])
                ),
                run_review=lambda *a, **k: ReviewResult(
                    passed=True, mode="advisory",
                ),
                run_chunked_review=lambda *a, **k: ReviewResult(
                    passed=True, mode="hard",
                ),
                run_security_review=lambda *a, **k: SecurityResult(
                    passed=True, mode="advisory",
                ),
                run_chunked_security_review=lambda *a, **k: SecurityResult(
                    passed=True, mode="hard",
                ),
                distill_facts=lambda *a, **k: (1, "1 fact written"),
                build_knowledge_context=lambda *a, **k: "FACT: fact-alpha",
                measure_fact_utilization=fake_measure,
                cleanup_worktree=lambda *a, **k: None,
            ),
            worktree_paths={comp.id: wt_path},
            component_contexts={},
            fresh_base_retry_ids=set(),
            component_failure_signatures={},
        )

        pipeline._phase_distill(
            comp,
            ComponentResult(
                comp.id, success=True, iterations=1, duration_seconds=1.0,
            ),
            wt_path,
            "diff",
        )

        assert seen == ["component log: fact-alpha referenced\n"]


class TestInLoopGuardSeesTheComponentScope:
    """The in-loop ALLOWED_PATHS guard must enforce the COMPONENT's scope.

    Production finding: `_run_component` was handed
    `base_config.allowed_paths` - the run-wide `--allowed-paths` flag,
    empty in every ordinary factory run. `if config.allowed_paths` in
    run_loop was therefore False and `guards.enforce_allowed_paths`
    never executed. The guard existed and was inert, so a scope
    violation surfaced only at Phase 1, after the whole engineer loop
    had been paid for: measured at $12.93 for one component, against
    ~$2.50 for the single iteration it takes to catch it in-loop.
    """

    @staticmethod
    def _component(prd_rel: str) -> Any:
        from kstrl.manifest import Component

        return Component(
            "comp-a", "A", "D", [], prd_rel, "kstrl/comp-a",
        )

    def test_the_prd_scope_is_what_reaches_the_worker(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.config import KstrlConfig
        from kstrl.factory import _component_scope

        prd_rel = "scripts/kstrl/feature/comp-a/prd.json"
        prd = tmp_path / prd_rel
        prd.parent.mkdir(parents=True)
        prd.write_text(json.dumps({
            "branchName": "kstrl/comp-a",
            "allowedPaths": ["src/", "tests/"],
            "userStories": [],
        }))
        scope = _component_scope(
            self._component(prd_rel), tmp_path, KstrlConfig(),
        )
        assert scope == ["src/", "tests/"]

    def test_an_empty_run_wide_flag_no_longer_disables_the_guard(
        self, tmp_path: Path,
    ) -> None:
        """The exact production condition: no --allowed-paths flag."""
        from kstrl.config import KstrlConfig
        from kstrl.factory import _component_scope

        prd_rel = "scripts/kstrl/feature/comp-a/prd.json"
        prd = tmp_path / prd_rel
        prd.parent.mkdir(parents=True)
        prd.write_text(json.dumps({
            "branchName": "kstrl/comp-a",
            "allowedPaths": ["hmac_signer/", "tests/"],
            "userStories": [],
        }))
        base = KstrlConfig()
        assert not base.allowed_paths, "precondition: run-wide flag unset"
        scope = _component_scope(self._component(prd_rel), tmp_path, base)
        assert scope, "guard would be inert with a falsy scope"

    def test_a_legacy_prd_falls_back_to_the_run_wide_flag(
        self, tmp_path: Path,
    ) -> None:
        from kstrl.config import KstrlConfig
        from kstrl.factory import _component_scope

        prd_rel = "scripts/kstrl/feature/comp-a/prd.json"
        prd = tmp_path / prd_rel
        prd.parent.mkdir(parents=True)
        prd.write_text(json.dumps({
            "branchName": "kstrl/comp-a", "userStories": [],
        }))
        base = KstrlConfig(allowed_paths=["fallback/"])
        scope = _component_scope(self._component(prd_rel), tmp_path, base)
        assert scope == ["fallback/"]

    def test_an_unreadable_prd_fails_open_here_and_closed_at_phase_1(
        self, tmp_path: Path,
    ) -> None:
        """Asymmetry by design: the tripwire yields, the gate does not.

        Failing closed in-loop would fail a component before the
        engineer had done anything. Phase 1 still fails CLOSED on the
        same unreadable PRD (R1.5), so nothing merges unverified.
        """
        from kstrl.config import KstrlConfig
        from kstrl.factory import _component_scope
        from kstrl.verify import check_diff_scope

        comp = self._component("scripts/kstrl/feature/comp-a/prd.json")
        assert _component_scope(comp, tmp_path, KstrlConfig()) is None

        gate = check_diff_scope(
            tmp_path, "main", None,
            allowed_paths_error="PRD not found: missing",
        )
        assert not gate.passed

    def test_the_guard_reports_which_files_it_rejected(self) -> None:
        """A halt the retry agent cannot diagnose is one it will repeat.

        The guard discarded its violations (`ok, _ =`) and the factory
        rendered the halt as "Did not complete".
        """
        from kstrl.loop import LoopResult

        result = LoopResult(
            completed=False, iterations=1, exit_code=1,
            guard_violations=("uv.lock", "scripts/kstrl/progress.txt"),
        )
        assert result.guard_violations == (
            "uv.lock", "scripts/kstrl/progress.txt",
        )


class TestProgressWriterAndReaderAgree:
    """Setting only one of the two progress settings must not point the
    writer and the reader at different files."""

    @pytest.mark.parametrize(
        ("writer", "reader", "expected"),
        [
            ("a/p.txt", None, ("a/p.txt", "a/p.txt")),
            (None, "b/p.txt", ("b/p.txt", "b/p.txt")),
            ("a/p.txt", "a/p.txt", ("a/p.txt", "a/p.txt")),
            (None, None, (None, None)),
        ],
    )
    def test_one_setting_propagates_to_the_other(
        self, writer: str | None, reader: str | None,
        expected: tuple[str | None, str | None],
    ) -> None:
        from kstrl.config import reconcile_progress_paths

        assert reconcile_progress_paths(writer, reader) == expected

    def test_two_explicit_paths_are_left_alone(self) -> None:
        """An operator who named two paths meant two paths; the caller
        warns rather than silently overriding one."""
        from kstrl.config import reconcile_progress_paths

        assert reconcile_progress_paths("a/p.txt", "b/p.txt") == (
            "a/p.txt", "b/p.txt",
        )
