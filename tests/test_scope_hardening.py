"""R1.5 scope-guard hardening tests (H-4, H-5, scope-none-fallthrough).

Three defect classes are covered:

1. Rename-move scope escape (H-5): `git diff --name-only` with git's
   rename detection lists only the DESTINATION of a rename, so
   `git mv protected/gate.py allowed/gate.py` looked in-scope.
   `git.get_diff_names` now reports both sides of renames/copies.
2. allowedPaths content (H-4): DECOMPOSE_PROMPT rule #12 promises the
   harness rejects entries like `.ralph/`; the validator now enforces
   exactly that EXCLUDE list plus structural hazards, and the error
   flows through the decompose retry-with-error loop.
3. PRD-load fail-closed: a PRD that fails to load at the factory's
   scope site fails the diff_scope check (infrastructure error)
   instead of silently disabling it; a PRD legitimately WITHOUT
   allowedPaths still passes with the existing message.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.decompose import (
    _validate_allowed_path_entry,
    _validate_decompose_output,
    decompose_spec,
)
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.git import _parse_name_status_z, get_diff_names
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    check_diff_scope,
    run_mechanical_verification,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo_with_protected_file(tmp_path: Path) -> Path:
    """Real repo whose base commit (mirrored to origin) contains a
    protected file, so a branch-side `git mv` is a true rename against
    the merge base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "protected").mkdir()
    (repo / "allowed").mkdir()
    (repo / "protected" / "gate.py").write_text(
        "def gate() -> bool:\n"
        "    # deliberately long, distinctive content so git's rename\n"
        "    # detection scores this file as a clean R100 move\n"
        "    return check_signature() and check_scope() and check_budget()\n"
    )
    (repo / "allowed" / "app.py").write_text("APP = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(repo), str(origin)],
        cwd=tmp_path, check=True, capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(origin))
    return repo


class TestRenameAwareDiffNames:
    """H-5: rename/copy SOURCES count as changed paths."""

    def test_rename_move_reports_both_sides(
        self, repo_with_protected_file: Path,
    ) -> None:
        repo = repo_with_protected_file
        _git(repo, "checkout", "-qb", "feat")
        _git(repo, "mv", "protected/gate.py", "allowed/gate.py")
        _git(repo, "commit", "-qm", "move gate")

        names = get_diff_names("main", cwd=repo)
        assert "protected/gate.py" in names
        assert "allowed/gate.py" in names

    def test_rename_move_fails_diff_scope(
        self, repo_with_protected_file: Path,
    ) -> None:
        """The empirical H-5 repro: `git mv protected/gate.py
        allowed/gate.py` must no longer pass a scope of allowed/."""
        repo = repo_with_protected_file
        _git(repo, "checkout", "-qb", "feat")
        _git(repo, "mv", "protected/gate.py", "allowed/gate.py")
        _git(repo, "commit", "-qm", "move gate")

        result = check_diff_scope(repo, "main", allowed_paths=["allowed/"])
        assert result.passed is False
        details = "\n".join(result.details)
        assert "protected/gate.py" in details

    def test_plain_changes_still_reported(
        self, repo_with_protected_file: Path,
    ) -> None:
        """Modify/add/delete statuses keep working under --name-status."""
        repo = repo_with_protected_file
        _git(repo, "checkout", "-qb", "feat")
        (repo / "allowed" / "app.py").write_text("APP = 2\n")
        (repo / "allowed" / "new.py").write_text("NEW = 1\n")
        _git(repo, "rm", "-q", "protected/gate.py")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "edits")

        names = get_diff_names("main", cwd=repo)
        assert sorted(names) == [
            "allowed/app.py", "allowed/new.py", "protected/gate.py",
        ]


class TestParseNameStatusZ:
    """Unit coverage for the -z record parser, including copy records
    (C status is heuristic-dependent in real repos, so it is pinned
    here rather than via git)."""

    def test_rename_record_yields_source_and_destination(self) -> None:
        raw = "R100\0protected/gate.py\0allowed/gate.py\0"
        assert _parse_name_status_z(raw) == [
            "protected/gate.py", "allowed/gate.py",
        ]

    def test_copy_record_yields_source_and_destination(self) -> None:
        raw = "C087\0protected/gate.py\0allowed/copy.py\0"
        assert _parse_name_status_z(raw) == [
            "protected/gate.py", "allowed/copy.py",
        ]

    def test_mixed_records_dedupe_and_preserve_order(self) -> None:
        raw = (
            "M\0a.py\0"
            "R100\0old/x.py\0new/x.py\0"
            "M\0a.py\0"
            "A\0b.py\0"
            "D\0gone.py\0"
        )
        assert _parse_name_status_z(raw) == [
            "a.py", "old/x.py", "new/x.py", "b.py", "gone.py",
        ]

    def test_empty_output(self) -> None:
        assert _parse_name_status_z("") == []


def _decompose_payload(allowed_paths: list[str]) -> dict[str, Any]:
    return {
        "components": [
            {
                "id": "comp-a",
                "title": "Component",
                "description": "A component",
                "dependencies": [],
                "allowedPaths": allowed_paths,
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
    }


class TestAllowedPathsContentValidation:
    """H-4: the validator enforces the EXCLUDE list DECOMPOSE_PROMPT
    rule #12 promises, plus structural hazards."""

    @pytest.mark.parametrize("entry", [
        ".ralph/",
        ".github/",
        "kstrl/",
        "src/ralph/",
        "scripts/ralph/",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
    ])
    def test_each_prompt_exclude_entry_rejected(self, entry: str) -> None:
        errors = _validate_decompose_output(_decompose_payload([entry]))
        assert any("allowedPaths" in e and entry in e for e in errors), errors

    @pytest.mark.parametrize("entry", [
        ".ralph",           # no trailing slash
        "./kstrl/",      # leading ./
        "./.ralph",         # both
        "scripts/ralph",    # bare prefix, no slash
    ])
    def test_normalized_variants_rejected(self, entry: str) -> None:
        errors = _validate_decompose_output(_decompose_payload([entry]))
        assert any("allowedPaths" in e for e in errors), errors

    @pytest.mark.parametrize("entry", [
        "/etc/passwd",
        "/src/",
        "..",
        "../sibling/",
        "src/../../escape/",
        "/",
        ".",
        "./",
    ])
    def test_structural_hazards_rejected(self, entry: str) -> None:
        errors = _validate_decompose_output(_decompose_payload([entry]))
        assert any("allowedPaths" in e for e in errors), errors

    @pytest.mark.parametrize("entry", [
        "src/",
        "tests/",
        "lib/",
        "scripts/ralph/feature/comp-a/",
        "docs/pyproject.toml",   # manifest NOT at repo root
        "packages/",             # prefix-similar to an excluded name
        "kstrl_docs/",
    ])
    def test_legitimate_entries_accepted(self, entry: str) -> None:
        assert _validate_allowed_path_entry(entry) is None
        assert _validate_decompose_output(_decompose_payload([entry])) == []

    def test_error_message_names_offending_entry(self) -> None:
        """The error feeds the retry prompt, so the architect must be
        told which entry to drop."""
        error = _validate_allowed_path_entry(".ralph/")
        assert error is not None
        assert ".ralph/" in error
        assert "EXCLUDE" in error


class _SequenceAgent:
    """Agent returning one canned output per invocation."""

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

    def get_final_message(self) -> str | None:
        return self._final_message


class TestExcludeRejectionFlowsThroughRetryLoop:
    def test_retry_prompt_carries_entry_error(self, tmp_path: Path) -> None:
        """First attempt lists `.ralph/`; the retry prompt must contain
        the rejection so the architect can fix it, and the corrected
        second attempt must succeed."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "ralph").mkdir(parents=True)

        bad = json.dumps(_decompose_payload([".ralph/", "src/"]))
        good = json.dumps(_decompose_payload(["src/", "tests/"]))
        agent = _SequenceAgent([bad, good])

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=True,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert ".ralph/" in agent.prompts[1]
        assert [c.id for c in manifest.components] == ["comp-a"]


class TestDiffScopeFailsClosed:
    """PRD-load failure fails the check; unconfigured scope still
    passes with the existing message."""

    def test_allowed_paths_error_fails_check(self, tmp_path: Path) -> None:
        result = check_diff_scope(
            tmp_path, "main",
            allowed_paths=None,
            allowed_paths_error="PRD failed to parse: bad JSON",
        )
        assert result.passed is False
        assert result.name == "diff_scope"
        assert "failing closed" in result.message
        assert any("PRD failed to parse" in d for d in result.details)

    def test_error_wins_even_with_allowed_paths(self, tmp_path: Path) -> None:
        """A half-loaded state (paths recovered but an error was
        recorded) must still fail closed rather than judge scope on
        possibly-stale paths."""
        result = check_diff_scope(
            tmp_path, "main",
            allowed_paths=["src/"],
            allowed_paths_error="PRD not found: prd.json",
        )
        assert result.passed is False

    def test_unconfigured_scope_still_passes(self, tmp_path: Path) -> None:
        result = check_diff_scope(tmp_path, "main", allowed_paths=None)
        assert result.passed is True
        assert result.message == "No scope constraints (allowed_paths not set)"

    def test_run_mechanical_verification_forwards_error(
        self, tmp_path: Path,
    ) -> None:
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "T",
                "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
        config = VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        verification = run_mechanical_verification(
            tmp_path, prd_path, "main", None, config,
            allowed_paths_error="PRD failed to parse: bad JSON",
        )
        diff_scope = next(
            c for c in verification.checks if c.name == "diff_scope"
        )
        assert diff_scope.passed is False
        assert verification.passed is False


def _factory_fixtures(tmp_path: Path) -> tuple[Manifest, FactoryConfig, KstrlConfig]:
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a", "Component A", "Desc",
                [], "scripts/ralph/feature/comp-a/prd.json",
                "ralph/factory/comp-a",
            ),
        ],
    )
    config = FactoryConfig(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=ralph_dir / "prompt.md",
        prd_file=ralph_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    return manifest, config, base


class TestFactoryScopeSiteFailsClosed:
    """The factory's PRD-load site forwards load failures into the
    diff_scope check instead of swallowing them into None."""

    def test_corrupt_prd_forwards_error(self, tmp_path: Path) -> None:
        manifest, config, base = _factory_fixtures(tmp_path)
        feature_dir = tmp_path / "scripts" / "ralph" / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text("{not valid json")

        captured: dict[str, Any] = {}

        def spy_rmv(
            worktree_path: Path,
            prd_path: Path,
            base_branch: str,
            allowed_paths: list[str] | None,
            verify_config: VerifyConfig,
            allowed_paths_error: str | None = None,
            fixtures_config: object | None = None,
            component_id: str | None = None,
        ) -> VerificationResult:
            captured["allowed_paths"] = allowed_paths
            captured["allowed_paths_error"] = allowed_paths_error
            return VerificationResult(
                passed=True, checks=[CheckResult("diff_scope", True, "ok")],
            )

        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch("kstrl.factory._run_component", return_value=success),
            patch(
                "kstrl.factory.run_mechanical_verification",
                side_effect=spy_rmv,
            ),
        ):
            run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)

        assert captured["allowed_paths"] is None
        assert captured["allowed_paths_error"] is not None
        assert "PRD failed to parse" in str(captured["allowed_paths_error"])

    def test_missing_prd_forwards_error(self, tmp_path: Path) -> None:
        manifest, config, base = _factory_fixtures(tmp_path)
        # No PRD file written at all.
        captured: dict[str, Any] = {}

        def spy_rmv(
            worktree_path: Path,
            prd_path: Path,
            base_branch: str,
            allowed_paths: list[str] | None,
            verify_config: VerifyConfig,
            allowed_paths_error: str | None = None,
            fixtures_config: object | None = None,
            component_id: str | None = None,
        ) -> VerificationResult:
            captured["allowed_paths_error"] = allowed_paths_error
            return VerificationResult(
                passed=True, checks=[CheckResult("diff_scope", True, "ok")],
            )

        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch("kstrl.factory._run_component", return_value=success),
            patch(
                "kstrl.factory.run_mechanical_verification",
                side_effect=spy_rmv,
            ),
        ):
            run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)

        assert "PRD not found" in str(captured["allowed_paths_error"])

    def test_legacy_prd_without_allowed_paths_stays_unconstrained(
        self, tmp_path: Path,
    ) -> None:
        """The legitimate-disable case: a PRD that loads fine but has
        no allowedPaths field must NOT produce an error."""
        manifest, config, base = _factory_fixtures(tmp_path)
        feature_dir = tmp_path / "scripts" / "ralph" / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "T",
                "acceptanceCriteria": ["AC"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))

        captured: dict[str, Any] = {}

        def spy_rmv(
            worktree_path: Path,
            prd_path: Path,
            base_branch: str,
            allowed_paths: list[str] | None,
            verify_config: VerifyConfig,
            allowed_paths_error: str | None = None,
            fixtures_config: object | None = None,
            component_id: str | None = None,
        ) -> VerificationResult:
            captured["allowed_paths"] = allowed_paths
            captured["allowed_paths_error"] = allowed_paths_error
            return VerificationResult(
                passed=True, checks=[CheckResult("diff_scope", True, "ok")],
            )

        success = ComponentResult("comp-a", success=True, iterations=1)
        with (
            patch("kstrl.factory._run_component", return_value=success),
            patch(
                "kstrl.factory.run_mechanical_verification",
                side_effect=spy_rmv,
            ),
        ):
            run_factory(manifest, config, base, PlainUI(no_color=True), tmp_path)

        assert captured["allowed_paths"] is None
        assert captured["allowed_paths_error"] is None
