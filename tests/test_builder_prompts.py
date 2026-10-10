"""H3a (#303): enrolment guards for the 30 harness-authored fragments (24
from #303, one from #233, four from #633, one from #700) that multi-branch builders assemble into a
role's prompt. Why these fragments cannot be enrolled the way one plain
prompt is is explained once, in ``tests/helpers/builder_prompts.py``'s
module docstring. Three layers are
checked here, none of them by ``tests/test_prompt_versions.py``
alone:

1. ``test_builder_text_is_under_an_enrolled_prompt`` -- the reproduction:
   a needle sentence per site must appear in some enrolled body.
2. ``test_delivered_prompt_digest`` -- scenarios rendered through the REAL
   production functions, digested, and pinned; the only guard that would
   catch un-hoisted text living beside an enrolled fragment.
3. ``test_enrolled_fragment_reaches_its_builder`` -- the call-time
   constants: patched to a marker, the marker must reach the rendered
   text, or the constant has gone orphan.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from kstrl import (
    context,
    factory,
    init_cmd,
    iteration_prompt,
    knowledge,
    review_claims,
    verify,
    verify_diff,
    verify_model,
)
from kstrl.context import LEGACY_ATTEMPT, IterationContext, IterationRecord
from kstrl.findings import Finding
from kstrl.knowledge import KnowledgeConfig
from kstrl.loop import LoopResult
from kstrl.manifest import Component, Manifest
from kstrl.policy import PolicyConfig
from kstrl.review import CriterionReview, ReviewResult, ReviewVerdict
from kstrl.scope import ComponentScope
from tests.helpers.builder_prompts import BUILDER_PROMPTS
from tests.helpers.component_prd import write_component_prd
from tests.test_knowledge import _make_fact
from tests.test_prompt_versions import _ORPHAN_MARKER, _PROMPTS, _sha256

# ---------------------------------------------------------------------------
# Scenario renderers. Copied from the lane's capture.py (the byte-identity
# harness run before and after the #303 hoist) with three adaptations:
# tempfile.TemporaryDirectory() -> pytest's tmp_path, the `from kstrl import
# ...` imports moved to the top of this file, and main()/sys.argv dropped.
# The `_policy_diff_unreadable` filter and every other normalisation below
# are load-bearing, not tidiness: dropping the "Error: " line keeps the
# digest machine-independent, since that line carries an absolute path.
# ---------------------------------------------------------------------------


def _claude_md(_tmp: Path) -> str:
    return init_cmd._generate_claude_md("proj")


def _ctx_empty(_tmp: Path) -> str:
    return IterationContext().format_for_prompt()


def _ctx_current_only(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_verification_failure("linter: E501 line too long", attempt=1)
    return ctx.format_for_prompt()


def _ctx_acceptance(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_acceptance_failure(["- has-marker (visible): passed 0 of 1 runs -> fail"], attempt=1)
    return ctx.format_for_prompt()


def _ctx_not_remeasured_dated(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_review_finding("criterion AC1 unmet", attempt=1, phase="review")
    ctx.add_verification_failure("linter: E501", attempt=2)
    return ctx.format_for_prompt()


def _ctx_not_remeasured_legacy(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_review_finding("legacy finding", attempt=LEGACY_ATTEMPT, phase="review")
    ctx.add_verification_failure("linter: E501", attempt=2)
    return ctx.format_for_prompt()


def _ctx_resolved(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_verification_failure("linter: E501", attempt=1)
    ctx.add_review_finding("criterion AC1 unmet", attempt=2, phase="review")
    return ctx.format_for_prompt()


def _ctx_history(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.records.append(
        IterationRecord(iteration=1, success=False, error="agent exited 1", summary="", attempt=1)
    )
    ctx.records.append(
        IterationRecord(iteration=2, success=True, error="", summary="done", attempt=2)
    )
    return ctx.format_for_prompt()


def _ctx_all(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_verification_failure("linter: E501", attempt=1)
    ctx.add_review_finding("criterion AC1 unmet", attempt=1, phase="review")
    ctx.add_review_finding("sql injection", attempt=2, phase="security")
    ctx.add_engineer_failure("agent exited non-zero", attempt=3)
    ctx.add_review_finding("legacy", attempt=LEGACY_ATTEMPT, phase="review")
    ctx.records.append(
        IterationRecord(iteration=1, success=False, error="boom", summary="s", attempt=1)
    )
    return ctx.format_for_prompt()


def _finding(location: str) -> Finding:
    return Finding(
        phase="review",
        category="claim_disagreement",
        severity="major",
        location=location,
        explanation=f"{location}: marked done, pass on only 1 of 2 criteria",
    )


def _claim(reverted: bool, criteria: list[CriterionReview]) -> Callable[[Path], str]:
    def render(_tmp: Path) -> str:
        result = ReviewResult(passed=True, mode="advisory", criteria=criteria)
        return review_claims.claim_retry_context([_finding("US-001")], result, reverted=reverted)

    return render


_UNMET = [
    CriterionReview(
        criterion="AC2 is honoured",
        verdict=ReviewVerdict.FAIL.value,
        explanation="the handler never checks it",
        suggestion="add the check in handler()",
        story_id="US-001",
    ),
]
_ALL_PASSED = [
    CriterionReview(
        criterion="AC1 is honoured",
        verdict=ReviewVerdict.PASS.value,
        explanation="ok",
        story_id="US-001",
    ),
]


def _claim_empty(_tmp: Path) -> str:
    return review_claims.claim_retry_context([], ReviewResult(passed=True, mode="advisory"))


def _diff_scope_plain(_tmp: Path) -> str:
    return "\n".join(
        verify_diff._diff_scope_details("main", ["src/", "tests/"], None, ["a.py", "b.py"])
    )


def _diff_scope_harness(_tmp: Path) -> str:
    return "\n".join(
        verify_diff._diff_scope_details(
            "origin/main", ["src/", "tests/"], ["scripts/kstrl/prd.json"], ["a.py"]
        )
    )


def _diff_scope_truncated(_tmp: Path) -> str:
    return "\n".join(
        verify_diff._diff_scope_details("main", ["src/"], None, [f"f{i}.py" for i in range(20)])
    )


def _prd_tamper(tmp: Path) -> str:
    pre = tmp / "pre"
    wt = tmp / "wt"
    pre.mkdir()
    wt.mkdir()
    story = {
        "id": "US-001",
        "title": "T",
        "acceptanceCriteria": ["AC1"],
        "priority": 1,
        "passes": True,
        "notes": "",
    }
    write_component_prd(pre, "prd.json", stories=[dict(story)])
    write_component_prd(
        wt, "prd.json", stories=[{**story, "acceptanceCriteria": ["AC1 rewritten"]}]
    )
    result = verify.check_prd_stories(wt / "prd.json", pre / "prd.json")
    return "\n".join(result.details)


def _scope_unreadable_cause(_tmp: Path) -> str:
    return "\n".join(verify_diff.check_scope_unreadable("prd.json: No such file").details)


def _scope_unreadable_no_cause(_tmp: Path) -> str:
    return "\n".join(verify_diff.check_scope_unreadable("").details)


def _policy_diff_unreadable(tmp: Path) -> str:
    empty = tmp / "not-a-repo"
    empty.mkdir()
    result = verify_diff.check_policy_envelope(empty, "main", PolicyConfig(enabled=True))
    # The Error: line carries a machine-specific path; keep only the prose.
    return "\n".join(d for d in result.details if not d.startswith("Error:"))


def _loop_measurement(_tmp: Path) -> str:
    """The block the engineer loop puts in the next prompt (#233)."""
    reading = verify_model.VerificationResult(
        passed=False,
        checks=[
            verify_model.CheckResult(
                name="linter",
                passed=False,
                message="Linter failed (exit code 1)",
                details=["a.py:1:1: F401 unused import"],
            )
        ],
    )
    return iteration_prompt.measurement_block(reading)


_FACTORY_PRD_REL = "components/c1/prd.json"


def _factory_guard(tmp: Path) -> str:
    root = tmp / "proj"
    (root / "scripts" / "kstrl").mkdir(parents=True)
    (root / "scripts" / "kstrl" / "prompt.md").write_text("test prompt", encoding="utf-8")
    (root / "scripts" / "kstrl" / "prd.json").write_text(
        '{"branchName": "t", "userStories": []}', encoding="utf-8"
    )
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n", encoding="utf-8")
    (root / "components" / "c1").mkdir(parents=True)
    write_component_prd(root, _FACTORY_PRD_REL, allowed_paths=["src/", "tests/"])

    def fake_run_loop(*_args: object, **_kwargs: object) -> LoopResult:
        return LoopResult(
            completed=False,
            iterations=3,
            exit_code=1,
            duration_seconds=0.0,
            guard_violations=tuple(f"out{i}.py" for i in range(20)),
        )

    scope = ComponentScope(
        ["src/", "tests/"],
        ["components/c1/prd.json", "scripts/kstrl/progress.txt"],
        "c1",
        _FACTORY_PRD_REL,
    )
    with patch("kstrl.loop.run_loop", side_effect=fake_run_loop):
        result = factory._run_component(
            component_id="c1",
            prd_path_str=_FACTORY_PRD_REL,
            worktree_path_str=str(root),
            root_dir_str=str(root),
            prompt_file_str="scripts/kstrl/prompt.md",
            agent_cmd="echo test",
            model=None,
            reasoning=None,
            agent_type=None,
            sleep_seconds=0.0,
            scope=scope,
            redirect_output=False,
            run_id="test-run",
        )
    return result.error or "<no error>"


def _knowledge(overflow: bool) -> Callable[[Path], str]:
    def render(tmp: Path) -> str:
        root = tmp / ("k-overflow" if overflow else "k-plain")
        root.mkdir()
        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    id="c1",
                    title="T1",
                    description="D",
                    dependencies=["c2"],
                    prd_path="p1.json",
                    branch_name="b1",
                ),
                Component(
                    id="c2",
                    title="T2",
                    description="D",
                    dependencies=[],
                    prd_path="p2.json",
                    branch_name="b2",
                ),
                Component(
                    id="c3",
                    title="T3",
                    description="D",
                    dependencies=[],
                    prd_path="p3.json",
                    branch_name="b3",
                ),
            ],
        )
        claim = "the handler validates its input."
        for cid in ("c1", "c2", "c3"):
            knowledge.write_facts(
                [
                    _make_fact(
                        fact_id=f"fact-{n:03d}",
                        component_id=cid,
                        created_iter=n,
                        created_run_id="run",
                        scope="handler",
                        evidence=["src/a.py"],
                        confidence="review_passed",
                        claim=claim,
                    )
                    for n in (1, 2)
                ],
                root,
                cid,
                "run",
            )
        config = KnowledgeConfig(
            enabled=True,
            knowledge_root=root,
            max_core_tokens=2000,
            max_dependency_tokens=1 if overflow else 1000,
            max_sibling_tokens=1 if overflow else 500,
        )
        return knowledge.build_knowledge_context(manifest, manifest.components[0], root, config)

    return render


SCENARIOS: dict[str, Callable[[Path], str]] = {
    "claude_md": _claude_md,
    "ctx_empty": _ctx_empty,
    "ctx_current_only": _ctx_current_only,
    "ctx_acceptance": _ctx_acceptance,
    "ctx_not_remeasured_dated": _ctx_not_remeasured_dated,
    "ctx_not_remeasured_legacy": _ctx_not_remeasured_legacy,
    "ctx_resolved": _ctx_resolved,
    "ctx_history": _ctx_history,
    "ctx_all": _ctx_all,
    "claim_reverted": _claim(True, _UNMET),
    "claim_not_reverted": _claim(False, _UNMET),
    "claim_all_judged_passed": _claim(True, _ALL_PASSED),
    "claim_no_verdict": _claim(True, []),
    "claim_empty": _claim_empty,
    "diff_scope_plain": _diff_scope_plain,
    "diff_scope_harness": _diff_scope_harness,
    "diff_scope_truncated": _diff_scope_truncated,
    "prd_tamper": _prd_tamper,
    "scope_unreadable_cause": _scope_unreadable_cause,
    "scope_unreadable_no_cause": _scope_unreadable_no_cause,
    "policy_diff_unreadable": _policy_diff_unreadable,
    "factory_guard": _factory_guard,
    "knowledge_plain": _knowledge(False),
    "knowledge_overflow": _knowledge(True),
    "loop_measurement": _loop_measurement,
}


# ---------------------------------------------------------------------------
# Test 1: the enrolment claim (RED before the #303 hoist, GREEN after).
# ---------------------------------------------------------------------------

#: One distinctive sentence per fragment group, measured by the critic to
#: be present in the rendered `before/` captures and absent from every
#: enrolled *_PROMPT body on 87a20b5.
NEEDLES: dict[str, str] = {
    "context.format_for_prompt closing": "do not assume they still apply.",
    "context.format_for_prompt heading": "## Current failures (measured in attempt ",
    "review.claim_retry_context": "Set-point disagreement: you marked the stories below done",
    "verify._diff_scope_details": "do NOT `git checkout ",
    "verify.check_prd_stories tamper": "permission to change what the component is measured",
    "verify.check_scope_unreadable": "The allowedPaths this component must be judged against",
    "verify.check_policy_envelope": "do not treat this as permission to merge.",
    "factory._run_component guard": "Do not widen allowedPaths.",
    "knowledge.build_knowledge_context": "Treat as ground truth unless contradicted",
    "init_cmd verification section": "kstrl runs the checks of this project's `[stack]`",
}


@pytest.mark.parametrize("site", sorted(NEEDLES))
def test_builder_text_is_under_an_enrolled_prompt(site: str) -> None:
    needle = NEEDLES[site]
    bodies = list(_PROMPTS.values())
    assert any(needle in body for body in bodies), (
        f"{site}: the text {needle!r} reaches a role's prompt but appears in "
        "no enrolled *_PROMPT body, so H3 cannot see it change."
    )


# ---------------------------------------------------------------------------
# Test 2: delivered-output digests (a pin, GREEN before and after -- the
# whole point of the #303 hoist is that no delivered byte moves).
# ---------------------------------------------------------------------------

#: The empty-render digest (sha256 of "") shared by any scenario whose
#: render is the empty string.
_EMPTY_DIGEST = _sha256("")

DIGESTS: dict[str, str] = {
    # Moved in #696 slice 6, deliberately: one CLAUDE.md for every tree.
    "ctx_acceptance": "a5d3778526055d1d5e67390f28fb796a948d2771b27ad010bdd45ee2262d01a4",
    "claude_md": "c15168db40ebf9bef7ea24e3192132749d1cd7ddddb78abbb65eccc6b37668d4",
    "ctx_all": "2659f4ff4e999f3ecb9b335e3bc25894ef92f52059f7f86203bf1b5f3a45cad5",
    "ctx_current_only": "4845a0234067507977e978189765994b040230787d3c98889ee7f11e5f2c199f",
    "ctx_empty": "85610680224f004a17afd838c2fc1d0fc601c7886da03fac757bf8b9e202cec6",
    "ctx_history": "ebb07ebb179aa5ee669abbabb065f2be05d056e8724590e42e7c41111a4344de",
    "ctx_not_remeasured_dated": "0e5121f93a81449a5910aa30a1d8cc581a745dcaaa8994aea3655860178e3f69",
    "ctx_not_remeasured_legacy": "d9eabccce09421fbe9ab0a002957913bac7a6c7ecd15df1a10ee6ed60b248af4",
    "ctx_resolved": "caa8d60d63cfdccbbb17b5bb055b57e778036197294f91d199f269ff95d47b74",
    "diff_scope_harness": "39f554f171250d631b05c0209c4da0e37910eb5021d7f368481c3e06a8959517",
    "diff_scope_plain": "6ba1a900c7e465f20cf67df9d78e80fee909f8b25007ef100d236e1055cafe44",
    "diff_scope_truncated": "2e6cb243401aea539d232f5f33c3fad4bbac417771fe38a03603afc7a0357ac4",
    "factory_guard": "975a099e1906f2c1585fb97a5c2b06dd3db574ab33b097b4c24b7173e84e39e5",
    "knowledge_overflow": "d7ef0f68094a4ae521985ae10270f5302813be17a1e36c0348075679f08d732b",
    "knowledge_plain": "88716e667ff7e50d57775c3900aadf3c668f9929983dd691acb557ffba65738f",
    "loop_measurement": "d52903bd125d4505f5e6026f62ff93a6db7bcca66a6a3212c79acb36a5c80740",
    "policy_diff_unreadable": "f628c8378f20bb097023e0a39dc113f268f8e75d7b06755b58f8d9cc97a82677",
    "prd_tamper": "1a210bdd7077628703f6e5bfa2893be949ebcf0636102cd4d7507a7b7af41a97",
    "scope_unreadable_cause": "599979f4317abead4ddb248f85e3b2a5b73bf6d37bcf66951694959acc3b9ace",
    "scope_unreadable_no_cause": "954eb333903b94d0c4dd36c9b03a9605ea16355f7311274d69d0232a67796d9e",
    "claim_all_judged_passed": ("3a0b75c6884a0334b36a0bd91640a9f2a4c8833c0382c70afdd01315b1d85a68"),
    "claim_empty": _EMPTY_DIGEST,
    "claim_no_verdict": "532c99dd2f7248673a850475e17950a4587b842cb4ad97666d6001df10b1efb4",
    "claim_not_reverted": "306a5c0154871b603ff5f76c405c57a0a06f8be5e4fb3914994b2827f3b24411",
    "claim_reverted": "631e395ab1166d21de851904853c4abaf6b5251324ffa97b1631db33b9685088",
}


def test_scenario_and_digest_keys_match() -> None:
    """A scenario cannot be added without a pin, nor a pin left behind
    without a scenario."""
    assert set(SCENARIOS) == set(DIGESTS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_delivered_prompt_digest(name: str, tmp_path: Path) -> None:
    render = SCENARIOS[name]
    text = render(tmp_path)
    actual = _sha256(text)
    expected = DIGESTS[name]
    assert actual == expected, (
        f"{name}: delivered-output digest moved (expected {expected}, got "
        f"{actual!r}). A moved digest means the text a role receives has "
        "changed. If that happens during this PR, the hoist dropped or "
        "added a byte: fix the hoist. Updating the digest is the "
        "audit-trail write and is correct only for a deliberate text "
        "change, which this PR is not."
    )


# ---------------------------------------------------------------------------
# Test 3: orphan guard for the 33 constants read at CALL TIME.
# ---------------------------------------------------------------------------

#: name -> (module holding the constant, scenario that exercises the
#: branch reading it). Every scenario here already renders through the
#: real production function, so patching the module attribute and
#: re-rendering is the same guard test_prompt_versions.py runs for the
#: nine already-enrolled prompts, one call site further in.
CALL_TIME_GUARDS: dict[str, tuple[ModuleType, str]] = {
    "ITERATION_CONTEXT_HEADER_PROMPT": (context, "ctx_empty"),
    "ITERATION_CONTEXT_CURRENT_PROMPT": (context, "ctx_current_only"),
    "ITERATION_CONTEXT_NOT_REMEASURED_PROMPT": (context, "ctx_not_remeasured_legacy"),
    "ITERATION_CONTEXT_NOT_REMEASURED_SINCE_PROMPT": (context, "ctx_not_remeasured_dated"),
    "ITERATION_CONTEXT_RESOLVED_PROMPT": (context, "ctx_resolved"),
    "ITERATION_CONTEXT_HISTORY_PROMPT": (context, "ctx_history"),
    "ITERATION_CONTEXT_CLOSING_PROMPT": (context, "ctx_empty"),
    "ACCEPTANCE_RETRY_PROMPT": (context, "ctx_acceptance"),
    "CLAIM_RETRY_PROMPT": (review_claims, "claim_reverted"),
    "CLAIM_REVERTED_PROMPT": (review_claims, "claim_reverted"),
    "CLAIM_NOT_REVERTED_PROMPT": (review_claims, "claim_not_reverted"),
    "CLAIM_PARTIALLY_JUDGED_PROMPT": (review_claims, "claim_all_judged_passed"),
    "CLAIM_NO_VERDICT_PROMPT": (review_claims, "claim_no_verdict"),
    "DIFF_SCOPE_BASE_BRANCH_PROMPT": (verify_diff, "diff_scope_plain"),
    "DIFF_SCOPE_ALLOWED_PATHS_PROMPT": (verify_diff, "diff_scope_plain"),
    "DIFF_SCOPE_HARNESS_PATHS_PROMPT": (verify_diff, "diff_scope_harness"),
    "DIFF_SCOPE_VIOLATIONS_PROMPT": (verify_diff, "diff_scope_plain"),
    "DIFF_SCOPE_TRUNCATION_PROMPT": (verify_diff, "diff_scope_truncated"),
    "PRD_TAMPER_FIELDS_PROMPT": (verify, "prd_tamper"),
    "PRD_TAMPER_GATES_PROMPT": (verify, "prd_tamper"),
    "SCOPE_UNREADABLE_EXPLANATION_PROMPT": (verify_diff, "scope_unreadable_cause"),
    "SCOPE_UNREADABLE_REMEDY_PROMPT": (verify_diff, "scope_unreadable_cause"),
    "POLICY_DIFF_UNREADABLE_PROMPT": (verify_diff, "policy_diff_unreadable"),
    "IN_LOOP_SCOPE_VIOLATION_PROMPT": (factory, "factory_guard"),
    "KNOWLEDGE_CONTEXT_PROMPT": (knowledge, "knowledge_plain"),
    "KNOWLEDGE_OVERFLOW_PROMPT": (knowledge, "knowledge_overflow"),
    "CLAUDE_MD_OVERVIEW_PROMPT": (init_cmd, "claude_md"),
    "CLAUDE_MD_VERIFICATION_PROMPT": (init_cmd, "claude_md"),
    "CLAUDE_MD_LEARNINGS_PROMPT": (init_cmd, "claude_md"),
    "LAST_ITERATION_MEASUREMENT_PROMPT": (iteration_prompt, "loop_measurement"),
}


@pytest.mark.parametrize("name", sorted(CALL_TIME_GUARDS))
def test_enrolled_fragment_reaches_its_builder(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, scenario_key = CALL_TIME_GUARDS[name]
    monkeypatch.setattr(module, name, _ORPHAN_MARKER)
    rendered = SCENARIOS[scenario_key](tmp_path)
    assert _ORPHAN_MARKER in rendered, (
        f"{module.__name__}.{name} does not reach the text {scenario_key!r} "
        "renders, so the enrolled constant is not what the role receives "
        "-- the builder stopped reading it, wraps text around it that no "
        "snapshot covers, or truncates it."
    )


#: No constant is captured BY VALUE into a container at import any more:
#: the twelve fix hints went with the parsers (#696 slice 5) and the
#: language tables with the language prompts (#696 slice 6), so the only
#: guard left is the call-time one (Test 3).
CONTAINER_CAPTURED_NAMES: frozenset[str] = frozenset()


def test_every_call_time_fragment_has_a_guard() -> None:
    """Closed by construction: a 31st constant with no entry in either
    set fails here rather than being silently unguarded."""
    covered = set(CALL_TIME_GUARDS) | CONTAINER_CAPTURED_NAMES
    assert covered == set(BUILDER_PROMPTS), (
        f"enrolled but unguarded: {sorted(set(BUILDER_PROMPTS) - covered)}; "
        f"guarded but not enrolled: {sorted(covered - set(BUILDER_PROMPTS))}"
    )
