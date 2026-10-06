"""R0.6 input hygiene: LLM-emitted component ids and branch names.

Component ids become filesystem path segments (.kstrl/worktrees/<id>,
scripts/kstrl/feature/<id>) and branch segments (kstrl/factory/<id>);
branch names reach git argv in ref position. What remains here drives
the two boundaries end to end: ``decompose_spec`` with a stub architect
that emits a traversal id (the retry prompt carries the validation
error and the second attempt lands) or an unsafe project name in
single-PR mode, and the ``--``-separated git invocations against a
real repository with a bare origin, which still work for legitimate
names and fail closed for option-shaped values.

The per-value tables for ``validate_component_id``,
``validate_branch_name``, ``Manifest.validate_schema`` and
``Manifest.from_prd`` are carried by ``ks decompose`` over a stub
architect emitting each bad id.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.decompose import decompose_spec
from kstrl.git import (
    checkout_existing,
    create_branch_from,
    delete_branch,
    get_diff_names,
    merge_branch,
)
from kstrl.pr import push_branch
from kstrl.ui.plain import PlainUI
from tests.helpers import gitrepo
from tests.helpers.before_spend import no_base_check
from tests.helpers.prompt_calls import architect_call
from tests.helpers.stack_confirmation import PROPOSED_STACK


def _decompose_output(comp_id: str) -> str:
    return json.dumps(
        {
            "stack": PROPOSED_STACK,
            "spec_issues": [],
            "decisions": [],
            "components": [
                {
                    "id": comp_id,
                    "title": "Component",
                    "description": "A component",
                    "dependencies": [],
                    "allowedPaths": [
                        "src/",
                        "tests/",
                        f"scripts/kstrl/feature/{comp_id}/",
                    ],
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Story",
                            "acceptanceCriteria": ["Works", "Tests pass"],
                            "priority": 1,
                            "passes": False,
                            "notes": "",
                        }
                    ],
                }
            ],
        }
    )


class SequenceAgent:
    """Agent returning one canned output per invocation, recording prompts."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self._calls = 0
        self._final_message: str | None = None
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "sequence-agent"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        output = self._outputs[min(self._calls, len(self._outputs) - 1)]
        self._calls += 1
        self._final_message = output
        yield from output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


class TestDecomposeValidationHygiene:
    def test_retry_loop_receives_id_error(self, tmp_path: Path) -> None:
        """A traversal id fails attempt 1; the retry prompt carries the
        validation error verbatim and attempt 2 succeeds."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        agent = SequenceAgent(
            [
                _decompose_output("../../repo"),
                _decompose_output("auth-service"),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test-project",
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
        assert "../../repo" in agent.prompts[1]
        assert manifest.components[0].id == "auth-service"
        assert manifest.components[0].branch_name == "kstrl/factory/auth-service"

    def test_unsafe_project_name_rejected_in_single_pr(
        self,
        tmp_path: Path,
    ) -> None:
        """single_pr derives the branch from project_name (user input);
        an unsafe name is rejected, not sanitized."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        agent = SequenceAgent([_decompose_output("auth-service")])
        with pytest.raises(ValueError, match="Cannot derive a git branch"):
            decompose_spec(
                spec_path=spec_file,
                project_name="my project",
                base_branch="main",
                single_pr=True,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                prompt_call=architect_call(tmp_path),
                timeout=None,
                before_spend=no_base_check,
            )


@pytest.fixture
def git_repo_with_origin(tmp_path: Path) -> Path:
    """A real git repo with one commit and a local bare 'origin' remote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    gitrepo.set_identity(repo)
    (repo / "f.txt").write_text("a\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(repo), str(origin)],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=repo,
        check=True,
    )
    return repo


class TestGitArgvSeparators:
    """The '--'-separated invocations still work for legitimate names
    (no regression) and fail closed for option-shaped values."""

    def test_push_branch_legitimate(self, git_repo_with_origin: Path) -> None:
        repo = git_repo_with_origin
        subprocess.run(
            ["git", "checkout", "-qb", "kstrl/factory/comp-a"],
            cwd=repo,
            check=True,
        )
        # R0.2: push_branch returns None on success, an error otherwise.
        assert push_branch("kstrl/factory/comp-a", repo) is None

    def test_push_branch_option_shape_fails_closed(
        self,
        git_repo_with_origin: Path,
    ) -> None:
        # With "--", "-evil" is an unknown refspec, not a push option.
        assert push_branch("-evil", git_repo_with_origin) is not None

    def test_merge_branch_legitimate(self, git_repo_with_origin: Path) -> None:
        repo = git_repo_with_origin
        subprocess.run(["git", "checkout", "-qb", "feat"], cwd=repo, check=True)
        (repo / "g.txt").write_text("b\n")
        subprocess.run(["git", "add", "g.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        assert merge_branch("feat", cwd=repo) is True

    def test_merge_branch_option_shape_fails_closed(
        self,
        git_repo_with_origin: Path,
    ) -> None:
        assert merge_branch("-evil", cwd=git_repo_with_origin) is False

    def test_delete_branch_legitimate(self, git_repo_with_origin: Path) -> None:
        repo = git_repo_with_origin
        subprocess.run(["git", "branch", "-q", "doomed"], cwd=repo, check=True)
        assert delete_branch("doomed", cwd=repo, force=True) is True

    def test_delete_branch_option_shape_fails_closed(
        self,
        git_repo_with_origin: Path,
    ) -> None:
        assert delete_branch("-evil", cwd=git_repo_with_origin, force=True) is False

    def test_checkout_and_create_branch_from(
        self,
        git_repo_with_origin: Path,
    ) -> None:
        repo = git_repo_with_origin
        assert create_branch_from("kstrl/factory/api", "main", cwd=repo) is True
        assert checkout_existing("main", cwd=repo) is True

    # NOTE: no fail-closed test for checkout_existing("-q"): for git
    # checkout the ref precedes "--", so an option-shaped value is still
    # parsed as an option ("git checkout -q --" exits 0, measured on git
    # 2.47.1). The defense for checkout is validate_branch_name upstream.

    def test_get_diff_names_with_range(self, git_repo_with_origin: Path) -> None:
        repo = git_repo_with_origin
        subprocess.run(["git", "checkout", "-qb", "delta"], cwd=repo, check=True)
        (repo / "h.txt").write_text("c\n")
        subprocess.run(["git", "add", "h.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "delta"], cwd=repo, check=True)
        assert get_diff_names("main", cwd=repo) == ["h.txt"]
