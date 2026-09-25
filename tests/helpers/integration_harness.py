"""A merged feature in a real git repository, and the real factory run over it (#482).

Every integration-review test drives the real ``run_factory`` in create_prs
mode over a repository whose ``main`` already holds the feature's merged
components. Three things are stubbed: the component worker (nothing may run,
every component is already COMPLETED and merged), the merge re-poll, and
``kstrl.agents.get_agent``, which returns :class:`FakeReviewer`. The Phase 3
integrated check, the temporary worktree, ``run_review`` and its diffstat
check are real.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import git
from kstrl.agents.base import UsageRecord
from kstrl.config import KstrlConfig
from kstrl.contract import ContractConfig, ContractMode
from kstrl.factory import FactoryConfig, FactoryResult, run_factory
from kstrl.integration import integration_stories
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.pipeline import ComponentPipeline
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers.component_prd import PASSING_STORY, write_component_prd
from tests.helpers.gitrepo import git_in, set_identity

COMPONENTS = ("comp-a", "comp-b")
STORE = "src/store.py"
API = "src/api.py"


def rev(root: Path, ref: str = "main") -> str:
    done = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return done.stdout.strip()


def commit_file(root: Path, rel: str, text: str) -> str:
    """Commit one file on the checked-out ``main``; returns the new head."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git_in(root, "add", rel)
    git_in(root, "commit", "-q", "-m", f"add {rel}")
    return rev(root)


def manifest_file(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def state_file(root: Path) -> Path:
    return root / ".kstrl" / "integration" / "state.json"


def evidence_files(root: Path) -> list[Path]:
    """Every review evidence file any run under ``root`` wrote, oldest run first."""
    return sorted((root / ".kstrl" / "runs").glob("*/integration/review-*.json"))


def merged_feature(
    root: Path,
    *,
    extra_components: Sequence[str] = (),
    feature_base: str | None = None,
) -> tuple[str, str]:
    """A repo whose ``main`` moved from ``base`` to ``head`` by two commits,
    with every component COMPLETED, holding a PR and a merge commit, and
    ``featureBaseSha = base`` (or ``feature_base`` when given). Returns
    ``(base, head)``. The kstrl scaffolding is left untracked."""
    root.mkdir(parents=True, exist_ok=True)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    git_in(root, "commit", "-q", "--allow-empty", "-m", "base")
    base = rev(root)
    ids = [*COMPONENTS, *extra_components]
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("test prompt", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n", encoding="utf-8")
    for cid in ids:
        write_component_prd(root, f"scripts/kstrl/feature/{cid}/prd.json", stories=[PASSING_STORY])
    commit_file(root, STORE, "def save(x: int) -> int:\n    return x\n")
    head = commit_file(
        root,
        API,
        "from src.store import save\n\n\ndef handle(x: int) -> int:\n    return save(x)\n",
    )
    components: list[Component] = []
    for number, cid in enumerate(ids, start=1):
        comp = Component(
            cid,
            cid.title(),
            "Desc",
            [],
            f"scripts/kstrl/feature/{cid}/prd.json",
            f"kstrl/factory/{cid}",
        )
        comp.status = ComponentStatus.COMPLETED.value
        comp.pr_url = f"https://github.com/o/r/pull/{number}"
        comp.merge_sha = head
        components.append(comp)
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
    )
    manifest.feature_base_sha = base if feature_base is None else feature_base
    manifest.save(manifest_file(root))
    return base, head


def review_payload(root: Path, base: str) -> dict[str, Any]:
    """A reviewer reply that passes IC1 to IC5 with the PRD's exact criterion
    text, reporting the diffstat git measures for ``base...main``."""
    stat = git.get_diff_stat(base, root, resolved=True)
    return {
        "observedDiffstat": {
            "files": stat.files,
            "insertions": stat.insertions,
            "deletions": stat.deletions,
        },
        "stories": [
            {
                "storyId": story.story_id,
                "storyTitle": story.title,
                "criteria": [
                    {
                        "criterion": story.criterion,
                        "verdict": "pass",
                        "explanation": f"{API}:1 read and checked",
                        "suggestion": "",
                    }
                ],
            }
            for story in integration_stories(base)
        ],
        "concerns": [],
        "exhaustively_searched": True,
        "overallNotes": "",
    }


def set_verdict(payload: dict[str, Any], story_id: str, verdict: str, explanation: str) -> None:
    for story in payload["stories"]:
        if story["storyId"] == story_id:
            story["criteria"][0]["verdict"] = verdict
            story["criteria"][0]["explanation"] = explanation
            return
    raise KeyError(story_id)


class FakeReviewer:
    """The reviewer CLI's stand-in: records every prompt and cwd, replies
    with ``output``, and reports one metered call the way an adapter does."""

    def __init__(self, output: str, on_prompt: Callable[[str], None] | None = None) -> None:
        self._output = output
        self._on_prompt = on_prompt
        self.prompts: list[str] = []
        self.cwds: list[Path | None] = []
        self.usage_records: list[UsageRecord] = []
        self.final_message: str | None = None

    @property
    def name(self) -> str:
        return "fake-integration-reviewer"

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        if self._on_prompt is not None:
            self._on_prompt(prompt)
        yield from self._output.splitlines()
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


def factory_config(root: Path, **overrides: Any) -> FactoryConfig:
    """The FactoryConfig every integration test runs under, plus ``overrides``."""
    config: dict[str, Any] = dict(
        use_worktrees=False,
        create_prs=True,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="hard",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        contract_config=ContractConfig(
            mode=ContractMode.TIER.value, test_command="true", timeout=60.0
        ),
        progress_log_path=root / "progress.jsonl",
    )
    config.update(overrides)
    return FactoryConfig(**config)


def kstrl_config(root: Path) -> KstrlConfig:
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


def run_factory_over(
    root: Path, reviewer: FakeReviewer, **overrides: Any
) -> tuple[FactoryResult, str]:
    """The real ``run_factory`` over ``root``'s saved manifest. Returns the
    result and everything the run printed."""
    out = io.StringIO()
    with (
        patch(
            "kstrl.factory._run_component",
            side_effect=AssertionError("no component may run: every component is merged"),
        ),
        patch.object(ComponentPipeline, "repoll_merge_pending", return_value=None),
        patch("kstrl.agents.get_agent", return_value=reviewer),
    ):
        result = run_factory(
            Manifest.load(manifest_file(root)),
            factory_config(root, **overrides),
            kstrl_config(root),
            PlainUI(no_color=True, file=out),
            root,
            manifest_file(root),
        )
    return result, out.getvalue()
