"""Phase E: architectural refinements (subset E2/E4/E5/E6/E9).

E3 (structured findings) and E8 (fact scope by import surface) are
deferred to follow-up PRs - see docs/adversarial-roadmap.md for the
rationale and exact follow-up scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ralph_py.config import RalphConfig
from ralph_py.factory import ComponentResult, FactoryConfig, run_factory
from ralph_py.git import strip_self_critique_from_diff
from ralph_py.knowledge import _coerce_facts, _parse_fact_md
from ralph_py.manifest import Component, Manifest
from ralph_py.review import ReviewResult, parse_review_output
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import VerifyConfig

# ---------------------------------------------------------------------------
# E5 - confidence rename + backwards compat
# ---------------------------------------------------------------------------


class TestE5ConfidenceRename:
    def test_new_review_passed_accepted(self) -> None:
        raw = [{
            "id": "fact-001", "scope": "handler",
            "confidence": "review_passed",
            "evidence": ["x:1"], "claim": "ok",
        }]
        facts = _coerce_facts(raw, "c", 1, "r", 7)
        assert len(facts) == 1
        assert facts[0].confidence == "review_passed"

    def test_test_verified_tier_accepted(self) -> None:
        raw = [{
            "id": "fact-001", "scope": "handler",
            "confidence": "test_verified",
            "evidence": ["x:1"], "claim": "ok",
        }]
        facts = _coerce_facts(raw, "c", 1, "r", 7)
        assert facts[0].confidence == "test_verified"

    def test_legacy_verified_value_maps_on_read(self, tmp_path: Path) -> None:
        """An old fact file with confidence=verified must still load,
        with the value rewritten to review_passed on read."""
        legacy = (
            "---\n"
            '{"id":"fact-001","component_id":"x","created_iter":1,'
            '"created_run_id":"factory-20260101-120000-aaaaaa",'
            '"scope":"handler","evidence":["x:1"],'
            '"confidence":"verified","tags":[]}\n'
            "---\n\n"
            "Legacy fact body.\n"
        )
        fact = _parse_fact_md(legacy)
        assert fact.confidence == "review_passed"


# ---------------------------------------------------------------------------
# E4 - LLM budget cap
# ---------------------------------------------------------------------------


class TestE4BudgetCap:
    def _scaffold(self, tmp_path: Path, comp_id: str) -> Path:
        (tmp_path / "scripts" / "ralph").mkdir(parents=True)
        (tmp_path / "scripts" / "ralph" / "prompt.md").write_text("p")
        (tmp_path / "scripts" / "ralph" / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )
        feature_dir = tmp_path / "scripts" / "ralph" / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "t",
            "userStories": [{
                "id": "US-1", "title": "t", "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
        return tmp_path

    def _make_manifest(self, ids: list[str]) -> Manifest:
        return Manifest(
            version="1", spec_file="s", project_name="t",
            base_branch="main", single_pr=False,
            components=[
                Component(
                    id=i, title=i, description="", dependencies=[],
                    prd_path=f"scripts/ralph/feature/{i}/prd.json",
                    branch_name=f"ralph/{i}",
                )
                for i in ids
            ],
        )

    def _base_config(self, root: Path) -> RalphConfig:
        return RalphConfig(
            prompt_file=root / "scripts/ralph/prompt.md",
            prd_file=root / "scripts/ralph/prd.json",
            sleep_seconds=0, agent_cmd="echo test",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )

    def test_budget_zero_means_unbounded(self, tmp_path: Path) -> None:
        """max_adversarial_calls=0 is the default and must not change
        the existing review/security behavior."""
        root = self._scaffold(tmp_path, "comp-a")
        manifest = self._make_manifest(["comp-a"])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="hard", max_adversarial_calls=0,
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        passing_review = ReviewResult(passed=True, mode="hard")
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.run_review", return_value=passing_review,
        ) as mock_review, patch(
            "ralph_py.git.get_diff_content", return_value="",
        ):
            run_factory(
                manifest, config, self._base_config(root),
                PlainUI(no_color=True), root,
            )
        # Unbounded budget means review fires
        assert mock_review.called

    def test_budget_one_skips_second_component_review(
        self, tmp_path: Path,
    ) -> None:
        root = self._scaffold(tmp_path, "comp-a")
        feature_b = root / "scripts/ralph/feature/comp-b"
        feature_b.mkdir(parents=True)
        (feature_b / "prd.json").write_text(json.dumps({
            "branchName": "t",
            "userStories": [{
                "id": "US-B", "title": "t", "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
        manifest = self._make_manifest(["comp-a", "comp-b"])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="hard", max_adversarial_calls=1,
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        passing_review = ReviewResult(passed=True, mode="hard")
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.factory.run_review", return_value=passing_review,
        ) as mock_review, patch(
            "ralph_py.git.get_diff_content", return_value="",
        ):
            run_factory(
                manifest, config, self._base_config(root),
                PlainUI(no_color=True), root,
            )
        # Budget=1; second component's review is budget-skipped
        assert mock_review.call_count == 1


# ---------------------------------------------------------------------------
# E6 - HITL checkpoint (non-interactive path)
# ---------------------------------------------------------------------------


class TestE6HitlCheckpoint:
    def test_non_interactive_ui_warns_and_proceeds(self, tmp_path: Path) -> None:
        """When pause_before_pr_merge=True but the UI can't prompt
        (PlainUI in tests), the factory must log a warning and proceed
        rather than block indefinitely."""
        from ralph_py.factory import ComponentResult
        scaffold = tmp_path / "scripts" / "ralph"
        scaffold.mkdir(parents=True)
        (scaffold / "prompt.md").write_text("p")
        (scaffold / "prd.json").write_text(
            '{"branchName": "t", "userStories": []}'
        )
        feature_dir = scaffold / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "t",
            "userStories": [{
                "id": "US-1", "title": "t", "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
        manifest = Manifest(
            version="1", spec_file="s", project_name="t",
            base_branch="main", single_pr=False,
            components=[Component(
                id="comp-a", title="A", description="", dependencies=[],
                prd_path="scripts/ralph/feature/comp-a/prd.json",
                branch_name="ralph/a",
            )],
        )
        config = FactoryConfig(
            use_worktrees=False, create_prs=True, max_parallel=1,
            review_mode="skip",
            pause_before_pr_merge=True,
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = RalphConfig(
            prompt_file=scaffold / "prompt.md",
            prd_file=scaffold / "prd.json",
            sleep_seconds=0, agent_cmd="echo test",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "ralph_py.factory._run_component", return_value=success,
        ), patch(
            "ralph_py.pr.is_gh_available", return_value=False,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, base, PlainUI(no_color=True), tmp_path,
            )
        # PlainUI returns False for can_prompt() so HITL skips and the
        # component completes (no gh available so no PR is actually
        # created, but the path executed cleanly).
        assert "comp-a" in result.completed


# ---------------------------------------------------------------------------
# E2 - strip Self-Critique from diff
# ---------------------------------------------------------------------------


class TestE2StripSelfCritique:
    def test_strips_self_critique_block(self) -> None:
        diff = """\
diff --git a/scripts/ralph/progress.txt b/scripts/ralph/progress.txt
+## Iteration 1 - US-001
+- What I did: added the function
+## Self-Critique
+- Failure mode 1: empty input crashes the parser
+- Failure mode 2: concurrent writes race
+- Failure mode 3: timeout swallowed silently
+---
+
diff --git a/src/x.py b/src/x.py
+def add(a, b): return a + b
"""
        result = strip_self_critique_from_diff(diff)
        assert "Failure mode 1" not in result
        assert "Self-Critique" not in result
        # The actual code change must survive
        assert "def add(a, b)" in result

    def test_no_block_returns_unchanged(self) -> None:
        diff = "+def f(): pass\n"
        assert strip_self_critique_from_diff(diff) == diff


# ---------------------------------------------------------------------------
# E9 - infrastructure_error on ReviewResult
# ---------------------------------------------------------------------------


class TestE9ReviewInfrastructureError:
    def test_parse_failure_sets_infrastructure_error(self) -> None:
        result = parse_review_output("not json at all")
        assert result.passed is False
        assert result.infrastructure_error is True

    def test_clean_review_has_no_infrastructure_error(self) -> None:
        result = parse_review_output(json.dumps({
            "stories": [],
            "concerns": [],
            "exhaustively_searched": True,
        }))
        assert result.passed is True
        assert result.infrastructure_error is False
