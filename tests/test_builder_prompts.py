"""H3a (#303): enrolment guards for the 53 harness-authored fragments that
seven multi-branch builders assemble into a role's prompt.

These builders emit optional blocks (a fragment may or may not appear in a
given render), so a single per-module ``*_PROMPT`` body cannot reproduce
their output byte for byte the way one enrolled prompt does elsewhere.
Each fragment is its own constant, hoisted from the literal that used to
sit inline. Four things are checked here, none of them by
``tests/test_prompt_versions.py`` alone:

1. ``test_builder_text_is_under_an_enrolled_prompt`` -- the reproduction:
   a needle sentence per site must appear in some enrolled ``*_PROMPT``
   body, or H3's snapshot mechanism cannot see it change.
2. ``test_delivered_prompt_digest`` -- 43 scenarios rendered through the
   REAL production functions, digested, and pinned. This is what proves
   the hoist changed no delivered byte, and it is also the only guard that
   would catch un-hoisted text living beside an enrolled fragment (#303's
   own residual, see plant 4 in the PR body).
3. ``test_enrolled_fragment_reaches_its_builder`` -- the 31 constants read
   at CALL TIME: patched to a marker, the marker must reach the rendered
   text, or the constant has gone orphan.
4. ``test_hint_table_is_the_enrolled_bodies`` /
   ``test_language_tables_are_the_enrolled_bodies`` -- the 22 constants
   captured BY VALUE into a container at import (``_HINT_PATTERNS``,
   ``_LANGUAGE_STANDARDS``, ``_LANGUAGE_ANTIPATTERNS``). Patching the
   module attribute is a no-op for these, so exact container equality is
   the only guard that can see one of them go orphan.

``BUILDER_RENDER_EXEMPT`` in ``tests/helpers/builder_prompts.py`` is why
none of these 53 also appears in ``test_prompt_versions.py``'s
``_RENDERERS``/render-guard machinery: each is one branch of a
multi-branch builder, so no single production function returns any one of
them verbatim, and the exact-equality guard there cannot hold it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from kstrl import factory, init_cmd, knowledge, parsers, review, verify
from kstrl.context import LEGACY_ATTEMPT, IterationContext, IterationRecord
from kstrl.findings import Finding
from kstrl.knowledge import Fact, KnowledgeConfig
from kstrl.loop import LoopResult
from kstrl.manifest import Component, Manifest
from kstrl.policy import PolicyConfig
from kstrl.review import CriterionReview, ReviewResult, ReviewVerdict
from kstrl.scope import ComponentScope
from tests.helpers.builder_prompts import BUILDER_PROMPTS
from tests.helpers.component_prd import write_component_prd
from tests.test_prompt_versions import _ORPHAN_MARKER, _PROMPTS

# ---------------------------------------------------------------------------
# Scenario renderers. Copied from the lane's capture.py (the byte-identity
# harness run before and after the #303 hoist) with three adaptations:
# tempfile.TemporaryDirectory() -> pytest's tmp_path, the `from kstrl import
# ...` imports moved to the top of this file, and main()/sys.argv dropped.
# The `_policy_diff_unreadable` filter and every other normalisation below
# are load-bearing, not tidiness: dropping the "Error: " line keeps the
# digest machine-independent, since that line carries an absolute path.
# ---------------------------------------------------------------------------


def _claude_md(language: str, framework: str) -> Callable[[Path], str]:
    def render(_tmp: Path) -> str:
        return init_cmd._generate_claude_md(
            {"name": "proj", "language": language, "framework": framework}
        )

    return render


def _ctx_empty(_tmp: Path) -> str:
    return IterationContext().format_for_prompt()


def _ctx_current_only(_tmp: Path) -> str:
    ctx = IterationContext()
    ctx.add_verification_failure("linter: E501 line too long", attempt=1)
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
        category="setpoint_disagreement",
        severity="major",
        location=location,
        explanation=f"{location}: marked done, pass on only 1 of 2 criteria",
    )


def _setpoint(reverted: bool, criteria: list[CriterionReview]) -> Callable[[Path], str]:
    def render(_tmp: Path) -> str:
        result = ReviewResult(passed=True, mode="advisory", criteria=criteria)
        return review.setpoint_retry_context([_finding("US-001")], result, reverted=reverted)

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


def _setpoint_empty(_tmp: Path) -> str:
    return review.setpoint_retry_context([], ReviewResult(passed=True, mode="advisory"))


def _diff_scope_plain(_tmp: Path) -> str:
    return "\n".join(verify._diff_scope_details("main", ["src/", "tests/"], None, ["a.py", "b.py"]))


def _diff_scope_harness(_tmp: Path) -> str:
    return "\n".join(
        verify._diff_scope_details(
            "origin/main", ["src/", "tests/"], ["scripts/kstrl/prd.json"], ["a.py"]
        )
    )


def _diff_scope_truncated(_tmp: Path) -> str:
    return "\n".join(
        verify._diff_scope_details("main", ["src/"], None, [f"f{i}.py" for i in range(20)])
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
    return "\n".join(verify.check_scope_unreadable("prd.json: No such file").details)


def _scope_unreadable_no_cause(_tmp: Path) -> str:
    return "\n".join(verify.check_scope_unreadable("").details)


def _policy_diff_unreadable(tmp: Path) -> str:
    empty = tmp / "not-a-repo"
    empty.mkdir()
    result = verify.check_policy_envelope(empty, "main", PolicyConfig(enabled=True))
    # The Error: line carries a machine-specific path; keep only the prose.
    return "\n".join(d for d in result.details if not d.startswith("Error:"))


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
        )
    return result.error or "<no error>"


_HINT_INPUTS: list[tuple[str, str, str]] = [
    ("missing_argument", "missing 1 required positional argument: 'x'", ""),
    ("too_many_arguments", "takes 2 positional arguments but 3 were given", ""),
    ("optional_not_handled", "Incompatible types in assignment (Optional[str])", ""),
    ("no_attribute", 'has no attribute "frobnicate"', ""),
    ("import_failed", "No module named 'widget'", ""),
    ("name_not_defined", "name 'widget' is not defined", ""),
    ("argument_type", 'Argument 1 has incompatible type "int"; expected "str"', ""),
    ("return_type", "Incompatible return value type (got int)", ""),
    ("assertion", "AssertionError", ""),
    ("unused_import", "unused import", "F401"),
    ("undefined_name", "undefined name", "F821"),
    ("line_too_long", "line too long", "E501"),
    ("no_match", "something nobody has a hint for", ""),
]


def _hint(message: str, rule: str) -> Callable[[Path], str]:
    def render(_tmp: Path) -> str:
        return parsers.generate_fix_hint(parsers.ParsedFailure(message=message, rule_or_test=rule))

    return render


def _fact(component_id: str, n: int, claim: str) -> Fact:
    return Fact(
        id=f"fact-{n:03d}",
        component_id=component_id,
        created_iter=n,
        created_run_id="run",
        scope="handler",
        evidence=["src/a.py"],
        confidence="review_passed",
        claim=claim,
    )


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
            knowledge.write_facts([_fact(cid, n, claim) for n in (1, 2)], root, cid, "run")
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
    "claude_md_python": _claude_md("Python", "FastAPI"),
    "claude_md_rust": _claude_md("Rust", ""),
    "claude_md_typescript": _claude_md("TypeScript", "Next.js"),
    "claude_md_javascript": _claude_md("JavaScript", ""),
    "claude_md_go": _claude_md("Go", ""),
    "claude_md_java": _claude_md("Java", ""),
    "claude_md_kotlin": _claude_md("Kotlin", ""),
    "claude_md_unknown": _claude_md("Unknown", ""),
    "ctx_empty": _ctx_empty,
    "ctx_current_only": _ctx_current_only,
    "ctx_not_remeasured_dated": _ctx_not_remeasured_dated,
    "ctx_not_remeasured_legacy": _ctx_not_remeasured_legacy,
    "ctx_resolved": _ctx_resolved,
    "ctx_history": _ctx_history,
    "ctx_all": _ctx_all,
    "setpoint_reverted": _setpoint(True, _UNMET),
    "setpoint_not_reverted": _setpoint(False, _UNMET),
    "setpoint_all_judged_passed": _setpoint(True, _ALL_PASSED),
    "setpoint_no_verdict": _setpoint(True, []),
    "setpoint_empty": _setpoint_empty,
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
}
for _name, _message, _rule in _HINT_INPUTS:
    SCENARIOS[f"hint_{_name}"] = _hint(_message, _rule)


# ---------------------------------------------------------------------------
# Test 1: the enrolment claim (RED before the #303 hoist, GREEN after).
# ---------------------------------------------------------------------------

#: One distinctive sentence per fragment group, measured by the critic to
#: be present in the rendered `before/` captures and absent from every
#: enrolled *_PROMPT body on 87a20b5.
NEEDLES: dict[str, str] = {
    "context.format_for_prompt closing": "do not assume they still apply.",
    "context.format_for_prompt heading": "## Current failures (measured in attempt ",
    "review.setpoint_retry_context": "Set-point disagreement: you marked the stories below done",
    "verify._diff_scope_details": "do NOT `git checkout ",
    "verify.check_prd_stories tamper": "permission to change what the component is measured",
    "verify.check_scope_unreadable": "The allowedPaths this component must be judged against",
    "verify.check_policy_envelope": "do not treat this as permission to merge.",
    "factory._run_component guard": "Do not widen allowedPaths.",
    "parsers.generate_fix_hint": "Unused import - remove it or use it.",
    "knowledge.build_knowledge_context": "Treat as ground truth unless contradicted",
    "init_cmd principles": "## Implementation Principles",
    "init_cmd language standards": "- Use `T | None` not `Optional[T]`",
    "init_cmd verification section": "kstrl resolves this project's test, typecheck and lint",
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

DIGESTS: dict[str, str] = {
    "claude_md_go": "c72dca7e7cbe6682205b588a0cc042ea97acecd05d70a6b23f10bd4ddf75f2d8",
    "claude_md_java": "0d0a0210263c62aa6dceb6aad160d0599f4df4fe9f9267a3f258f684034ce05d",
    "claude_md_javascript": "1f0e2f545fee6f9753435b8a9d61f5831a524ee780b45976703470d14df33073",
    "claude_md_kotlin": "8d9fdc30da6da90bf0e02441cb61cb9c63cd53560dac4621cf14870af5e4713c",
    "claude_md_python": "cae9b9f398dbda6199f504e3812b09c3b01ee2af0b1331063adaa6183350736a",
    "claude_md_rust": "08a1d00b46f9069a342e5e67dd610ca23ef17a9702c65d2feb0ded6239367487",
    "claude_md_typescript": "6dea6729c315bc2cb6aeca0547475ed3b1785fccfc7d66f19d70ee47a3b50dbc",
    "claude_md_unknown": "7ee33da5f7c68e05691019bdda3dead61136190bd0f8158c34cef78dd7a167ad",
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
    "hint_argument_type": "69e9948a8290e85aa0a5424fe81f2a6718e6c3d68390ee2c8f5d3a9f72fcb71a",
    "hint_assertion": "af4d89529f0d80814e3e842f1acc6d4d80fb8638c2a06e7deb7871ba322a8a7a",
    "hint_import_failed": "2cbeb381bc4e8bb186b9a0c5dbf93e1e8142d2b808494c1c01fbaab1367c4378",
    "hint_line_too_long": "bf88f1d0ba62922386cc9b9ec29796a0b7f708972abddbfee9de99f4a9829fad",
    "hint_missing_argument": "ea60b982ca8be700e75ff9215e228be605a85997690aae57e29ee5157e9b6db8",
    "hint_name_not_defined": "d8d7a74a81cea158c61290d19970546759bc6dac2256301f80b6c591a36cd3d9",
    "hint_no_attribute": "dead66db49b07e77ea2fc73ddfb3410c04743be63d4f6638aa0e48ec3e8387e8",
    "hint_no_match": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "hint_optional_not_handled": "bccdfd1d26e9e77a717d172d2bc9bb4131a5b2436eebe65f17c4d47313dad74a",
    "hint_return_type": "c150120da7ecae7e90bdf193a3398d16dfcc1a68fd166bfd969f84f020b446e4",
    "hint_too_many_arguments": "9b9adf45ea4a21579c5e1b058c3d3d4c8d1fd2dd9420d624743be04cee464b26",
    "hint_undefined_name": "11ffa9b6c490e54bc5e5b24577dc43a0c9ac1e3d149c3170f5ad5d6f90edc378",
    "hint_unused_import": "a87c684a0de34bcc8f356b067b10a1497c02710167b120917624feb90eac4871",
    "knowledge_overflow": "d7ef0f68094a4ae521985ae10270f5302813be17a1e36c0348075679f08d732b",
    "knowledge_plain": "88716e667ff7e50d57775c3900aadf3c668f9929983dd691acb557ffba65738f",
    "policy_diff_unreadable": "f628c8378f20bb097023e0a39dc113f268f8e75d7b06755b58f8d9cc97a82677",
    "prd_tamper": "1a210bdd7077628703f6e5bfa2893be949ebcf0636102cd4d7507a7b7af41a97",
    "scope_unreadable_cause": "599979f4317abead4ddb248f85e3b2a5b73bf6d37bcf66951694959acc3b9ace",
    "scope_unreadable_no_cause": "954eb333903b94d0c4dd36c9b03a9605ea16355f7311274d69d0232a67796d9e",
    "setpoint_all_judged_passed": (
        "3a0b75c6884a0334b36a0bd91640a9f2a4c8833c0382c70afdd01315b1d85a68"
    ),
    "setpoint_empty": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "setpoint_no_verdict": "532c99dd2f7248673a850475e17950a4587b842cb4ad97666d6001df10b1efb4",
    "setpoint_not_reverted": "306a5c0154871b603ff5f76c405c57a0a06f8be5e4fb3914994b2827f3b24411",
    "setpoint_reverted": "631e395ab1166d21de851904853c4abaf6b5251324ffa97b1631db33b9685088",
}


def test_scenario_and_digest_keys_match() -> None:
    """A scenario cannot be added without a pin, nor a pin left behind
    without a scenario."""
    assert set(SCENARIOS) == set(DIGESTS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_delivered_prompt_digest(name: str, tmp_path: Path) -> None:
    render = SCENARIOS[name]
    text = render(tmp_path)
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
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
# Test 3: orphan guard for the 31 constants read at CALL TIME.
# ---------------------------------------------------------------------------

#: name -> (module holding the constant, scenario that exercises the
#: branch reading it). Every scenario here already renders through the
#: real production function, so patching the module attribute and
#: re-rendering is the same guard test_prompt_versions.py runs for the
#: nine already-enrolled prompts, one call site further in.
CALL_TIME_GUARDS: dict[str, tuple[str, str]] = {
    "ITERATION_CONTEXT_HEADER_PROMPT": ("context", "ctx_empty"),
    "ITERATION_CONTEXT_CURRENT_PROMPT": ("context", "ctx_current_only"),
    "ITERATION_CONTEXT_NOT_REMEASURED_PROMPT": ("context", "ctx_not_remeasured_legacy"),
    "ITERATION_CONTEXT_NOT_REMEASURED_SINCE_PROMPT": ("context", "ctx_not_remeasured_dated"),
    "ITERATION_CONTEXT_RESOLVED_PROMPT": ("context", "ctx_resolved"),
    "ITERATION_CONTEXT_HISTORY_PROMPT": ("context", "ctx_history"),
    "ITERATION_CONTEXT_CLOSING_PROMPT": ("context", "ctx_empty"),
    "SETPOINT_RETRY_PROMPT": ("review", "setpoint_reverted"),
    "SETPOINT_REVERTED_PROMPT": ("review", "setpoint_reverted"),
    "SETPOINT_NOT_REVERTED_PROMPT": ("review", "setpoint_not_reverted"),
    "SETPOINT_PARTIALLY_JUDGED_PROMPT": ("review", "setpoint_all_judged_passed"),
    "SETPOINT_NO_VERDICT_PROMPT": ("review", "setpoint_no_verdict"),
    "DIFF_SCOPE_BASE_BRANCH_PROMPT": ("verify", "diff_scope_plain"),
    "DIFF_SCOPE_ALLOWED_PATHS_PROMPT": ("verify", "diff_scope_plain"),
    "DIFF_SCOPE_HARNESS_PATHS_PROMPT": ("verify", "diff_scope_harness"),
    "DIFF_SCOPE_VIOLATIONS_PROMPT": ("verify", "diff_scope_plain"),
    "DIFF_SCOPE_TRUNCATION_PROMPT": ("verify", "diff_scope_truncated"),
    "PRD_TAMPER_FIELDS_PROMPT": ("verify", "prd_tamper"),
    "PRD_TAMPER_GATES_PROMPT": ("verify", "prd_tamper"),
    "SCOPE_UNREADABLE_EXPLANATION_PROMPT": ("verify", "scope_unreadable_cause"),
    "SCOPE_UNREADABLE_REMEDY_PROMPT": ("verify", "scope_unreadable_cause"),
    "POLICY_DIFF_UNREADABLE_PROMPT": ("verify", "policy_diff_unreadable"),
    "IN_LOOP_SCOPE_VIOLATION_PROMPT": ("factory", "factory_guard"),
    "KNOWLEDGE_CONTEXT_PROMPT": ("knowledge", "knowledge_plain"),
    "KNOWLEDGE_OVERFLOW_PROMPT": ("knowledge", "knowledge_overflow"),
    "CLAUDE_MD_OVERVIEW_PROMPT": ("init_cmd", "claude_md_python"),
    "CLAUDE_MD_VERIFICATION_PROMPT": ("init_cmd", "claude_md_python"),
    "CLAUDE_MD_STANDARDS_HEADING_PROMPT": ("init_cmd", "claude_md_python"),
    "CLAUDE_MD_PRINCIPLES_PROMPT": ("init_cmd", "claude_md_python"),
    "CLAUDE_MD_ANTIPATTERNS_HEADING_PROMPT": ("init_cmd", "claude_md_python"),
    "CLAUDE_MD_LEARNINGS_PROMPT": ("init_cmd", "claude_md_python"),
}

_MODULES: dict[str, ModuleType] = {
    "context": __import__("kstrl.context", fromlist=["_"]),
    "review": review,
    "verify": verify,
    "factory": factory,
    "knowledge": knowledge,
    "init_cmd": init_cmd,
}


@pytest.mark.parametrize("name", sorted(CALL_TIME_GUARDS))
def test_enrolled_fragment_reaches_its_builder(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name, scenario_key = CALL_TIME_GUARDS[name]
    module = _MODULES[module_name]
    monkeypatch.setattr(module, name, _ORPHAN_MARKER)
    rendered = SCENARIOS[scenario_key](tmp_path)
    assert _ORPHAN_MARKER in rendered, (
        f"{module_name}.{name} does not reach the text {scenario_key!r} "
        "renders, so the enrolled constant is not what the role receives "
        "-- the builder stopped reading it, wraps text around it that no "
        "snapshot covers, or truncates it."
    )


#: The 22 constants captured BY VALUE into a container at import
#: (patching the module attribute is a no-op for these; see Test 4).
CONTAINER_CAPTURED_NAMES: frozenset[str] = frozenset(
    {
        "MISSING_ARGUMENT_HINT_PROMPT",
        "TOO_MANY_ARGUMENTS_HINT_PROMPT",
        "OPTIONAL_TYPE_HINT_PROMPT",
        "NO_ATTRIBUTE_HINT_PROMPT",
        "IMPORT_FAILED_HINT_PROMPT",
        "UNDEFINED_NAME_HINT_PROMPT",
        "ARGUMENT_TYPE_HINT_PROMPT",
        "RETURN_TYPE_HINT_PROMPT",
        "ASSERTION_HINT_PROMPT",
        "UNUSED_IMPORT_HINT_PROMPT",
        "RUFF_UNDEFINED_NAME_HINT_PROMPT",
        "LINE_TOO_LONG_HINT_PROMPT",
        "PYTHON_STANDARDS_PROMPT",
        "RUST_STANDARDS_PROMPT",
        "TYPESCRIPT_STANDARDS_PROMPT",
        "GO_STANDARDS_PROMPT",
        "JAVA_STANDARDS_PROMPT",
        "KOTLIN_STANDARDS_PROMPT",
        "PYTHON_ANTIPATTERNS_PROMPT",
        "RUST_ANTIPATTERNS_PROMPT",
        "TYPESCRIPT_ANTIPATTERNS_PROMPT",
        "GO_ANTIPATTERNS_PROMPT",
    }
)


def test_every_call_time_fragment_has_a_guard() -> None:
    """Closed by construction: a 54th constant with no entry in either
    set fails here rather than being silently unguarded."""
    covered = set(CALL_TIME_GUARDS) | CONTAINER_CAPTURED_NAMES
    assert covered == set(BUILDER_PROMPTS), (
        f"enrolled but unguarded: {sorted(set(BUILDER_PROMPTS) - covered)}; "
        f"guarded but not enrolled: {sorted(covered - set(BUILDER_PROMPTS))}"
    )


# ---------------------------------------------------------------------------
# Test 4: orphan guard for the 22 constants captured BY VALUE at import.
# ---------------------------------------------------------------------------


def test_hint_table_is_the_enrolled_bodies() -> None:
    """Exact list equality, so a reorder or a thirteenth entry fails.

    This is the only guard that can see one of these twelve constants go
    orphan: patching the module attribute is a no-op once the value has
    already been copied into the list at import time."""
    assert [hint for _pattern, hint in parsers._HINT_PATTERNS] == [
        parsers.MISSING_ARGUMENT_HINT_PROMPT,
        parsers.TOO_MANY_ARGUMENTS_HINT_PROMPT,
        parsers.OPTIONAL_TYPE_HINT_PROMPT,
        parsers.NO_ATTRIBUTE_HINT_PROMPT,
        parsers.IMPORT_FAILED_HINT_PROMPT,
        parsers.UNDEFINED_NAME_HINT_PROMPT,
        parsers.ARGUMENT_TYPE_HINT_PROMPT,
        parsers.RETURN_TYPE_HINT_PROMPT,
        parsers.ASSERTION_HINT_PROMPT,
        parsers.UNUSED_IMPORT_HINT_PROMPT,
        parsers.RUFF_UNDEFINED_NAME_HINT_PROMPT,
        parsers.LINE_TOO_LONG_HINT_PROMPT,
    ]


def test_language_tables_are_the_enrolled_bodies() -> None:
    """Exact dict equality: a seventh language or a hand-edited value
    fails, and this is the only guard that can see it -- patching the
    module attribute does not reach a value already copied into the
    dict at import."""
    assert init_cmd._LANGUAGE_STANDARDS == {
        "Python": init_cmd.PYTHON_STANDARDS_PROMPT,
        "Rust": init_cmd.RUST_STANDARDS_PROMPT,
        "TypeScript": init_cmd.TYPESCRIPT_STANDARDS_PROMPT,
        "Go": init_cmd.GO_STANDARDS_PROMPT,
        "Java": init_cmd.JAVA_STANDARDS_PROMPT,
        "Kotlin": init_cmd.KOTLIN_STANDARDS_PROMPT,
    }
    assert init_cmd._LANGUAGE_ANTIPATTERNS == {
        "Python": init_cmd.PYTHON_ANTIPATTERNS_PROMPT,
        "Rust": init_cmd.RUST_ANTIPATTERNS_PROMPT,
        "TypeScript": init_cmd.TYPESCRIPT_ANTIPATTERNS_PROMPT,
        "Go": init_cmd.GO_ANTIPATTERNS_PROMPT,
    }
