"""R1.4 truncated-diff policy tests (H-16) + security strip parity.

Head-truncating a review diff at the prompt cap let a rogue engineer pad
the first 50KB with benign churn and land a malicious hunk after the
cut: both reviewers saw a truncation notice, but hard mode had no
policy, so the unreviewed tail merged. These tests prove the mechanical
policy:

- Oversized diffs split on file boundaries into <=cap chunks; splitting
  never drops content; a single file over the cap is unsplittable and
  fails closed.
- Hard mode runs one review pass per chunk (each pass consumes the
  adversarial budget) and merges verdicts: any chunk failure fails.
- Budget that cannot cover the chunks is an infrastructure failure with
  ZERO passes run - never a partial review, never a silent skip.
- Advisory mode keeps the single truncated pass but the result is
  visibly PARTIAL (flag + injected finding + PR body annotation).
- The security reviewer no longer sees the engineer's Self-Critique
  block (E2 anti-anchoring parity), and the factory strips it once for
  both reviewers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.git import (
    DEFAULT_PROMPT_DIFF_CHAR_LIMIT,
    DiffUnsplittableError,
    split_diff_for_prompt,
)
from kstrl.manifest import Component, Manifest
from kstrl.review import (
    ReviewMode,
    ReviewResult,
    merge_review_results,
    run_chunked_review,
    run_review,
)
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    SecurityResult,
    merge_security_results,
    run_chunked_security_review,
    run_security_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig

UI = PlainUI(no_color=True)

_VERIFICATION = VerificationResult(
    passed=True, checks=[CheckResult("test_suite", True, "ok")],
)


class CountingAgent:
    """Agent that counts invocations, captures prompts, and replies with
    the output at the current call index (last output repeats)."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self.calls = 0
        self.prompts: list[str] = []
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "counting-agent"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        idx = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        self.prompts.append(prompt)
        yield from self._outputs[idx].splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


def _story(story_id: str, verdict: str) -> dict[str, object]:
    return {
        "storyId": story_id,
        "storyTitle": f"Story {story_id}",
        "criteria": [{
            "criterion": "AC1",
            "verdict": verdict,
            "explanation": "checked",
            "suggestion": "",
        }],
    }


def _review_json(verdict: str = "pass") -> str:
    return json.dumps({
        "stories": [_story("US-001", verdict)],
        "concerns": [],
        "exhaustively_searched": True,
        "overallNotes": "",
    })


_CLEAN_SECURITY_JSON = json.dumps({
    "findings": [],
    "exhaustively_searched": True,
    "overallNotes": "",
})


def _write_prd(path: Path, story_ids: list[str]) -> None:
    path.write_text(json.dumps({
        "branchName": "test",
        "userStories": [
            {
                "id": sid, "title": f"Story {sid}",
                "acceptanceCriteria": ["AC1"], "priority": 1,
                "passes": True, "notes": "",
            }
            for sid in story_ids
        ],
    }))


def _file_segment(name: str, payload_chars: int) -> str:
    """One per-file segment of a synthetic unified diff, ~payload_chars
    long."""
    header = (
        f"diff --git a/{name} b/{name}\n"
        f"--- a/{name}\n"
        f"+++ b/{name}\n"
        "@@ -0,0 +1 @@\n"
    )
    line = "+" + "x" * 98 + "\n"
    n_lines = max(1, (payload_chars - len(header)) // 100)
    return header + line * n_lines


def _synthetic_diff(n_files: int, payload_chars: int) -> str:
    return "".join(
        _file_segment(f"src/f{i}.py", payload_chars) for i in range(n_files)
    )


_SELF_CRITIQUE_DIFF = """\
diff --git a/scripts/ralph/progress.txt b/scripts/ralph/progress.txt
+## Iteration 1 - US-001
+- What I did: added the function
+## Self-Critique
+- Failure mode 1: empty input crashes the parser
+- Failure mode 2: concurrent writes race
+---
+
diff --git a/src/x.py b/src/x.py
+def add(a, b): return a + b
"""


# ---------------------------------------------------------------------------
# split_diff_for_prompt mechanics
# ---------------------------------------------------------------------------


class TestSplitDiffForPrompt:
    def test_small_diff_returned_unchanged(self) -> None:
        diff = _synthetic_diff(2, 300)
        assert split_diff_for_prompt(diff, limit=5000) == [diff]

    def test_oversized_diff_splits_on_file_boundaries(self) -> None:
        diff = _synthetic_diff(10, 300)
        chunks = split_diff_for_prompt(diff, limit=1000)
        assert len(chunks) >= 2
        for i, chunk in enumerate(chunks, 1):
            assert len(chunk) <= 1000
            assert chunk.startswith(
                f"# [ralph R1.4] diff chunk {i} of {len(chunks)}"
            )
        # Reassembly invariant: dropping each chunk's header line
        # reproduces the input exactly - chunking never loses content.
        reassembled = "".join(c.split("\n", 1)[1] for c in chunks)
        assert reassembled == diff
        # Every file boundary survives exactly once.
        for i in range(10):
            assert reassembled.count(f"diff --git a/src/f{i}.py") == 1

    def test_preamble_before_first_boundary_is_kept(self) -> None:
        diff = "binary files differ notice\n" + _synthetic_diff(6, 300)
        chunks = split_diff_for_prompt(diff, limit=1000)
        reassembled = "".join(c.split("\n", 1)[1] for c in chunks)
        assert reassembled == diff

    def test_single_oversized_file_raises(self) -> None:
        diff = _file_segment("src/big.py", 3000)
        with pytest.raises(DiffUnsplittableError, match="single-file"):
            split_diff_for_prompt(diff, limit=1000)

    def test_no_file_boundaries_raises(self) -> None:
        with pytest.raises(DiffUnsplittableError, match="no 'diff --git'"):
            split_diff_for_prompt("x" * 2000, limit=1000)

    def test_limit_must_exceed_header_reserve(self) -> None:
        with pytest.raises(ValueError, match="header"):
            split_diff_for_prompt("x", limit=100)


# ---------------------------------------------------------------------------
# merge semantics
# ---------------------------------------------------------------------------


class TestMergeResults:
    def _result(self, passed: bool, **kwargs: object) -> ReviewResult:
        return ReviewResult(passed=passed, mode="hard", **kwargs)  # type: ignore[arg-type]

    def test_all_chunks_pass_merges_to_pass(self) -> None:
        merged = merge_review_results(
            [self._result(True), self._result(True)], "hard",
        )
        assert merged.passed is True
        assert merged.infrastructure_error is False
        assert "Chunked review: 2 passes" in merged.overall_notes

    def test_any_chunk_failure_fails(self) -> None:
        merged = merge_review_results(
            [self._result(True), self._result(False)], "hard",
        )
        assert merged.passed is False

    def test_findings_concatenate(self) -> None:
        from kstrl.review import CriterionReview, ReviewConcern
        a = self._result(True)
        a.criteria.append(CriterionReview("AC1", "pass", "ok"))
        b = self._result(False)
        b.concerns.append(ReviewConcern(
            "dead_code", "fail", "x.py:1", "unused",
        ))
        merged = merge_review_results([a, b], "hard")
        assert len(merged.criteria) == 1
        assert len(merged.concerns) == 1

    def test_chunk_infra_error_marks_merged_infra(self) -> None:
        merged = merge_review_results(
            [
                self._result(True),
                self._result(False, infrastructure_error=True),
            ],
            "hard",
        )
        assert merged.infrastructure_error is True
        assert merged.passed is False

    def test_exhaustive_hint_requires_all_chunks(self) -> None:
        merged = merge_review_results(
            [
                self._result(True, exhaustively_searched=True),
                self._result(True, exhaustively_searched=False),
            ],
            "hard",
        )
        assert merged.exhaustively_searched is False

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError):
            merge_review_results([], "hard")
        with pytest.raises(ValueError):
            merge_security_results([], "hard")

    def test_security_merge_mirrors_policy(self) -> None:
        from kstrl.security import SecurityFinding
        a = SecurityResult(passed=True, mode="hard")
        a.findings.append(SecurityFinding(
            "injection", "low", "x.py:1", "meh",
        ))
        b = SecurityResult(
            passed=False, mode="hard", infrastructure_error=True,
        )
        merged = merge_security_results([a, b], "hard")
        assert merged.passed is False
        assert merged.infrastructure_error is True
        assert len(merged.findings) == 1


# ---------------------------------------------------------------------------
# chunked runners: one pass per chunk, budget rules
# ---------------------------------------------------------------------------


class TestRunChunkedReview:
    def _prd(self, tmp_path: Path) -> Path:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        return prd

    def test_one_pass_per_chunk_and_merge(self, tmp_path: Path) -> None:
        agent = CountingAgent([_review_json("pass")])
        consumed = {"n": 0}

        def consume() -> None:
            consumed["n"] += 1

        chunks = ["chunk-a", "chunk-b", "chunk-c"]
        result = run_chunked_review(
            agent, self._prd(tmp_path), tmp_path, "main",
            _VERIFICATION, ReviewMode.HARD, UI,
            diff_chunks=chunks,
            budget_remaining=3,
            consume_budget=consume,
        )
        assert agent.calls == 3
        assert consumed["n"] == 3
        assert result.passed is True
        assert len(result.criteria) == 3  # US-001 verdict per chunk
        # Each pass saw exactly its own chunk.
        for chunk, prompt in zip(chunks, agent.prompts, strict=True):
            assert chunk in prompt

    def test_failing_chunk_fails_merged_verdict(self, tmp_path: Path) -> None:
        agent = CountingAgent([
            _review_json("pass"), _review_json("fail"), _review_json("pass"),
        ])
        result = run_chunked_review(
            agent, self._prd(tmp_path), tmp_path, "main",
            _VERIFICATION, ReviewMode.HARD, UI,
            diff_chunks=["a", "b", "c"],
        )
        assert agent.calls == 3
        assert result.passed is False
        assert result.infrastructure_error is False

    def test_insufficient_budget_is_infra_fail_with_zero_passes(
        self, tmp_path: Path,
    ) -> None:
        agent = CountingAgent([_review_json("pass")])
        result = run_chunked_review(
            agent, self._prd(tmp_path), tmp_path, "main",
            _VERIFICATION, ReviewMode.HARD, UI,
            diff_chunks=["a", "b", "c"],
            budget_remaining=2,
        )
        assert agent.calls == 0
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "refusing" in result.overall_notes

    def test_security_chunked_runner_mirrors(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        result = run_chunked_security_review(
            agent, prd, tmp_path, "main", config, UI,
            diff_chunks=["a", "b"],
            budget_remaining=2,
        )
        assert agent.calls == 2
        assert result.passed is True

        starved = CountingAgent([_CLEAN_SECURITY_JSON])
        result = run_chunked_security_review(
            starved, prd, tmp_path, "main", config, UI,
            diff_chunks=["a", "b"],
            budget_remaining=1,
        )
        assert starved.calls == 0
        assert result.passed is False
        assert result.infrastructure_error is True


# ---------------------------------------------------------------------------
# advisory mode: single truncated pass, visibly PARTIAL
# ---------------------------------------------------------------------------


class TestAdvisoryPartial:
    def _oversized(self) -> str:
        return _synthetic_diff(3, DEFAULT_PROMPT_DIFF_CHAR_LIMIT // 2)

    def test_advisory_review_is_marked_partial(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        result = run_review(
            agent, prd, tmp_path, "main", _VERIFICATION,
            ReviewMode.ADVISORY, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is True
        assert result.partial is True
        partial_concerns = [
            c for c in result.concerns if "Partial review" in c.explanation
        ]
        assert len(partial_concerns) == 1
        assert partial_concerns[0].severity == "advisory"
        assert "PARTIAL REVIEW" in result.as_pr_body_section()

    def test_fitting_diff_is_not_partial(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        result = run_review(
            agent, prd, tmp_path, "main", _VERIFICATION,
            ReviewMode.ADVISORY, UI,
            diff_content="+small diff\n",
        )
        assert result.partial is False
        assert "PARTIAL" not in result.as_pr_body_section()

    def test_hard_review_backstop_fails_closed(self, tmp_path: Path) -> None:
        """Direct hard-mode call with an unchunked oversized diff must
        never pass (the factory chunks; this guards other callers)."""
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        result = run_review(
            agent, prd, tmp_path, "main", _VERIFICATION,
            ReviewMode.HARD, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "without chunking" in result.overall_notes

    def test_advisory_security_is_marked_partial(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        result = run_security_review(
            agent, prd, tmp_path, "main", config, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is True
        assert result.partial is True
        markers = [
            f for f in result.findings
            if "Partial security review" in f.explanation
        ]
        assert len(markers) == 1
        assert markers[0].severity == "low"
        assert "PARTIAL SECURITY REVIEW" in result.as_pr_body_section()

    def test_hard_security_backstop_fails_closed(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.HARD.value)
        result = run_security_review(
            agent, prd, tmp_path, "main", config, UI,
            diff_content=self._oversized(),
        )
        assert result.passed is False
        assert result.infrastructure_error is True
        assert "without chunking" in result.overall_notes


# ---------------------------------------------------------------------------
# security parity: the Self-Critique block never reaches either prompt
# ---------------------------------------------------------------------------


class TestSelfCritiqueStripParity:
    def test_security_prompt_contains_no_self_critique(
        self, tmp_path: Path,
    ) -> None:
        """String-level proof on the BUILT prompt via the fetch-fallback
        path (diff_content=None)."""
        prd = tmp_path / "prd.json"
        prd.write_text("spec")
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        config = SecurityConfig(mode=SecurityMode.ADVISORY.value)
        with patch(
            "kstrl.git.get_diff_content",
            return_value=_SELF_CRITIQUE_DIFF,
        ):
            run_security_review(agent, prd, tmp_path, "main", config, UI)
        assert len(agent.prompts) == 1
        assert "Self-Critique" not in agent.prompts[0]
        assert "Failure mode 1" not in agent.prompts[0]
        # The actual code change still reaches the reviewer.
        assert "def add(a, b)" in agent.prompts[0]

    def test_review_prompt_fallback_still_strips(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.json"
        _write_prd(prd, ["US-001"])
        agent = CountingAgent([_review_json("pass")])
        with patch(
            "kstrl.git.get_diff_content",
            return_value=_SELF_CRITIQUE_DIFF,
        ):
            run_review(
                agent, prd, tmp_path, "main", _VERIFICATION,
                ReviewMode.ADVISORY, UI,
            )
        assert len(agent.prompts) == 1
        assert "Self-Critique" not in agent.prompts[0]
        assert "def add(a, b)" in agent.prompts[0]


# ---------------------------------------------------------------------------
# factory wiring: chunk orchestration, budget accounting, strip-once
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, comp_ids: list[str]) -> Path:
    (tmp_path / "scripts" / "ralph").mkdir(parents=True)
    (tmp_path / "scripts" / "ralph" / "prompt.md").write_text("p")
    (tmp_path / "scripts" / "ralph" / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    for comp_id in comp_ids:
        feature_dir = tmp_path / "scripts" / "ralph" / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        _write_prd(feature_dir / "prd.json", ["US-001"])
    return tmp_path


def _make_manifest(ids: list[str]) -> Manifest:
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


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts/ralph/prompt.md",
        prd_file=root / "scripts/ralph/prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _factory_config(**overrides: object) -> FactoryConfig:
    defaults: dict[str, object] = dict(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)  # type: ignore[arg-type]


def _read_events(log_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# Three ~40KB files: over the 50KB cap, and each file fits a chunk, so
# greedy packing yields exactly 3 chunks of one file each.
def _oversized_component_diff() -> str:
    return _synthetic_diff(3, 40_000)


class TestFactoryChunkedReview:
    def test_hard_mode_oversized_diff_runs_one_pass_per_chunk(
        self, tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="hard", max_adversarial_calls=3,
            progress_log_path=log_path,
        )
        agent = CountingAgent([_review_json("pass")])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_oversized_component_diff(),
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        assert agent.calls == 3
        events = _read_events(log_path)
        chunk_events = [e for e in events if e["event"] == "diff_chunked"]
        assert len(chunk_events) == 1
        assert chunk_events[0]["data"]["chunks"] == 3  # type: ignore[index]

    def test_insufficient_budget_fails_component_without_retry(
        self, tmp_path: Path,
    ) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        # 3 chunks needed, budget 2; retries available but must NOT be
        # used - the budget can only shrink, so retrying is pure waste.
        config = _factory_config(
            review_mode="hard", max_adversarial_calls=2, max_retries=2,
            progress_log_path=log_path,
        )
        agent = CountingAgent([_review_json("pass")])
        run_component_calls = {"n": 0}

        def fake_run_component(
            comp_id: str, *a: object, **k: object,
        ) -> ComponentResult:
            run_component_calls["n"] += 1
            return ComponentResult(comp_id, success=True, iterations=1)

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_oversized_component_diff(),
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.failed
        assert agent.calls == 0
        assert run_component_calls["n"] == 1
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.error is not None
        assert "Review infrastructure error" in comp.error
        infra = [
            f for f in comp.findings
            if f.is_infrastructure_error and f.phase == "review"
        ]
        assert len(infra) == 1
        assert "refusing" in infra[0].explanation
        events = _read_events(log_path)
        assert any(
            e["event"] == "chunk_budget_insufficient" for e in events
        )

    def test_security_hard_mode_chunks_too(self, tmp_path: Path) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            review_mode="skip", max_adversarial_calls=3,
            security_config=SecurityConfig(mode=SecurityMode.HARD.value),
        )
        agent = CountingAgent([_CLEAN_SECURITY_JSON])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_oversized_component_diff(),
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        assert agent.calls == 3

    def test_unsplittable_diff_fails_closed(self, tmp_path: Path) -> None:
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        log_path = tmp_path / "progress.jsonl"
        config = _factory_config(
            review_mode="hard", progress_log_path=log_path,
        )
        # One file bigger than the cap: no file boundary to split on.
        big_single_file = _file_segment(
            "src/huge.py", DEFAULT_PROMPT_DIFF_CHAR_LIMIT + 10_000,
        )
        agent = CountingAgent([_review_json("pass")])
        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content", return_value=big_single_file,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.failed
        assert agent.calls == 0
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.error is not None
        assert "unsplittable" in comp.error
        events = _read_events(log_path)
        assert any(e["event"] == "diff_unsplittable" for e in events)


class TestSinglePassSecurityBudget:
    def test_single_pass_security_consumes_exactly_one_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: the R1.4 refactor briefly double-consumed the
        budget for non-chunked security passes, which would silently
        halve the effective budget. Two components + budget 2 must both
        get their security pass."""
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")
        root = _scaffold(tmp_path, ["comp-a", "comp-b"])
        manifest = _make_manifest(["comp-a", "comp-b"])
        config = _factory_config(
            review_mode="skip", max_adversarial_calls=2,
            security_config=SecurityConfig(
                mode=SecurityMode.ADVISORY.value,
            ),
        )
        agent = CountingAgent([_CLEAN_SECURITY_JSON])

        def fake_run_component(
            comp_id: str, *a: object, **k: object,
        ) -> ComponentResult:
            return ComponentResult(comp_id, success=True, iterations=1)

        with patch(
            "kstrl.factory._run_component", side_effect=fake_run_component,
        ), patch(
            "kstrl.agents.get_agent", return_value=agent,
        ), patch(
            "kstrl.git.get_diff_content", return_value="+small\n",
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert set(result.completed) == {"comp-a", "comp-b"}
        assert agent.calls == 2
        for comp_id in ("comp-a", "comp-b"):
            comp = manifest.get_component(comp_id)
            assert comp is not None
            assert not any(
                f.is_phase_skip and f.phase == "security"
                for f in comp.findings
            )


class TestFactoryStripOnce:
    def test_both_reviewers_receive_stripped_diff(
        self, tmp_path: Path,
    ) -> None:
        """R1.4 requirement 3: the factory strips the Self-Critique
        block once and shares the result with Phase 2 AND Phase 2.5."""
        root = _scaffold(tmp_path, ["comp-a"])
        manifest = _make_manifest(["comp-a"])
        config = _factory_config(
            review_mode="advisory", max_adversarial_calls=2,
            security_config=SecurityConfig(
                mode=SecurityMode.ADVISORY.value,
            ),
        )
        captured: dict[str, str] = {}

        def fake_run_review(
            *args: object, **kwargs: object,
        ) -> ReviewResult:
            captured["review"] = str(kwargs.get("diff_content"))
            return ReviewResult(passed=True, mode="advisory")

        def fake_run_security(
            *args: object, **kwargs: object,
        ) -> SecurityResult:
            captured["security"] = str(kwargs.get("diff_content"))
            return SecurityResult(passed=True, mode="advisory")

        success = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "kstrl.factory._run_component", return_value=success,
        ), patch(
            "kstrl.factory.run_review", side_effect=fake_run_review,
        ), patch(
            "kstrl.factory.run_security_review",
            side_effect=fake_run_security,
        ), patch(
            "kstrl.git.get_diff_content",
            return_value=_SELF_CRITIQUE_DIFF,
        ):
            result = run_factory(
                manifest, config, _base_config(root),
                PlainUI(no_color=True), root,
            )
        assert "comp-a" in result.completed
        assert "Self-Critique" not in captured["review"]
        assert "Self-Critique" not in captured["security"]
        # The real change survives the strip for both reviewers.
        assert "def add(a, b)" in captured["review"]
        assert "def add(a, b)" in captured["security"]
