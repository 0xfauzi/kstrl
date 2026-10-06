"""``decompose_spec`` driven end to end with a stub architect.

Every test here hands ``decompose_spec`` a real spec file under
``tmp_path`` and an agent that emits canned JSON, then reads what the
run left on disk: the manifest, the planned PRDs (#545, #568), the
``spec-issues.json`` audit (R1.7), the register beside the manifest
(#260) and the evolution journal. Covered: the successful decomposition
in single-PR and multi-PR mode, the retry on invalid or vacuous output
(R1.8) and the terminal failure that leaves no partial files, the halt
on an escalated blocker and the continuation on a closed issue, a
register write that cannot land failing the decompose while the halt
path still halts (#260 round 3), and the Spec Convergence report as the
operator meets it across runs, renames and projects (#280, #314). The
``_extract_json`` / ``_validate_decompose_output`` rules are carried by
``tests/test_decompose_run.py``, where a stub architect emits each bad
shape.

The agents and payload builders at module level (``MockDecomposeAgent``,
``SequenceAgent``, ``VALID_DECOMPOSE_OUTPUT``, ``BLOCKER_ISSUE``,
``_story``, ``_with_ids``, ``_closures_for``, ``_single_component_output``,
``_run_decompose``) are imported by other test modules.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.decompose import SpecBlockerError, decompose_spec
from kstrl.evolution import SPEC_ISSUES_EVENT, EvolutionConfig, EvolutionJournal
from kstrl.prd import PRD
from kstrl.statedir import plan_prd_path
from kstrl.ui.plain import PlainUI
from tests.helpers.before_spend import no_base_check
from tests.helpers.journal import journal_at
from tests.helpers.prompt_calls import architect_call
from tests.helpers.stack_confirmation import PROPOSED_STACK


class MockDecomposeAgent:
    """Mock agent that returns predetermined JSON output."""

    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-decompose"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        yield from self._output.splitlines()
        if self._output.strip():
            self._final_message = self._output.splitlines()[-1]

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_DECOMPOSE_OUTPUT = json.dumps(
    {
        "components": [
            {
                "id": "database",
                "title": "Database Schema",
                "description": "Create the database tables",
                "dependencies": [],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/database/",
                ],
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Create users table",
                        "acceptanceCriteria": ["Users table exists", "Tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            },
            {
                "id": "api",
                "title": "API Endpoints",
                "description": "Create REST API endpoints",
                "dependencies": ["database"],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/api/",
                ],
                "userStories": [
                    {
                        "id": "US-002",
                        "title": "GET /users endpoint",
                        "acceptanceCriteria": ["Returns user list", "Tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            },
        ],
        # v3.0.0 requires the array, even empty: a payload that omitted
        # it used to pass with zero decisions and zero escalations, and
        # zero agrees with any count.
        "stack": PROPOSED_STACK,
        "spec_issues": [],
        "decisions": [],
    }
)


class TestSpecIssues:
    """Tests for the red-team / spec-audit surface."""

    def test_decompose_raises_on_escalation(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec\nDo something good.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "stack": PROPOSED_STACK,
                "spec_issues": [
                    {
                        "id": "spec-empty",
                        "severity": "blocker",
                        "kind": "ambiguity",
                        "summary": "Spec is empty",
                        "location": "everywhere",
                        "suggestion": "Write actual requirements",
                    }
                ],
                "decisions": [
                    {
                        "issue": "spec-empty",
                        "question": "what is this product for",
                        "disposition": "escalated",
                        "resolution": "the owner must say",
                    }
                ],
                "components": [],
            }
        )
        agent = MockDecomposeAgent(output)
        ui = PlainUI(no_color=True)
        with pytest.raises(SpecBlockerError) as exc_info:
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=ui,
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )
        assert len(exc_info.value.escalations) == 1
        assert exc_info.value.escalations[0].question == "what is this product for"
        # R1.7: the halt points at BOTH durable records. The audit holds
        # the finding; only the register holds the question the owner
        # has to answer and the reason it was not answered.
        lines = exc_info.value.artifact_lines()
        assert any("spec-issues.json" in line for line in lines)
        assert any("decisions.json" in line for line in lines)

    def test_decompose_continues_on_non_blockers(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "stack": PROPOSED_STACK,
                "spec_issues": [
                    {
                        "id": "edge-case",
                        "severity": "minor",
                        "kind": "missing_detail",
                        "summary": "Edge case unspecified",
                    }
                ],
                "decisions": [
                    {
                        "issue": "edge-case",
                        "question": "what does the empty-input path do",
                        "disposition": "assumed",
                        "resolution": "return an empty list; pinned by AC2",
                    }
                ],
                "components": [
                    {
                        "id": "comp-a",
                        "title": "A",
                        "description": "x",
                        "dependencies": [],
                        "allowedPaths": [
                            "src/",
                            "tests/",
                            "scripts/kstrl/feature/comp-a/",
                        ],
                        "userStories": [
                            {
                                "id": "US-001",
                                "title": "S1",
                                "acceptanceCriteria": ["AC1", "AC2"],
                                "priority": 1,
                                "passes": False,
                                "notes": "",
                            }
                        ],
                    },
                ],
            }
        )
        agent = MockDecomposeAgent(output)
        ui = PlainUI(no_color=True)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )
        assert len(manifest.components) == 1
        assert manifest.components[0].id == "comp-a"


class TestDecomposeSpec:
    """Tests for decompose_spec end-to-end."""

    def test_successful_decomposition(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# My Feature\nBuild a user management system.")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test-project",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )

        assert len(manifest.components) == 2
        assert manifest.components[0].id == "database"
        assert manifest.components[1].id == "api"
        assert manifest.components[1].dependencies == ["database"]
        assert manifest.project_name == "test-project"

        # Verify PRD files were created, at the plan path and not at the
        # manifest's prdPath, which the component branch commits (#545)
        assert manifest.components[0].prd_path == "scripts/kstrl/feature/database/prd.json"
        assert not (tmp_path / manifest.components[0].prd_path).exists()
        # #568: one plan id for the decompose, on every component, and the
        # directory the planned copies are under.
        plan_id = manifest.components[0].plan_id
        assert plan_id
        assert [c.plan_id for c in manifest.components] == [plan_id, plan_id]
        db_prd = plan_prd_path(tmp_path, "database", plan_id=plan_id)
        assert db_prd.exists()
        prd = PRD.load(db_prd)
        assert len(prd.user_stories) == 1
        assert prd.user_stories[0].id == "US-001"

        # Verify manifest was saved
        manifest_path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        assert manifest_path.exists()

    def test_single_pr_mode_uses_shared_branch(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="my-project",
            base_branch="main",
            single_pr=True,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )

        # All components should share the same branch
        branches = {c.branch_name for c in manifest.components}
        assert len(branches) == 1
        assert "my-project" in branches.pop()

    def test_multi_pr_mode_uses_separate_branches(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )

        branches = {c.branch_name for c in manifest.components}
        assert len(branches) == 2
        assert any("database" in b for b in branches)
        assert any("api" in b for b in branches)

    def test_retries_on_invalid_json(self, tmp_path: Path) -> None:
        """Agent returns invalid output first, then valid."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        call_count = 0

        class RetryAgent:
            @property
            def name(self) -> str:
                return "retry-mock"

            def run(
                self, prompt: str, cwd: Path | None = None, timeout: float | None = None
            ) -> Iterator[str]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    yield "not valid json"
                else:
                    yield VALID_DECOMPOSE_OUTPUT

            @property
            def final_message(self) -> str | None:
                return None

        ui = PlainUI(no_color=True)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=RetryAgent(),
            ui=ui,
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )

        assert call_count == 2
        assert len(manifest.components) == 2

    def test_fails_after_max_retries(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent("always invalid")
        ui = PlainUI(no_color=True)

        with pytest.raises(ValueError, match="Failed to decompose"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=ui,
                root_dir=tmp_path,
                max_retries=2,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )


class SequenceAgent:
    """Agent returning one canned output per invocation, recording prompts."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self._final_message: str | None = None
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "sequence-agent"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        output = self._outputs[min(len(self.prompts) - 1, len(self._outputs) - 1)]
        self._final_message = output
        yield from output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


def _story(**overrides: object) -> dict[str, object]:
    story: dict[str, object] = {
        "id": "US-001",
        "title": "S1",
        "acceptanceCriteria": ["AC1", "AC2"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    story.update(overrides)
    return story


def _with_ids(spec_issues: list[dict[str, object]]) -> list[dict[str, object]]:
    """The v3.0.0 schema requires a unique id on every issue."""
    return [
        entry if entry.get("id") else {**entry, "id": f"issue-{index}"}
        for index, entry in enumerate(spec_issues)
    ]


def _closures_for(spec_issues: list[dict[str, object]]) -> list[dict[str, object]]:
    """One decision per issue, as the v3.0.0 prompt requires.

    #260 made "blocker" severity and an escalated decision two views of
    one fact, and round 2 made the correspondence a per-record JOIN on
    the issue id rather than a count. Derived here rather than written
    out at every call site so a test that adds an issue cannot forget
    the half that makes the halt real.
    """
    return [
        {
            "issue": issue["id"],
            "question": f"who decides: {issue['summary']}",
            "disposition": ("escalated" if issue.get("severity") == "blocker" else "decided"),
            "resolution": "the owner must choose",
        }
        for issue in spec_issues
    ]


def _single_component_output(
    stories: list[dict[str, object]],
    spec_issues: list[dict[str, object]] | None = None,
    decisions: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "stack": PROPOSED_STACK,
        "components": [
            {
                "id": "comp-a",
                "title": "A",
                "description": "x",
                "dependencies": [],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/comp-a/",
                ],
                "userStories": stories,
            }
        ],
    }
    issues = _with_ids(spec_issues or [])
    payload["spec_issues"] = issues
    if decisions is not None:
        payload["decisions"] = decisions
    else:
        payload["decisions"] = _closures_for(issues)
    return json.dumps(payload)


class TestARegisterThatDidNotLandFailsTheDecompose:
    """#260 round 3. A swallowed write error silently disabled the whole
    register.

    The round-2 /simplify pass measured it: ``_write_decompose_artifact``
    caught ``OSError``, printed one line and returned ``None``, and the
    success path ignored the return, so a decompose whose register write
    failed still reported success. Every later ``ks factory`` run against
    that manifest then read status ``missing``, bound ``()`` and said
    nothing, because a missing register is the legal pre-#260 state.
    One full disk, one read-only mount, and the feature is off for good
    with no message anywhere.

    The rule: the register beside a saved manifest IS part of the result,
    so its write failure is the decompose's failure. The halting copy is
    not, because the halt reaches the operator through
    ``SpecBlockerError`` whether or not the file landed.
    """

    def test_the_success_path_actually_asks_for_required(self, tmp_path: Path) -> None:
        """Mutation guard, and the reason this class exists.

        Deleting ``required=True`` from the one call site left all 288
        tests passing: the unit tests above prove the helper honours the
        flag, and nothing proved the caller passes it. So this drives a
        real decompose with a register write that cannot land and
        asserts the run fails rather than reporting success.
        """
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# My Feature\nBuild a user management system.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        with (
            patch(
                "kstrl.decompose.write_decisions",
                side_effect=OSError("no space left on device"),
            ),
            pytest.raises(OSError, match="no space left on device"),
        ):
            decompose_spec(
                spec_path=spec_file,
                project_name="test-project",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

    def test_the_halt_path_still_halts_when_its_register_cannot_land(self, tmp_path: Path) -> None:
        """The other half of the policy, and the reason it is a flag
        rather than a rule. A halt reaches the operator through
        ``SpecBlockerError`` whether or not the file landed, so the
        halting copy must not turn an escalation into an OSError."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature\nTODO")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        output = json.dumps(
            {
                "stack": PROPOSED_STACK,
                "spec_issues": [
                    {
                        "id": "spec-empty",
                        "severity": "blocker",
                        "kind": "missing_detail",
                        "summary": "The spec has no requirements",
                        "location": "everywhere",
                        "suggestion": "Write actual requirements",
                    }
                ],
                "decisions": [
                    {
                        "issue": "spec-empty",
                        "question": "what is this product for",
                        "disposition": "escalated",
                        "resolution": "the owner must say",
                    }
                ],
                "components": [],
            }
        )
        with (
            patch(
                "kstrl.decompose.write_decisions",
                side_effect=OSError("no space left on device"),
            ),
            pytest.raises(SpecBlockerError) as exc_info,
        ):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )
        assert len(exc_info.value.escalations) == 1
        # The halt still names what it can: the audit landed, the
        # register did not, and the message must not point at a file
        # that is not there.
        lines = exc_info.value.artifact_lines()
        assert not any("decisions.json" in line for line in lines)


class TestVacuousPrdRejection:
    """R1.8: vacuous shapes that previously sailed through validation."""

    def test_vacuous_output_is_retryable(self, tmp_path: Path) -> None:
        """passes:true fails attempt 1; the retry prompt carries the
        error and attempt 2 succeeds."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        agent = SequenceAgent(
            [
                _single_component_output([_story(passes=True)]),
                _single_component_output([_story()]),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "passes" in agent.prompts[1]
        assert len(manifest.components) == 1


BLOCKER_ISSUE: dict[str, object] = {
    "id": "fast-undefined",
    "severity": "blocker",
    "kind": "ambiguity",
    "summary": "What 'fast' means is not defined",
    "location": "Performance section",
    "suggestion": "Specify a P95 latency budget",
}

MINOR_ISSUE: dict[str, object] = {
    "id": "edge-case-unspecified",
    "severity": "minor",
    "kind": "missing_detail",
    "summary": "Edge case unspecified",
    "location": "API section",
    "suggestion": "Document the empty-input path",
}


def _blockers(count: int) -> list[dict[str, object]]:
    """``count`` blockers the de-duplicator keeps apart, so an audit's
    recorded blocker count is the number asked for.

    The id varies with the summary because the v3.0.0 schema requires a
    unique id per issue and ``_with_ids`` only fills one in when it is
    absent: ``BLOCKER_ISSUE`` carries its own, so without this every
    entry would repeat it and validation would reject the payload.
    """
    return [
        {**BLOCKER_ISSUE, "id": f"blocker-{n}", "summary": f"blocker {n}"} for n in range(count)
    ]


def _run_decompose(
    tmp_path: Path,
    output: str,
    *,
    spec_name: str = "spec.md",
    project_name: str = "test",
) -> str:
    """Decompose a spec against a mock agent; returns the UI output.

    A blocker halt is swallowed, because what decompose printed and
    wrote before raising is what these tests are about.
    """
    spec_file = tmp_path / spec_name
    spec_file.write_text("# Spec\nBuild it.")
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    try:
        decompose_spec(
            spec_path=spec_file,
            project_name=project_name,
            base_branch="main",
            single_pr=False,
            agent=MockDecomposeAgent(output),
            ui=PlainUI(no_color=True, file=buffer),
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )
    except SpecBlockerError:
        pass
    return buffer.getvalue()


def _journal_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Every line of the journal file, parsed STRICTLY, in file order.

    Not a second copy of any selection rule - it selects nothing. It is
    here because ``get_spec_audits`` reads through
    ``read_progress_events``, which is deliberately tolerant and SKIPS a
    line it cannot parse. A test whose subject is what the WRITER put on
    disk cannot ask that reader: a malformed row would be skipped and
    the count would still come out right. The helper #337 deleted parsed
    strictly as a side effect of being a copy; this keeps the strictness
    and drops the copy.
    """
    journal = tmp_path / ".kstrl" / "evolution.jsonl"
    rows: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # Strict in both dimensions: a line that parses but is not an
        # object would satisfy `"run_id" not in row` and pass silently.
        if not isinstance(row, dict):
            raise AssertionError(f"journal line is not an object: {line!r}")
        rows.append(row)
    return rows


class TestSpecIssuesPersistence:
    """R1.7: red-team output becomes a durable artifact + journal event."""

    def _run(self, tmp_path: Path, output: str) -> Path:
        _run_decompose(tmp_path, output)
        return tmp_path / "scripts" / "kstrl" / "spec-issues.json"

    def test_artifact_written_on_halt(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "components": [],
                "stack": PROPOSED_STACK,
                "spec_issues": [BLOCKER_ISSUE],
                "decisions": _closures_for([BLOCKER_ISSUE]),
            }
        )
        with pytest.raises(SpecBlockerError) as exc_info:
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

        artifact = tmp_path / "scripts" / "kstrl" / "spec-issues.json"
        assert artifact.exists()
        assert exc_info.value.artifact_path == artifact

        content = json.loads(artifact.read_text(encoding="utf-8"))
        assert content["project"] == "test"
        assert content["specFile"] == "spec.md"
        assert content["halted"] is True
        assert content["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert content["issues"] == [
            {
                "id": "fast-undefined",
                "severity": "blocker",
                "kind": "ambiguity",
                "summary": "What 'fast' means is not defined",
                "location": "Performance section",
                "suggestion": "Specify a P95 latency budget",
            }
        ]

    def test_artifact_written_on_success(self, tmp_path: Path) -> None:
        artifact = self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text(encoding="utf-8"))
        assert content["halted"] is False
        assert content["counts"] == {"blocker": 0, "major": 0, "minor": 1}
        assert content["issues"][0]["summary"] == "Edge case unspecified"
        assert content["issues"][0]["location"] == "API section"

    def test_artifact_written_on_clean_audit(self, tmp_path: Path) -> None:
        """An empty issues array is the record that the audit ran and
        found nothing - distinct from no record at all."""
        artifact = self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text(encoding="utf-8"))
        assert content["halted"] is False
        assert content["issues"] == []

    def test_journal_event_on_halt(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "components": [],
                "stack": PROPOSED_STACK,
                "spec_issues": [BLOCKER_ISSUE],
                "decisions": _closures_for([BLOCKER_ISSUE]),
            }
        )
        with pytest.raises(SpecBlockerError):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

        assert len(_journal_rows(tmp_path)) == 1, "the writer put more than the audit on disk"
        events = journal_at(tmp_path).get_spec_audits()
        assert len(events) == 1
        assert events[0]["halted"] is True
        assert events[0]["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert events[0]["artifact"] == "scripts/kstrl/spec-issues.json"

    def test_journal_event_on_success(self, tmp_path: Path) -> None:
        self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        assert len(_journal_rows(tmp_path)) == 1, "the writer put more than the audit on disk"
        events = journal_at(tmp_path).get_spec_audits()
        assert len(events) == 1
        assert events[0]["halted"] is False
        assert events[0]["counts"] == {"blocker": 0, "major": 0, "minor": 1}

    def test_the_event_type_on_disk_is_the_wire_value(self, tmp_path: Path) -> None:
        """The one deliberate literal in the test tree, and why it is one.

        Every other site writes the row and reads it back through
        SPEC_ISSUES_EVENT, so renaming the constant renames both sides
        and nothing goes red. Measured in round 1 of review on #337:
        with SPEC_ISSUES_EVENT set to "spec_audit_row" and the two
        guard-side literals updated the way somebody doing the rename
        would update them, 437 passed and 1 xfailed while the journal
        wrote an event_type no journal already on disk carries. Rows
        already written are what makes the wire value not the constant's
        to change, so it is pinned here, once, against the bytes.

        Asserted as a substring of the file rather than as
        ``row["event_type"] == ...`` because that second shape is
        exactly what layer 2 of the event-name guard forbids, and one
        deliberate site is not worth an allowlist in a layer that has
        none.
        """
        self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )

        raw = (tmp_path / ".kstrl" / "evolution.jsonl").read_text(encoding="utf-8")

        assert '"event_type":"spec_issues"' in raw, (
            "the spec audit on disk no longer carries the wire value spec_issues. "
            f"Renaming SPEC_ISSUES_EVENT does not rename rows already written: {raw}"
        )


class TestPrdValidationInsideRetryLoop:
    """R1.8: PRD schema errors are retryable and never leave partial files."""

    def test_malformed_story_triggers_retry(self, tmp_path: Path) -> None:
        """A story missing the 'notes' key passes decompose-output
        validation but fails PRD schema validation; the error must feed
        back through the retry loop instead of crashing after it."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        malformed = _story()
        del malformed["notes"]
        agent = SequenceAgent(
            [
                _single_component_output([malformed]),
                _single_component_output([_story()]),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
            prompt_call=architect_call(tmp_path),
            timeout=None,
            before_spend=no_base_check,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "notes" in agent.prompts[1]
        assert len(manifest.components) == 1
        prd_path = plan_prd_path(tmp_path, "comp-a", plan_id=manifest.components[0].plan_id)
        assert prd_path.exists()
        assert PRD.load(prd_path).user_stories[0].id == "US-001"

    def test_no_partial_files_after_terminal_failure(self, tmp_path: Path) -> None:
        """Terminal validation failure must not leave prd.json, feature
        dirs, or a manifest behind."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        malformed = _story()
        del malformed["notes"]
        agent = MockDecomposeAgent(_single_component_output([malformed]))
        with pytest.raises(ValueError, match="Failed to decompose"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                max_retries=2,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

        assert not (tmp_path / "scripts" / "kstrl" / "feature").exists()
        assert not (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        assert list(tmp_path.rglob("prd.json")) == []

    def test_write_failure_cleans_up_partial_prds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If writing component 2's PRD fails, component 1's already
        written PRD and the directories created for it are removed; the
        spec-issues audit artifact survives."""
        import kstrl.decompose as decompose_mod

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        real_generate = decompose_mod._generate_component_prd
        calls: list[str] = []

        def flaky_generate(
            comp_data: dict[str, object],
            root_dir: Path,
            branch_name: str,
            spec_issues: Sequence[dict[str, str]] = (),
            *,
            plan_id: str,
        ) -> Path:
            calls.append(str(comp_data["id"]))
            if len(calls) == 2:
                raise OSError("disk full")
            return real_generate(  # type: ignore[arg-type]
                comp_data,
                root_dir,
                branch_name,
                spec_issues,
                plan_id=plan_id,
            )

        monkeypatch.setattr(decompose_mod, "_generate_component_prd", flaky_generate)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        with pytest.raises(OSError, match="disk full"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

        assert calls == ["database", "api"]
        assert list(tmp_path.rglob("prd.json")) == []
        assert not (tmp_path / "scripts" / "kstrl" / "feature").exists()
        # #545: the directories created for the planned copy go too.
        assert not (tmp_path / ".kstrl" / "plan").exists()
        assert not (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        # The audit artifact is deliberately kept.
        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()


class TestJournalFieldsAreReadNotStringified:
    """#280 round 2, finding 1: a null field is an absent field, on
    every site that reads one, not just the site that was patched."""

    def _run_with_history(self, tmp_path: Path, entry: dict[str, object]) -> str:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        return _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE]),
            spec_name="spec.md",
            project_name="mine",
        )

    def test_a_null_spec_file_is_not_a_phantom_rename(self, tmp_path: Path) -> None:
        """``str(entry.get("spec_file", ""))`` yields 'None' when the
        key is PRESENT and null, so the rename line fired comparing
        'None' with the real file: "the previous audit read None"."""
        output = self._run_with_history(
            tmp_path,
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine",
                "spec_file": None,
                "timestamp": "2026-08-20T00:00:00Z",
                "issues": [{"severity": "blocker", "kind": "ambiguity", "summary": "old"}],
            },
        )

        assert "previous audit read None" not in output
        assert "Runs are matched by project name" not in output

    def test_an_unscoreable_severity_does_not_put_a_false_zero_in_the_trend(
        self,
        tmp_path: Path,
    ) -> None:
        """The worse half of the same class. ``_issue_counts`` buckets
        by severity, so seven issues stored with a null severity were
        counted as nothing: "Previous run raised 0 issue(s)" and a 0 in
        the blocker trend for a run that raised seven. The audit is now
        refused and reported instead of part-scored."""
        output = self._run_with_history(
            tmp_path,
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine",
                "spec_file": "spec.md",
                "timestamp": "2026-08-20T00:00:00Z",
                "issues": [
                    {"severity": None, "kind": "ambiguity", "summary": f"old-{n}"} for n in range(7)
                ],
            },
        )

        assert "Previous run raised 0 issue(s)" not in output
        assert "0, 1 (blockers" not in output
        assert "could not be scored" in output


class TestOneJournalRead:
    """#280 round 2, findings 6 and 7, and #314: one read, taken through
    ``EvolutionJournal`` rather than past it."""

    def test_the_journal_is_parsed_once_per_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The trend and the accounting used to read the same file
        twice. The call site has both in scope, so one read feeds both
        and they cannot disagree because the file moved between them.

        Counted at ``_read_all_entries``, the journal's single read
        point, so dropping the ``audits=`` argument that lets the
        window reuse the snapshot fails here rather than costing a
        silent second parse.
        """
        reads: list[int] = []
        real = EvolutionJournal._read_all_entries

        def counting(self: EvolutionJournal) -> list[dict[str, object]]:
            reads.append(1)
            return real(self)

        monkeypatch.setattr(EvolutionJournal, "_read_all_entries", counting)
        _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE]),
            project_name="mine",
        )

        assert len(reads) == 1


class TestSpecConvergenceThroughDecompose:
    """The report as the operator meets it, on the real code path."""

    def _run(
        self,
        tmp_path: Path,
        issues: list[dict[str, object]],
        spec_name: str = "spec.md",
        project_name: str = "writers-room",
    ) -> str:
        return _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=issues),
            spec_name=spec_name,
            project_name=project_name,
        )

    def test_first_run_prints_no_report(self, tmp_path: Path) -> None:
        output = self._run(tmp_path, [BLOCKER_ISSUE])
        assert "Spec Convergence" not in output

    def test_second_run_compares_against_the_first(self, tmp_path: Path) -> None:
        """Also pins the ordering: the history is read before this run
        is appended, so "previous run" is never this run."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(
            tmp_path,
            [
                BLOCKER_ISSUE,
                {
                    "severity": "blocker",
                    "kind": "contradiction",
                    "summary": "Stage is recorded twice",
                    "location": "",
                    "suggestion": "",
                },
                MINOR_ISSUE,
            ],
        )

        assert "Spec Convergence" in output
        assert "Blocker:" in output
        assert "2 (previous run: 1, +1)" in output
        assert "Minor:" in output
        assert "1 (previous run: 0, +1)" in output
        assert "1, 2 (blockers, oldest run first)" in output
        assert "Previous run raised 1 issue(s): 1 reappear verbatim, 0 do not." in output

    def test_journal_entries_still_carry_no_run_id(self, tmp_path: Path) -> None:
        """The report reads entries the run-windowed reader drops; if a
        run_id ever appears here, that reader would start windowing
        spec audits by factory run and this feature would go quiet.

        Read off the bytes rather than through ``get_spec_audits``: the
        subject is what decompose PUT on disk, so a reader that dropped
        or renamed an unknown key could answer this question "no run_id"
        about rows that carry one, which is the failure the paragraph
        above names. Every row this run writes is a spec audit
        (``_record_spec_issues_event`` is decompose's only journal
        write), so nothing is selected here and no selection rule is
        copied.
        """
        self._run(tmp_path, [BLOCKER_ISSUE])
        rows = _journal_rows(tmp_path)

        assert rows, "the run wrote no journal rows, so this assertion pins nothing"
        assert all("run_id" not in row for row in rows), rows

    def test_a_legacy_entry_without_run_id_does_not_break_the_read(
        self,
        tmp_path: Path,
    ) -> None:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps({"event_type": "component_result", "component": "legacy"}) + "\n"
        )

        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "1 (previous run: 1, no change)" in output
        assert "1, 1 (blockers, oldest run first)" in output

    def test_a_rename_is_reported_rather_than_hidden(self, tmp_path: Path) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec.md")
        output = self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec-slice-1.md")

        assert "the previous audit read spec.md, this one read spec-slice-1.md" in output

    def test_a_different_project_has_its_own_history(self, tmp_path: Path) -> None:
        """Still its own trend, but no longer its own silence (#280).

        This test previously asserted the whole section was absent,
        which is exactly the loss #280 reports: the operator was told
        nothing at all about the audits the trend had just dropped.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "Trend:" not in output
        assert "No earlier audit of this project is recorded." in output
        assert "also records 1 spec audit(s)" in output
        assert "'writers-room' (1 audit(s), spec.md, last " in output

    def test_the_rename_that_lost_two_runs_now_says_so(self, tmp_path: Path) -> None:
        """#280's own shape, end to end: five audits, a project AND
        spec rename between runs 2 and 3, and a trend that covers only
        the last three. The trend is unchanged; what is new is the line
        that says the other two exist."""
        for spec, project in [
            ("spec.md", "writers-room"),
            ("spec.md", "writers-room"),
            ("spec-slice-1.md", "writers-room-slice1"),
            ("spec-slice-1.md", "writers-room-slice1"),
        ]:
            self._run(tmp_path, [BLOCKER_ISSUE], spec_name=spec, project_name=project)
        output = self._run(
            tmp_path,
            [BLOCKER_ISSUE],
            spec_name="spec-slice-1.md",
            project_name="writers-room-slice1",
        )

        assert "1, 1, 1 (blockers, oldest run first)" in output
        assert (
            "Note: audits are matched by project name, and this report covers "
            "'writers-room-slice1'. This journal also records 2 spec audit(s) under "
            "'writers-room' (2 audit(s), spec.md, last " in output
        )
        # The trend counted every audit of this project, so neither the
        # anomaly line nor the trend footnote has anything to say.
        assert "earlier audit(s) of 'writers-room-slice1'" not in output
        assert "outside the lookback window" not in output

    def test_an_ordinary_single_project_history_prints_no_note(
        self,
        tmp_path: Path,
    ) -> None:
        """The false-positive check. A warning that always fires is
        noise, and noise is how a report stops being read."""
        for _ in range(4):
            self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "1, 1, 1, 1, 1 (blockers, oldest run first)" in output
        assert "Note:" not in output

    def test_a_genuine_two_project_repo_is_told_the_truth(self, tmp_path: Path) -> None:
        """The measured cost of keying the note on "any other project"
        rather than on a matching spec file: a repo holding two real
        projects sees the line on every decompose of either.

        Pinned rather than hidden, because it is the price of covering
        #280's own session, where the spec file was renamed at the same
        moment as the project and a spec-file match would have found
        nothing. The line names the other project and the file it read,
        so an operator on 'billing' dismisses 'auth' (auth.md) at a
        glance instead of investigating.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="auth.md", project_name="auth")
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="billing.md", project_name="billing")
        output = self._run(
            tmp_path,
            [BLOCKER_ISSUE],
            spec_name="billing.md",
            project_name="billing",
        )

        assert "1, 1 (blockers, oldest run first)" in output
        assert "this report covers 'billing'" in output
        assert "also records 1 spec audit(s) under 'auth' (1 audit(s), auth.md, last " in output

    def test_a_rename_within_one_project_prints_no_note(self, tmp_path: Path) -> None:
        """The spec file moving is already reported by the rename line;
        the note is about the OTHER half of the key and must stay out
        of it."""
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec.md")
        output = self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec-slice-1.md")

        assert "the previous audit read spec.md" in output
        assert "Note:" not in output

    def test_no_note_when_the_journal_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A disabled journal reads nothing, so it can claim nothing."""
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        monkeypatch.setenv("KSTRL_EVOLUTION_ENABLED", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "Spec Convergence" not in output

    def test_the_note_is_not_windowed_by_the_lookback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``lookback_runs`` bounds how far back the trend reaches. A
        note about history the trend excludes that were itself windowed
        would omit history silently, which is the bug it fixes."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(4):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "also records 4 spec audit(s)" in output

    def test_a_windowed_out_run_is_counted_not_swallowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1 of review, finding 2: the note counted only
        cross-project audits while its wording claimed everything the
        journal holds, so same-project audits dropped by
        ``lookback_runs`` went silently missing. That is #280's own
        defect on the other axis. Reachable on the DEFAULT lookback of
        10 after 11 audits, so not an exotic config.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(6):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert (
            "1, 1, 1 (blockers, oldest run first; 4 older audit(s) outside the "
            "lookback window)" in output
        )
        # Round 2 of review: this is the configured steady state, so it
        # qualifies the trend in place rather than firing a warning that
        # would print on every run forever.
        assert "could not be scored" not in output

    def test_no_earlier_audit_is_claimed_only_when_none_is_recorded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1 of review, finding 1: with ``lookback_runs=0`` the
        trend reads nothing, and the report announced that no earlier
        audit of this project was recorded while three of them sat on
        disk. A confident statement over less data than the journal
        holds is the defect #280 is about.
        """
        for _ in range(3):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert "No earlier audit of this project is recorded." not in output
        assert (
            "No earlier audit of 'mine' could be compared, though this journal records 3." in output
        )

    def test_a_legacy_entry_with_no_issue_list_is_counted_not_denied(
        self,
        tmp_path: Path,
    ) -> None:
        """The second route to the same false line: entries the trend
        cannot compare because they carry no issue list, which is the
        legacy journal shape ``_stored_issues`` exists to tolerate."""
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            "".join(
                json.dumps(
                    {
                        "event_type": SPEC_ISSUES_EVENT,
                        "project": "mine",
                        "spec_file": "spec.md",
                        "timestamp": "2026-08-20T00:00:00Z",
                        "counts": {"blocker": 1, "major": 0, "minor": 0},
                    }
                )
                + "\n"
                for _ in range(3)
            ),
            encoding="utf-8",
        )

        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert "No earlier audit of this project is recorded." not in output
        assert (
            "No earlier audit of 'mine' could be compared, though this journal records 3." in output
        )
        assert (
            "Note: 3 earlier audit(s) of 'mine' fall inside the lookback window but "
            "could not be scored" in output
        )

    def test_the_journal_reader_agrees_with_the_event_name_written(
        self,
        tmp_path: Path,
    ) -> None:
        """Round 1 of review, finding 5, and #314 item 3. There is one
        ``SPEC_ISSUES_EVENT`` now, on ``evolution`` where the journal's
        schema is defined, and the writer imports it; the second copy
        this module used to hold, and the third the journal held as a
        literal, are gone.

        The end-to-end round trip is still worth its own test, because
        one constant makes the two spellings agree by construction but
        says nothing about the row actually reaching the reader: change
        the constant and this passes, break the write path and it
        fails.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        runs = EvolutionJournal(EvolutionConfig.load(tmp_path)).get_spec_issue_runs("writers-room")

        assert [r["event_type"] for r in runs] == [SPEC_ISSUES_EVENT]

    def test_a_clean_audit_still_reports_the_drop(self, tmp_path: Path) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [])

        assert "0 (previous run: 1, -1)" in output
        assert "1, 0 (blockers, oldest run first)" in output
        assert "Previous run raised 1 issue(s): 0 reappear verbatim, 1 do not." in output

    def test_the_window_is_the_journal_lookback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(3):
            self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (
            "1, 1, 1 (blockers, oldest run first; 1 older audit(s) outside the "
            "lookback window)" in output
        )

    def test_the_window_keeps_the_newest_audits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Direction, not just size. Every other window test in this
        file records audits that raise the same count, so ``[:last_n]``
        and ``[-last_n:]`` produce identical output and the whole suite
        stays green with the trend reading the OLDEST audits: measured,
        with the rule inverted, before this test existed. The counts
        differ per run here, so the trend line says which end was kept.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for count in (1, 2, 3):
            self._run(tmp_path, _blockers(count))
        output = self._run(tmp_path, _blockers(4))

        assert "2, 3, 4 (blockers, oldest run first" in output
        assert "1, 2, 4" not in output

    def test_no_report_when_the_journal_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE])
        monkeypatch.setenv("KSTRL_EVOLUTION_ENABLED", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "Spec Convergence" not in output

    def test_the_rendered_overlap_never_goes_negative(self, tmp_path: Path) -> None:
        """The end-to-end guard on the count: two current issues that
        normalize to the previous run's single issue must not print
        "2 reappear verbatim, -1 do not"."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        restated = dict(BLOCKER_ISSUE)
        # A DIFFERENT id: v3.0.0 requires them unique, and the point of
        # the test is two issues that NORMALIZE to one, not two records
        # that are the same record.
        restated["id"] = "fast-undefined-restated"
        restated["severity"] = "major"
        restated["summary"] = "  What   'FAST'  MEANS is not   defined  "
        output = self._run(tmp_path, [BLOCKER_ISSUE, restated])

        assert "Previous run raised 1 issue(s): 1 reappear verbatim, 0 do not." in output
        assert "-1 do not" not in output

    def test_a_bad_evolution_config_does_not_cost_the_audit_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R1.7 says the artifact is written for halt, success and
        clean-audit alike. Loading the journal config happens before
        that write, so a config that will not parse must degrade to
        "no journal", never abort the audit."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "many")

        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
        assert "Evolution config unreadable" in output
        assert "Spec Convergence" not in output

    def test_malformed_toml_does_not_cost_the_audit_artifact_either(
        self,
        tmp_path: Path,
    ) -> None:
        """The other ValueError path into EvolutionConfig.load.

        Scoped to the halt path on purpose, and the reason narrowed when
        #272 landed. It used to be that a malformed kstrl.toml ALSO
        failed LinearConfig.load further down decompose, after the
        architect had been paid for; ``ks decompose`` now rejects the
        file at command entry and never reaches this function, which
        ``tests/test_config_preflight.py`` pins. What this still covers
        is the direct call: ``decompose_spec`` invoked in-process, where
        the halt raises before the Linear load and the artifact is the
        only record the operator gets.
        """
        (tmp_path / "kstrl.toml").write_text("[evolution\nenabled = true\n")

        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
        assert "Evolution config unreadable" in output

    def test_the_artifact_is_written_before_any_journal_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The structural version of the two tests above, independent of
        which exceptions the config guard happens to catch.

        R1.7's artifact is the only durable record on the halt path, so
        nothing that can fail belongs upstream of it. An error the guard
        does not catch still leaves the artifact on disk.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise RuntimeError("journal config exploded")

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
        with pytest.raises(RuntimeError, match="journal config exploded"):
            decompose_spec(
                spec_path=spec_file,
                project_name="writers-room",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(
                    _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE])
                ),
                ui=PlainUI(no_color=True, file=io.StringIO()),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
