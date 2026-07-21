"""R0.6 input hygiene: LLM-emitted component ids and branch names.

Component ids become filesystem path segments (.kstrl/worktrees/<id>,
scripts/kstrl/feature/<id>) and branch segments (kstrl/factory/<id>);
branch names reach git argv in ref position. These tests prove that
traversal ids, option-injection branch names, and unicode confusables
are rejected at every parse boundary, and that the legitimate shapes
used across the codebase still pass.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl import pr as pr_module
from kstrl.decompose import _validate_decompose_output, decompose_spec
from kstrl.git import (
    checkout_existing,
    create_branch_from,
    delete_branch,
    get_diff_names,
    merge_branch,
)
from kstrl.manifest import (
    Component,
    Manifest,
    validate_branch_name,
    validate_component_id,
)
from kstrl.pr import push_branch
from kstrl.ui.plain import PlainUI

# Unicode dash confusables: non-breaking hyphen, minus sign, en dash.
NB_HYPHEN = "‑"
MINUS_SIGN = "−"
EN_DASH = "–"


REJECTED_COMPONENT_IDS = [
    "../../repo",
    "..",
    "a/../b",
    "foo/bar",
    "/etc",
    "-foo",
    "--force",
    ".hidden",
    "_leading-underscore",
    "Uppercase",
    "foo bar",
    "foo\tbar",
    "foo\nbar",
    "foo..bar",
    "trailing-dot.",
    "collides.lock",
    "",
    "a" * 65,
    f"auth{NB_HYPHEN}service",
    f"auth{MINUS_SIGN}service",
    f"auth{EN_DASH}service",
    "føo",
]

ACCEPTED_COMPONENT_IDS = [
    "main",  # Manifest.from_prd
    "auth-service",
    "comp-a",
    "database",
    "api",
    "c1",
    "a",
    "0start-digit",
    "api.v2",
    "data_models",
    "a" * 64,
]

REJECTED_BRANCH_NAMES = [
    "-evil",
    "--force",
    "-u",
    f"{NB_HYPHEN}evil",
    f"{MINUS_SIGN}evil",
    f"br{EN_DASH}anch",
    "..",
    "a..b",
    "kstrl/../main",
    " main",
    "main ",
    "my branch",
    "branch\tname",
    "branch\nname",
    "re:branch",
    "/leading-slash",
    "trailing-slash/",
    "a//b",
    ".hidden",
    "kstrl/.hidden",
    "branch.",
    "branch.lock",
    "",
    "a" * 201,
]

ACCEPTED_BRANCH_NAMES = [
    "main",
    "master",
    "develop",
    "kstrl/run",  # ks run default
    "kstrl/factory/auth-service",
    "kstrl/factory/comp-a",
    "kstrl/auth-feature",
    "kstrl/c1",
    "test-branch",
    "feature/foo_bar",
    "release-1.2.3",
    "kstrl/contract-0-20260713",  # contract temp branches
    "a" * 200,
]


class TestValidateComponentId:
    @pytest.mark.parametrize("comp_id", REJECTED_COMPONENT_IDS)
    def test_rejected(self, comp_id: str) -> None:
        error = validate_component_id(comp_id)
        assert error is not None
        assert "component id" in error

    @pytest.mark.parametrize("comp_id", ACCEPTED_COMPONENT_IDS)
    def test_accepted(self, comp_id: str) -> None:
        assert validate_component_id(comp_id) is None

    def test_error_is_actionable_for_retry_loop(self) -> None:
        """The message names the offending id and states the rule, so
        the decompose retry loop can feed it back to the architect."""
        error = validate_component_id("../../repo")
        assert error is not None
        assert "../../repo" in error
        assert "must match" in error


class TestValidateBranchName:
    @pytest.mark.parametrize("branch", REJECTED_BRANCH_NAMES)
    def test_rejected(self, branch: str) -> None:
        error = validate_branch_name(branch)
        assert error is not None
        assert "branch name" in error

    @pytest.mark.parametrize("branch", ACCEPTED_BRANCH_NAMES)
    def test_accepted(self, branch: str) -> None:
        assert validate_branch_name(branch) is None

    def test_option_injection_message_explains_risk(self) -> None:
        error = validate_branch_name("-evil")
        assert error is not None
        assert "option" in error


def _manifest_data(
    comp_id: str = "comp-a",
    branch_name: str = "kstrl/factory/comp-a",
    base_branch: str = "main",
) -> dict[str, object]:
    return {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "test-project",
        "baseBranch": base_branch,
        "singlePr": False,
        "components": [
            {
                "id": comp_id,
                "title": "Component A",
                "description": "A component",
                "dependencies": [],
                "prdPath": f"scripts/kstrl/feature/{comp_id}/prd.json",
                "branchName": branch_name,
            }
        ],
    }


class TestManifestSchemaHygiene:
    def test_legitimate_manifest_passes(self) -> None:
        assert Manifest.validate_schema(_manifest_data()) == []

    @pytest.mark.parametrize("comp_id", ["../../repo", "foo/bar", "-foo"])
    def test_traversal_id_rejected(self, comp_id: str) -> None:
        errors = Manifest.validate_schema(_manifest_data(comp_id=comp_id))
        assert any("components[0].id" in e for e in errors)

    @pytest.mark.parametrize("branch", ["-evil", "a..b", "my branch"])
    def test_bad_branch_name_rejected(self, branch: str) -> None:
        errors = Manifest.validate_schema(_manifest_data(branch_name=branch))
        assert any("components[0].branchName" in e for e in errors)

    @pytest.mark.parametrize("base", ["-evil", "a..b"])
    def test_bad_base_branch_rejected(self, base: str) -> None:
        errors = Manifest.validate_schema(_manifest_data(base_branch=base))
        assert any(e.startswith("baseBranch:") for e in errors)

    def test_load_rejects_traversal_id(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(_manifest_data(comp_id="../../repo")))
        with pytest.raises(ValueError, match="Invalid manifest schema"):
            Manifest.load(path)


class TestFromPrdHygiene:
    def test_legitimate_branch_accepted(self, tmp_path: Path) -> None:
        manifest = Manifest.from_prd(
            prd_path=tmp_path / "prd.json", branch="kstrl/auth",
        )
        assert manifest.components[0].branch_name == "kstrl/auth"

    @pytest.mark.parametrize("branch", ["-evil", "my branch", "a..b"])
    def test_bad_branch_rejected(self, tmp_path: Path, branch: str) -> None:
        with pytest.raises(ValueError, match="Invalid branch name"):
            Manifest.from_prd(prd_path=tmp_path / "prd.json", branch=branch)

    def test_bad_base_branch_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid base branch"):
            Manifest.from_prd(
                prd_path=tmp_path / "prd.json",
                branch="kstrl/auth",
                base_branch="-evil",
            )


def _decompose_output(comp_id: str) -> str:
    return json.dumps({
        "components": [
            {
                "id": comp_id,
                "title": "Component",
                "description": "A component",
                "dependencies": [],
                "allowedPaths": [
                    "src/", "tests/", f"scripts/kstrl/feature/{comp_id}/",
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
        ]
    })


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

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        output = self._outputs[min(self._calls, len(self._outputs) - 1)]
        self._calls += 1
        self._final_message = output
        yield from output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


class TestDecomposeValidationHygiene:
    @pytest.mark.parametrize(
        "comp_id", ["../../repo", "foo/bar", "-foo", "Uppercase", ".."],
    )
    def test_bad_id_rejected(self, comp_id: str) -> None:
        data = json.loads(_decompose_output(comp_id))
        errors = _validate_decompose_output(data)
        assert any("components[0].id" in e for e in errors)

    def test_legitimate_output_accepted(self) -> None:
        data = json.loads(_decompose_output("auth-service"))
        assert _validate_decompose_output(data) == []

    def test_retry_loop_receives_id_error(self, tmp_path: Path) -> None:
        """A traversal id fails attempt 1; the retry prompt carries the
        validation error verbatim and attempt 2 succeeds."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        agent = SequenceAgent([
            _decompose_output("../../repo"),
            _decompose_output("auth-service"),
        ])
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test-project",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "../../repo" in agent.prompts[1]
        assert manifest.components[0].id == "auth-service"
        assert manifest.components[0].branch_name == "kstrl/factory/auth-service"

    def test_unsafe_project_name_rejected_in_single_pr(
        self, tmp_path: Path,
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
            )


@pytest.fixture
def git_repo_with_origin(tmp_path: Path) -> Path:
    """A real git repo with one commit and a local bare 'origin' remote."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("a\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(repo), str(origin)],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True,
    )
    return repo


class TestGitArgvSeparators:
    """The '--'-separated invocations still work for legitimate names
    (no regression) and fail closed for option-shaped values."""

    def test_push_branch_legitimate(self, git_repo_with_origin: Path) -> None:
        repo = git_repo_with_origin
        subprocess.run(
            ["git", "checkout", "-qb", "kstrl/factory/comp-a"],
            cwd=repo, check=True,
        )
        # R0.2: push_branch returns None on success, an error otherwise.
        assert push_branch("kstrl/factory/comp-a", repo) is None

    def test_push_branch_option_shape_fails_closed(
        self, git_repo_with_origin: Path,
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
        self, git_repo_with_origin: Path,
    ) -> None:
        assert merge_branch("-evil", cwd=git_repo_with_origin) is False

    def test_delete_branch_legitimate(self, git_repo_with_origin: Path) -> None:
        repo = git_repo_with_origin
        subprocess.run(["git", "branch", "-q", "doomed"], cwd=repo, check=True)
        assert delete_branch("doomed", cwd=repo, force=True) is True

    def test_delete_branch_option_shape_fails_closed(
        self, git_repo_with_origin: Path,
    ) -> None:
        assert delete_branch("-evil", cwd=git_repo_with_origin, force=True) is False

    def test_checkout_and_create_branch_from(
        self, git_repo_with_origin: Path,
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


class TestPrArgvShapes:
    """Argv-shape assertions for the gh invocation: --head=/--base= bind
    branch values to their flags."""

    def test_create_component_pr_uses_equals_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> object:
            captured.append(argv)
            return type(
                "R", (),
                {
                    "returncode": 0,
                    "stdout": "https://github.com/o/r/pull/7\n",
                    "stderr": "",
                },
            )()

        monkeypatch.setattr(pr_module.subprocess, "run", fake_run)

        component = Component(
            id="comp-a",
            title="Component A",
            description="A component",
            dependencies=[],
            prd_path="scripts/kstrl/feature/comp-a/prd.json",
            branch_name="kstrl/factory/comp-a",
        )
        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="test-project",
            base_branch="main",
            single_pr=False,
            components=[component],
        )

        pr_number, pr_url = pr_module.create_component_pr(
            component, manifest, tmp_path,
        )

        assert pr_number == 7
        assert pr_url == "https://github.com/o/r/pull/7"
        (argv,) = captured
        assert "--head=kstrl/factory/comp-a" in argv
        assert "--base=main" in argv

    def test_push_branch_uses_separator(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> object:
            captured.append(argv)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(pr_module.subprocess, "run", fake_run)

        # R0.2: push_branch returns None on success, an error otherwise.
        assert push_branch("kstrl/factory/comp-a", tmp_path) is None
        (argv,) = captured
        assert argv.index("--") < argv.index("kstrl/factory/comp-a")
