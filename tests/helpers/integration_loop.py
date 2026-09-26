"""The blocking integration loop over a real repository and the real factory (#483).

Builds on ``tests/helpers/integration_harness.py``: the same merged feature,
with allowedPaths and a tooling criterion on its two components, and a run
in which a fix component really runs. Stubbed: the component worker (it
marks the fix PRD's stories done and commits on ``main`` the way a squash
merge would), the PR flow (it reports the merge), the merge re-poll, and the
reviewers. The integration reviewer answers from a script, one entry per
review, reading the PRD the harness wrote for that review; the fix's own
per-component review passes. Phase 1, the scope snapshot and its
preflights, Phase 3, the temporary worktree, ``run_review`` and its
diffstat check, the state file and the inbox are real.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import git
from kstrl.agents.base import UsageRecord
from kstrl.factory import ComponentResult, FactoryResult, run_factory
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind
from kstrl.integration_fix import fix_prd_rel
from kstrl.manifest import Component, Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.pr import PrOutcome
from kstrl.prd import PRD
from kstrl.review import ReviewMode, ReviewResult
from kstrl.review import run_review as real_run_review
from kstrl.scope import ComponentScope
from kstrl.statedir import plan_prd_path, pre_run_prd_path
from kstrl.ui.plain import PlainUI
from tests.helpers import integration_harness as h
from tests.helpers.component_prd import PASSING_STORY, write_component_prd

#: comp-a owns more than the file the findings cite, so a fix scoped to a
#: whole component would include src/schema.py and the narrow scope does not.
SCOPES: dict[str, list[str]] = {
    "comp-a": ["src/store.py", "src/schema.py", "tests/test_store.py"],
    "comp-b": ["src/api.py", "tests/test_api.py"],
}
TOOLING = "Typecheck passes"
FIX_1 = "integration-fix-1"
IC2_FAIL = {"IC2": ("fail", f"{h.STORE}:1 re-applies request rules to stored rows")}
IC1_FAIL = {"IC1": ("fail", f"{h.API}:2 calls save with a string")}

Verdicts = dict[str, tuple[str, str]]


def loop_feature(root: Path, scopes: dict[str, list[str]] | None = None) -> tuple[str, str]:
    """``integration_harness.merged_feature`` with allowedPaths and a tooling
    criterion on each component PRD. Returns ``(base, head)``."""
    base, head = h.merged_feature(root)
    story = {**PASSING_STORY, "acceptanceCriteria": ["AC1", TOOLING]}
    for cid, allowed in (SCOPES if scopes is None else scopes).items():
        write_component_prd(
            root, f"scripts/kstrl/feature/{cid}/prd.json", allowed_paths=allowed, stories=[story]
        )
    return base, head


class ScriptedReviewer(h.FakeReviewer):
    """The integration reviewer's stand-in: review N answers from
    ``rounds[N]``, every story it does not name passes, and the diffstat is
    measured in the worktree it runs in. An unscripted review raises, which
    ``run_review`` turns into an infrastructure error."""

    def __init__(
        self,
        base: str,
        rounds: Sequence[Verdicts],
        on_prompt: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__("", on_prompt)
        self._base = base
        self._rounds = list(rounds)
        self.prd_paths: list[Path] = []

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        if self._on_prompt is not None:
            self._on_prompt(prompt)
        index = len(self.prompts) - 1
        if index >= len(self._rounds) or cwd is None:
            raise AssertionError(f"integration review {index + 1} was not scripted")
        yield from json.dumps(self._payload(self._rounds[index], cwd)).splitlines()
        self.usage_records.append(
            UsageRecord(
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                cost_usd=0.02,
                duration_seconds=1.0,
                source="claude-stream-json",
            )
        )

    def _payload(self, verdicts: Verdicts, cwd: Path) -> dict[str, Any]:
        stat = git.get_diff_stat(self._base, cwd, resolved=True)
        stories = PRD.load(self.prd_paths[-1]).user_stories
        return {
            "observedDiffstat": {
                "files": stat.files,
                "insertions": stat.insertions,
                "deletions": stat.deletions,
            },
            "stories": [
                {
                    "storyId": story.id,
                    "storyTitle": story.title,
                    "criteria": [
                        {
                            "criterion": story.acceptance_criteria[0],
                            "verdict": verdicts.get(story.id, ("pass", ""))[0],
                            "explanation": verdicts.get(story.id, ("", ""))[1]
                            or f"{h.API}:1 read and checked",
                            "suggestion": "",
                        }
                    ],
                }
                for story in stories
            ],
            "concerns": [],
            "exhaustively_searched": True,
            "overallNotes": "",
        }


class Rig:
    """The stubs for one factory run, and what they saw."""

    def __init__(
        self,
        root: Path,
        reviewer: ScriptedReviewer,
        *,
        fix_succeeds: bool = True,
        merge: str = "merged",
        fix_file: str = h.STORE,
        fix_text: str = "def save(x: int) -> int:\n    return int(x)\n",
    ) -> None:
        self.root = root
        self.reviewer = reviewer
        self.fix_succeeds = fix_succeeds
        self.merge = merge
        self.fix_file = fix_file
        self.fix_text = fix_text
        self.launched: list[str] = []
        self.scopes: dict[str, ComponentScope] = {}

    def review(self, agent: Any, prd_path: Path, *args: Any, **kwargs: Any) -> ReviewResult:
        """Integration reviews go to the real run_review; a fix's own review passes."""
        if "integration" in Path(prd_path).parts:
            self.reviewer.prd_paths.append(Path(prd_path))
            return real_run_review(agent, prd_path, *args, **kwargs)
        return ReviewResult(passed=True, mode=ReviewMode.HARD.value)

    def component(self, comp_id: str, *args: Any, **kwargs: Any) -> ComponentResult:
        self.launched.append(comp_id)
        self.scopes[comp_id] = next(a for a in args if isinstance(a, ComponentScope))
        if not self.fix_succeeds:
            return ComponentResult(comp_id, success=False, iterations=1, error="planted failure")
        # args[1] is the worktree (the root itself without worktrees). The
        # real worker copies the root PRD there; the engineer marks it done.
        prd = PRD.load(plan_prd_path(self.root, comp_id, plan_id=kwargs["plan_id"]))
        for story in prd.user_stories:
            story.passes = True
        target = Path(args[1]) / fix_prd_rel(comp_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        prd.save(target)
        h.commit_file(self.root, self.fix_file, f"{self.fix_text}# {comp_id}\n")
        return ComponentResult(comp_id, success=True, iterations=1, duration_seconds=1.0)

    def pr(self, comp: Component, *args: Any, **kwargs: Any) -> PrOutcome:
        comp.pr_number = 90 + len(self.launched)
        comp.pr_url = f"https://github.com/o/r/pull/{comp.pr_number}"
        if self.merge == "pending":
            return PrOutcome(
                pushed=True,
                pr_number=comp.pr_number,
                pr_url=comp.pr_url,
                merged=False,
                merge_pending=True,
                error="merge not confirmed",
            )
        return PrOutcome(
            pushed=True,
            pr_number=comp.pr_number,
            pr_url=comp.pr_url,
            merged=True,
            merge_sha=h.rev(self.root),
        )


def run_loop(root: Path, rig: Rig, **overrides: Any) -> tuple[FactoryResult, str]:
    """The real ``run_factory`` with blocking on, unless overridden."""
    out = io.StringIO()
    config = {"integration_blocking": True, **overrides}
    with (
        patch("kstrl.factory._run_component", side_effect=rig.component),
        patch("kstrl.factory.run_review", side_effect=rig.review),
        patch.object(ComponentPipeline, "repoll_merge_pending", return_value=None),
        patch("kstrl.agents.get_agent", return_value=rig.reviewer),
        patch("kstrl.pr.is_gh_available", return_value=True),
        patch("kstrl.pr.push_create_and_merge_pr", side_effect=rig.pr),
    ):
        result = run_factory(
            Manifest.load(h.manifest_file(root)),
            h.factory_config(root, **config),
            h.kstrl_config(root),
            PlainUI(no_color=True, file=out),
            root,
            h.manifest_file(root),
        )
    return result, out.getvalue()


def state(root: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    return loaded


def manifest_ids(root: Path) -> list[str]:
    return [c.id for c in Manifest.load(h.manifest_file(root)).components]


def planned_prd(root: Path, comp_id: str) -> Path:
    """The copy ``comp_id`` starts from, addressed through the manifest the
    way the run addresses it (#568)."""
    comp = Manifest.load(h.manifest_file(root)).get_component(comp_id)
    assert comp is not None, comp_id
    return pre_run_prd_path(root, comp.id, comp.prd_path, plan_id=comp.plan_id)


def run_halts(root: Path) -> list[InboxItem]:
    """The run-level HALTED_RUN items: the integration loop's, not a component's."""
    return [
        item
        for item in Inbox(root, InboxConfig()).items()
        if item.kind == ItemKind.HALTED_RUN and not item.component
    ]


def events(root: Path, name: str) -> list[dict[str, Any]]:
    """Every ``name`` event any run under ``root`` wrote, oldest run first."""
    found: list[dict[str, Any]] = []
    for path in sorted((root / ".kstrl" / "runs").glob("*/events.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("event") == name:
                found.append(json.loads(line))
    return found
