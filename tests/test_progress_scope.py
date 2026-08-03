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
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import git
from kstrl.config import KstrlConfig, component_progress_path
from kstrl.decompose import _generate_component_prd
from kstrl.events import EventBus, V1CompatSink
from kstrl.factory import (
    AdversarialAgentSelection,
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    _component_scope,
    run_factory,
)
from kstrl.guards import enforce_allowed_paths, path_is_allowed
from kstrl.interaction import PromptRequest, PromptResponse
from kstrl.knowledge import KnowledgeConfig
from kstrl.loop import run_loop
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
    """A factory base config with NOTHING configured for the progress
    log. ``progress_file`` is deliberately left at its None default:
    passing a value here is now an explicit setting that is forced on
    every component (review finding 2)."""
    return KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _pipeline(
    root: Path,
    comp: Component,
    wt_path: Path,
    *,
    measure_fact_utilization: Any = None,
    run_mechanical_verification: Any = None,
    knowledge_prefix: str | None = None,
) -> ComponentPipeline:
    """A ComponentPipeline wired to stub hooks so a single phase can be
    driven directly. Only the hooks a test cares about are injected.

    ``knowledge_prefix`` stands in for the factory's submit-time capture
    (#191): the distill phase measures fact utilization against what was
    recorded here, so a test that wants a measurement must supply one.
    """
    ui = PlainUI(no_color=True, file=io.StringIO())
    pipeline = ComponentPipeline(
        manifest=_manifest([comp]),
        manifest_path=root / "manifest.json",
        factory_config=FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=0, retry_delay=0, review_mode="skip",
        ),
        base_config=_base_config(root),
        ui=ui,
        root_dir=root,
        run_id="run-test",
        bus=EventBus(
            V1CompatSink(ProgressLog(
                root / "progress.jsonl", run_id="run-test",
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
                run_mechanical_verification
                or (lambda *a, **k: VerificationResult(passed=True, checks=[]))
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
            measure_fact_utilization=(
                measure_fact_utilization
                or (lambda *a, **k: {"injected": 0, "referenced": 0})
            ),
            cleanup_worktree=lambda *a, **k: None,
        ),
        worktree_paths={comp.id: wt_path},
        component_contexts={},
        fresh_base_retry_ids=set(),
        component_failure_signatures={},
    )
    if knowledge_prefix is not None:
        pipeline.record_injected_knowledge(comp.id, knowledge_prefix)
    return pipeline


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
        """The standalone loop has no feature subtree: a PRD at
        scripts/kstrl/prd.json still yields scripts/kstrl/progress.txt,
        and an unconfigured config still resolves to the historical
        repo-root path when a concrete one is demanded."""
        assert component_progress_path(
            "scripts/kstrl/prd.json",
        ) == Path("scripts/kstrl/progress.txt")
        assert KstrlConfig().resolved_progress_file(tmp_path) == (
            tmp_path / "scripts/kstrl/progress.txt"
        )


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
        assert config.progress_file is not None
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "docs/agent-progress.md"

    def test_env_progress_path_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PROGRESS_FILE", "docs/env-progress.md")
        config = KstrlConfig.load(tmp_path)
        assert config.progress_file is not None
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "docs/env-progress.md"

    def test_explicit_value_equal_to_the_default_is_still_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicitness is carried by the sentinel, not inferred by
        comparing against the default (R2.1 removed that pattern) - an
        operator who pins the historical path gets it.
        """
        monkeypatch.setenv("PROGRESS_FILE", "scripts/kstrl/progress.txt")
        config = KstrlConfig.load(tmp_path)
        assert config.progress_file is not None
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "scripts/kstrl/progress.txt"

    def test_unset_config_is_the_none_sentinel(self, tmp_path: Path) -> None:
        assert KstrlConfig.load(tmp_path).progress_file is None
        assert KstrlConfig.from_env(tmp_path).progress_file is None
        assert KstrlConfig.from_toml(tmp_path / "kstrl.toml").progress_file is None


class TestProgrammaticallySetProgressFileIsHonored:
    """Review finding 2: a caller that CONSTRUCTS a config must be obeyed.

    ``progress_file_explicit`` was set only by the toml and env loaders,
    so ``KstrlConfig(progress_file=...)`` - the shape used by tests,
    embedders and the SDK, and honored by ``run_factory(..., base_config)``
    before this PR - was silently ignored and the derived per-component
    path used instead. The None sentinel makes construction and attribute
    assignment unambiguously explicit with no second field to coordinate.
    """

    def test_constructor_value_is_honored(self, tmp_path: Path) -> None:
        config = KstrlConfig(progress_file=Path("docs/custom-progress.md"))
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "docs/custom-progress.md"

    def test_attribute_assignment_is_honored(self, tmp_path: Path) -> None:
        config = _base_config(tmp_path)
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == f"{FEATURE_DIR}/progress.txt"
        config.progress_file = tmp_path / "docs" / "custom-progress.md"
        assert config.component_progress_file(
            f"{FEATURE_DIR}/prd.json", tmp_path,
        ) == "docs/custom-progress.md"

    def test_run_factory_honors_a_directly_constructed_config(
        self, tmp_path: Path,
    ) -> None:
        """The end-to-end shape of the regression: no toml, no env, one
        constructor argument, and the worker must be told THAT path."""
        _setup_project(tmp_path, [COMPONENT_ID])
        captured: list[tuple[Any, ...]] = []

        def fake_component(*args: Any, **kwargs: Any) -> ComponentResult:
            captured.append(args)
            return ComponentResult(
                COMPONENT_ID, success=True, iterations=1,
                duration_seconds=1.0,
            )

        base = _base_config(tmp_path)
        base.progress_file = Path("docs/custom-progress.md")

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
                base,
                PlainUI(no_color=True, file=io.StringIO()),
                tmp_path,
            )

        assert captured, "worker was never invoked"
        assert "docs/custom-progress.md" in captured[0]

    def test_the_standalone_loop_still_gets_a_concrete_path(
        self, tmp_path: Path,
    ) -> None:
        """The sentinel must not leak "None" into the engineer prompt:
        the loop resolves it against the run root."""
        assert KstrlConfig().resolved_progress_file(tmp_path) == (
            tmp_path / "scripts/kstrl/progress.txt"
        )
        # An absolute explicit setting is returned untouched (the factory
        # worker's case); a relative one is anchored to the root.
        assert KstrlConfig(
            progress_file=tmp_path / "wt" / "p.txt",
        ).resolved_progress_file(tmp_path) == tmp_path / "wt" / "p.txt"
        assert KstrlConfig(
            progress_file=Path("docs/p.md"),
        ).resolved_progress_file(tmp_path) == tmp_path / "docs" / "p.md"


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
            prefix: str, *artifacts: str, **kwargs: Any,
        ) -> dict[str, int]:
            # Mirrors the real signature: the progress log is a plain
            # artifact, the diff arrives as `diff=` so it can be
            # reduced to added lines.
            seen.extend(artifacts)
            return {"injected": 1, "referenced": 1}

        pipeline = _pipeline(
            tmp_path, comp, wt_path,
            measure_fact_utilization=fake_measure,
            knowledge_prefix="FACT: fact-alpha",
        )

        # Measurement moved off the distill phase to the diff phase
        # (#191 follow-up) so components that fail review are sampled
        # too; the progress path it resolves is unchanged.
        pipeline.record_fact_utilization(comp, wt_path, "diff")

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


# ---------------------------------------------------------------------------
# Review finding 3: the writer and the reader must be reconciled in ONE
# path domain, and the proof is the RUNTIME paths, not the strings.
# ---------------------------------------------------------------------------


_CUSTOM_PROGRESS = "docs/custom-progress.md"


def _worker_write_path(
    base_config: KstrlConfig, root: Path, wt_path: Path, prd_rel: str,
) -> Path:
    """The file the engineer's worker actually writes.

    Both steps are the production ones: ``component_progress_file`` is
    what ``factory._submit_args`` passes down, and ``_run_component``
    turns that into the ``KstrlConfig.progress_file`` the loop hands to
    the agent as ``$progress_path``.
    """
    from kstrl.factory import _run_component
    from kstrl.loop import LoopResult

    seen: list[KstrlConfig] = []

    def fake_run_loop(
        config: KstrlConfig, *args: Any, **kwargs: Any,
    ) -> LoopResult:
        seen.append(config)
        return LoopResult(
            completed=True, iterations=1, exit_code=0, duration_seconds=0.0,
        )

    with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
        _run_component(
            component_id=COMPONENT_ID,
            prd_path_str=prd_rel,
            worktree_path_str=str(wt_path),
            root_dir_str=str(root),
            prompt_file_str="scripts/kstrl/prompt.md",
            agent_cmd="echo test",
            model=None, reasoning=None, agent_type=None,
            sleep_seconds=0.0,
            progress_file_str=base_config.component_progress_file(
                prd_rel, root,
            ),
            redirect_output=False,
        )

    assert seen, "run_loop was never called"
    assert seen[0].progress_file is not None
    return seen[0].progress_file


class TestWriterAndReaderResolveToTheSameFILE:
    """Review finding 3: reconciling the raw values compared an ABSOLUTE
    writer (``KstrlConfig.load`` resolves ``[paths] progress`` against the
    main checkout) with a verbatim-relative reader. The reconciled
    STRINGS came out equal while the runtime paths did not - the worker
    wrote ``<worktree>/docs/custom-progress.md`` and the self-critique
    check read ``<root>/docs/custom-progress.md``.

    These tests compare the paths, never the strings: the writer path is
    the one ``_run_component`` hands the loop, and the reader is the real
    ``run_mechanical_verification``.
    """

    def _project(self, tmp_path: Path, toml: str) -> tuple[Path, Path, str]:
        root = tmp_path / "root"
        root.mkdir()
        _setup_project(root, [COMPONENT_ID])
        (root / "kstrl.toml").write_text(toml)
        wt_path = tmp_path / "wt"
        prd_rel = f"{FEATURE_DIR}/prd.json"
        (wt_path / FEATURE_DIR).mkdir(parents=True)
        (wt_path / prd_rel).write_text(json.dumps({
            "branchName": "test", "userStories": [],
        }))
        return root, wt_path, prd_rel

    def _reconciled(self, root: Path) -> tuple[KstrlConfig, VerifyConfig]:
        """The factory command's wiring, verbatim."""
        from kstrl.config import reconcile_progress_config

        base_config = KstrlConfig.load(root)
        v_config = VerifyConfig.load(root)
        v_config.require_self_critique = True
        v_config.test_command = "true"
        v_config.typecheck_command = "true"
        v_config.lint_command = "true"
        v_config.check_diff_scope = False
        v_config.check_bad_patterns = False
        v_config.subprocess_timeout = 10.0
        assert reconcile_progress_config(base_config, v_config, root) is None
        return base_config, v_config

    def test_writer_path_is_what_the_verification_reads(
        self, tmp_path: Path,
    ) -> None:
        """[paths] progress set alone: the file the worker writes is the
        file Phase 1 reads, and it is inside the WORKTREE."""
        root, wt_path, prd_rel = self._project(
            tmp_path, f'[paths]\nprogress = "{_CUSTOM_PROGRESS}"\n',
        )
        base_config, v_config = self._reconciled(root)

        write_path = _worker_write_path(base_config, root, wt_path, prd_rel)
        assert write_path == wt_path / _CUSTOM_PROGRESS, (
            "the worker writes inside its worktree; a reader anchored "
            "anywhere else inspects a file that does not exist"
        )

        # The engineer writes its entry EXACTLY where it was told to.
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(_SELF_CRITIQUE_ENTRY)
        # A stale copy in the main checkout must not rescue the check.
        (root / "docs").mkdir(parents=True, exist_ok=True)
        (root / _CUSTOM_PROGRESS).write_text("stale root copy, no critique\n")

        result = run_mechanical_verification(
            wt_path, wt_path / prd_rel, "main", None, v_config,
        )
        check = _self_critique_check(result)
        assert check.passed, check.message

    def test_reader_only_configuration_agrees_too(
        self, tmp_path: Path,
    ) -> None:
        """The symmetric case: [verify] progress_file_path set alone
        propagates to the writer in the same domain."""
        root, wt_path, prd_rel = self._project(
            tmp_path,
            f'[verify]\nprogress_file_path = "{_CUSTOM_PROGRESS}"\n',
        )
        base_config, v_config = self._reconciled(root)

        write_path = _worker_write_path(base_config, root, wt_path, prd_rel)
        assert write_path == wt_path / _CUSTOM_PROGRESS

        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(_SELF_CRITIQUE_ENTRY)
        result = run_mechanical_verification(
            wt_path, wt_path / prd_rel, "main", None, v_config,
        )
        assert _self_critique_check(result).passed

    def test_both_set_differently_warns_and_overrides_neither(
        self, tmp_path: Path,
    ) -> None:
        root, _wt_path, _prd_rel = self._project(
            tmp_path,
            '[paths]\nprogress = "docs/writer.md"\n'
            '[verify]\nprogress_file_path = "docs/reader.md"\n',
        )
        from kstrl.config import reconcile_progress_config

        base_config = KstrlConfig.load(root)
        v_config = VerifyConfig.load(root)
        warning = reconcile_progress_config(base_config, v_config, root)
        assert warning is not None
        assert "docs/writer.md" in warning
        assert "docs/reader.md" in warning
        assert base_config.progress_file == Path("docs/writer.md")
        assert v_config.progress_file_path == "docs/reader.md"


# ---------------------------------------------------------------------------
# Review finding 1: the GENERATED docs must not ship the defective key
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


def _generated_config_reference() -> str:
    """The toml block README.md ships between the config-reference
    markers - the text an operator copies into their kstrl.toml."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    body = text.split("<!-- BEGIN GENERATED: config-reference -->", 1)[1]
    body = body.split("<!-- END GENERATED: config-reference -->", 1)[0]
    return body.split("```toml", 1)[1].split("```", 1)[0]


class TestShippedConfigsKeepTheProgressLogInScope:
    """Review finding 1: ``README.md`` (GENERATED by scripts/gen_docs.py)
    emitted a LIVE ``progress = "scripts/kstrl/progress.txt"`` line.
    Copying it recreated the exact defect this PR fixes - the resolved
    progress path leaves the component's allowedPaths and Phase 1
    diff_scope fails the component. Commenting the key out of
    kstrl.toml.example alone was insufficient: the generator regenerates
    the active block.
    """

    @pytest.mark.parametrize(
        "source", ["readme", "example"],
    )
    def test_a_copied_config_keeps_the_log_inside_component_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
    ) -> None:
        monkeypatch.delenv("PROGRESS_FILE", raising=False)
        toml_text = (
            _generated_config_reference() if source == "readme"
            else (REPO_ROOT / "kstrl.toml.example").read_text(encoding="utf-8")
        )
        (tmp_path / "kstrl.toml").write_text(toml_text, encoding="utf-8")

        config = KstrlConfig.load(tmp_path)
        prd_rel = f"{FEATURE_DIR}/prd.json"
        progress_rel = config.component_progress_file(prd_rel, tmp_path)

        assert path_is_allowed(progress_rel, ARCHITECT_ALLOWED_PATHS), (
            f"a config copied from {source} resolves the progress log to "
            f"{progress_rel}, outside the component's allowedPaths "
            f"{ARCHITECT_ALLOWED_PATHS}; Phase 1 diff_scope would fail "
            "the component"
        )
        assert progress_rel == f"{FEATURE_DIR}/progress.txt"

    def test_the_generated_line_is_inert(self) -> None:
        """The rendering is `progress = ""`, i.e. unset. The value is
        ignored by the loader, so the documented line cannot reintroduce
        a forced repo-root path."""
        block = _generated_config_reference()
        assert 'progress = ""' in block
        assert 'progress = "scripts/kstrl/progress.txt"' not in block

    def test_config_show_reports_the_setting_as_unset(
        self, tmp_path: Path,
    ) -> None:
        """`ks config show` reported <root>/scripts/kstrl/progress.txt as
        the effective default - the obsolete path, presented as fact."""
        from kstrl.config_report import build_config_report

        report = build_config_report(tmp_path)
        rows = [
            r for r in report.rows
            if r.section == "paths" and r.key == "progress"
        ]
        assert len(rows) == 1
        assert rows[0].source == "default"
        assert "scripts/kstrl/progress.txt" not in rows[0].value
        assert "unset" in rows[0].value

    def test_config_show_still_reports_a_configured_path(
        self, tmp_path: Path,
    ) -> None:
        """The unset rendering must not swallow a real setting."""
        from kstrl.config_report import build_config_report

        (tmp_path / "kstrl.toml").write_text(
            f'[paths]\nprogress = "{_CUSTOM_PROGRESS}"\n'
        )
        report = build_config_report(tmp_path)
        row = next(
            r for r in report.rows
            if r.section == "paths" and r.key == "progress"
        )
        assert row.source == "toml"
        assert row.value.endswith(_CUSTOM_PROGRESS)


# ---------------------------------------------------------------------------
# Review finding 4: the in-loop guard needs a per-worker change baseline
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True,
    )


def _git_repo(root: Path) -> None:
    """A repo whose scaffolding is already COMMITTED, so the only later
    changes are the ones a test makes."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True, capture_output=True,
    )
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "existing.py").write_text("x = 1\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


def _guard_config(root: Path, *, interactive: bool = False) -> KstrlConfig:
    return KstrlConfig(
        max_iterations=1,
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        interactive=interactive,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        allowed_paths=["src/"],
    )


class _ScriptedChannel:
    """An InteractionChannel that always answers with one fixed choice."""

    def __init__(self, choice: int) -> None:
        self.choice = choice

    def can_prompt(self) -> bool:
        return True

    def request(self, req: PromptRequest) -> PromptResponse:
        return PromptResponse(
            request_id=req.request_id, choice=self.choice, answered=True,
        )


class _CommittingRogueAgent:
    """The engineer as the prompt actually instructs it: do the work,
    then COMMIT. Here the work is out of scope."""

    def __init__(self, repo: Path, rel_path: str) -> None:
        self._repo = repo
        self._rel_path = rel_path
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "committing-rogue"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        target = self._repo / self._rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("out of scope\n")
        _git("add", "-A", cwd=self._repo)
        _git("commit", "-q", "-m", "story 1", cwd=self._repo)
        yield "committed story 1"

    @property
    def final_message(self) -> str | None:
        return self._final_message


class TestGuardSeesCommittedChanges:
    """Review finding 4, reproduced verbatim: the engineer COMMITS after
    every story, and ``git.get_changed_files`` reports only staged,
    unstaged and untracked files. The tripwire was therefore bypassed
    exactly when it mattered - after the agent had done (and paid for)
    the most work.
    """

    def test_the_blind_spot_is_real(self, tmp_path: Path) -> None:
        """The premise, asserted rather than assumed: once committed, an
        out-of-scope file is invisible to the old change source."""
        _git_repo(tmp_path)
        (tmp_path / "OUTSIDE.md").write_text("agent wrote this\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "story 1", cwd=tmp_path)

        assert git.get_changed_files(tmp_path) == set()

    def test_a_committed_out_of_scope_edit_is_caught(
        self, tmp_path: Path,
    ) -> None:
        _git_repo(tmp_path)
        baseline = git.capture_workspace_baseline(tmp_path)

        (tmp_path / "OUTSIDE.md").write_text("agent wrote this\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "story 1", cwd=tmp_path)

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
            baseline=baseline,
        )
        assert not ok
        assert violations == ["OUTSIDE.md"]

    def test_a_committed_in_scope_edit_is_not_a_violation(
        self, tmp_path: Path,
    ) -> None:
        """The guard must not simply flag everything committed."""
        _git_repo(tmp_path)
        baseline = git.capture_workspace_baseline(tmp_path)

        (tmp_path / "src" / "new.py").write_text("y = 2\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "story 1", cwd=tmp_path)

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
            baseline=baseline,
        )
        assert ok
        assert violations == []

    def test_no_baseline_keeps_the_historical_semantics(
        self, tmp_path: Path,
    ) -> None:
        """Backwards compatibility, stated explicitly: a caller that
        supplies no baseline gets the index+worktree view it always got,
        blind spot included. Every in-harness caller now supplies one."""
        _git_repo(tmp_path)
        (tmp_path / "OUTSIDE.md").write_text("agent wrote this\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "story 1", cwd=tmp_path)

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
        )
        assert ok
        assert violations == []

    def test_the_loop_catches_a_committing_agent(
        self, tmp_path: Path,
    ) -> None:
        """End to end through run_loop, which is where the baseline is
        captured: an agent that commits out of scope halts the loop and
        the violated file reaches the caller."""
        _git_repo(tmp_path)
        result = run_loop(
            _guard_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            _CommittingRogueAgent(tmp_path, "OUTSIDE.md"),
            tmp_path,
        )
        assert result.completed is False
        assert result.exit_code == 1
        assert result.guard_violations == ("OUTSIDE.md",)


class TestGuardIgnoresPreExistingDirt:
    """The converse failure: in a --no-worktrees run the operator's own
    uncommitted file was reported as the AGENT's violation, and
    "Revert and continue" would have destroyed it."""

    def _dirty_operator_file(self, tmp_path: Path) -> None:
        _git_repo(tmp_path)
        (tmp_path / "operator-notes.txt").write_text("my notes\n")

    def test_pre_existing_dirt_is_not_attributed_to_the_agent(
        self, tmp_path: Path,
    ) -> None:
        self._dirty_operator_file(tmp_path)
        baseline = git.capture_workspace_baseline(tmp_path)
        assert "operator-notes.txt" in baseline.dirty

        # The agent then works strictly in scope.
        (tmp_path / "src" / "new.py").write_text("y = 2\n")

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
            baseline=baseline,
        )
        assert ok
        assert violations == []

    def test_without_a_baseline_the_operator_is_blamed(
        self, tmp_path: Path,
    ) -> None:
        """The false positive being fixed, pinned so the contrast is not
        a claim."""
        self._dirty_operator_file(tmp_path)

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
        )
        assert not ok
        assert violations == ["operator-notes.txt"]

    def test_the_loop_leaves_operator_work_alone(
        self, tmp_path: Path,
    ) -> None:
        """Interactive "Revert and continue" must not reach a file the
        agent never touched - it is not in the violation list at all."""
        self._dirty_operator_file(tmp_path)
        result = run_loop(
            _guard_config(tmp_path, interactive=True),
            PlainUI(no_color=True, file=io.StringIO()),
            _CommittingRogueAgent(tmp_path, "src/in_scope.py"),
            tmp_path,
            interaction=_ScriptedChannel(1),
        )
        assert result.guard_violations == ()
        assert (tmp_path / "operator-notes.txt").read_text() == "my notes\n"


class TestRevertUndoesCommittedViolations:
    """A baseline-aware guard can be reverting a COMMITTED change, which
    ``git restore`` from the INDEX would report as reverted while leaving
    the file exactly as the agent left it."""

    def test_a_committed_new_file_is_removed(self, tmp_path: Path) -> None:
        _git_repo(tmp_path)
        baseline = git.capture_workspace_baseline(tmp_path)
        (tmp_path / "OUTSIDE.md").write_text("agent wrote this\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "story 1", cwd=tmp_path)

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path, interactive=True),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
            interaction=_ScriptedChannel(1),
            baseline=baseline,
        )
        assert ok
        assert violations == []
        assert not (tmp_path / "OUTSIDE.md").exists()
        # And the delta against the baseline no longer carries it, so the
        # next iteration does not re-detect the same violation.
        assert "OUTSIDE.md" not in git.get_changed_files_since(
            baseline, tmp_path,
        )

    def test_a_committed_edit_to_a_tracked_file_is_rolled_back(
        self, tmp_path: Path,
    ) -> None:
        _git_repo(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "notes.md").write_text("original\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "docs", cwd=tmp_path)
        baseline = git.capture_workspace_baseline(tmp_path)

        (tmp_path / "docs" / "notes.md").write_text("agent rewrote this\n")
        _git("add", "-A", cwd=tmp_path)
        _git("commit", "-q", "-m", "story 1", cwd=tmp_path)

        ok, violations = enforce_allowed_paths(
            _guard_config(tmp_path, interactive=True),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
            interaction=_ScriptedChannel(1),
            baseline=baseline,
        )
        assert ok
        assert violations == []
        assert (tmp_path / "docs" / "notes.md").read_text() == "original\n"
        assert "docs/notes.md" not in git.get_changed_files_since(
            baseline, tmp_path,
        )


# ---------------------------------------------------------------------------
# Review finding 5: an unreadable PRD must not abort scheduling or Phase 1
# ---------------------------------------------------------------------------


class TestUnreadablePrdIsCaught:
    """``_component_scope`` caught only (FileNotFoundError, ValueError).
    A prd.json that is a DIRECTORY raises IsADirectoryError and an
    unreadable one PermissionError - both OSError subclasses that escaped
    and aborted SCHEDULING, before Phase 1 ever ran."""

    PRD_REL = "scripts/kstrl/feature/comp-a/prd.json"

    def _component(self) -> Component:
        return Component("comp-a", "A", "D", [], self.PRD_REL, "kstrl/comp-a")

    def test_a_prd_that_is_a_directory_falls_back(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / self.PRD_REL).mkdir(parents=True)
        base = KstrlConfig(allowed_paths=["fallback/"])
        assert _component_scope(
            self._component(), tmp_path, base,
        ) == ["fallback/"]

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses file permissions",
    )
    def test_an_unreadable_prd_falls_back(self, tmp_path: Path) -> None:
        prd = tmp_path / self.PRD_REL
        prd.parent.mkdir(parents=True)
        prd.write_text('{"branchName": "b", "userStories": []}')
        prd.chmod(0o000)
        try:
            base = KstrlConfig(allowed_paths=["fallback/"])
            assert _component_scope(
                self._component(), tmp_path, base,
            ) == ["fallback/"]
        finally:
            prd.chmod(0o644)

    def test_phase_1_turns_a_directory_prd_into_a_verification_result(
        self, tmp_path: Path,
    ) -> None:
        """Fail-closed only works if the failure is CAUGHT: Phase 1 must
        hand check_diff_scope an allowed_paths_error, not raise."""
        comp = self._component()
        wt_path = tmp_path / "wt"
        (wt_path / self.PRD_REL).mkdir(parents=True)
        seen: list[str | None] = []

        def fake_verify(*args: Any, **kwargs: Any) -> VerificationResult:
            seen.append(kwargs.get("allowed_paths_error"))
            return VerificationResult(passed=False, checks=[])

        pipeline = _pipeline(
            tmp_path, comp, wt_path, run_mechanical_verification=fake_verify,
        )
        result = pipeline._phase_verify(
            comp,
            ComponentResult(
                comp.id, success=True, iterations=1, duration_seconds=1.0,
            ),
            wt_path,
        )
        assert result.ran
        assert seen and seen[0] is not None
        assert "PRD could not be read" in seen[0]
