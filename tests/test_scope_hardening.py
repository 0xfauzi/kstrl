"""R1.5 scope-guard hardening tests (H-4, H-5, scope-none-fallthrough).

Three defect classes are covered:

1. Rename-move scope escape (H-5): `git diff --name-only` with git's
   rename detection lists only the DESTINATION of a rename, so
   `git mv protected/gate.py allowed/gate.py` looked in-scope.
   `git.get_diff_names` now reports both sides of renames/copies.
2. allowedPaths content (H-4): DECOMPOSE_PROMPT rule #12 promises the
   harness rejects entries like `.kstrl/`; the validator now enforces
   exactly that EXCLUDE list plus structural hazards, and the error
   flows through the decompose retry-with-error loop.
3. PRD-load fail-closed: a PRD that fails to load at the factory's
   scope site fails the diff_scope check (infrastructure error)
   instead of silently disabling it; a PRD legitimately WITHOUT
   allowedPaths still passes with the existing message.
"""

from __future__ import annotations

import io
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
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    _preflight_component_scope,
    run_factory,
)
from kstrl.git import _parse_name_status_z, get_diff_names
from kstrl.manifest import Component, Manifest
from kstrl.scope import RunScope
from kstrl.ui.plain import PlainUI
from kstrl.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    _scope_checks,
    check_diff_scope,
    check_scope_source,
    run_mechanical_verification,
)
from tests.helpers.component_prd import PASSING_STORY, write_component_prd


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
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(origin))
    return repo


class TestRenameAwareDiffNames:
    """H-5: rename/copy SOURCES count as changed paths."""

    def test_rename_move_reports_both_sides(
        self,
        repo_with_protected_file: Path,
    ) -> None:
        repo = repo_with_protected_file
        _git(repo, "checkout", "-qb", "feat")
        _git(repo, "mv", "protected/gate.py", "allowed/gate.py")
        _git(repo, "commit", "-qm", "move gate")

        names = get_diff_names("main", cwd=repo)
        assert "protected/gate.py" in names
        assert "allowed/gate.py" in names

    def test_rename_move_fails_diff_scope(
        self,
        repo_with_protected_file: Path,
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
        self,
        repo_with_protected_file: Path,
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
            "allowed/app.py",
            "allowed/new.py",
            "protected/gate.py",
        ]


class TestParseNameStatusZ:
    """Unit coverage for the -z record parser, including copy records
    (C status is heuristic-dependent in real repos, so it is pinned
    here rather than via git)."""

    def test_rename_record_yields_source_and_destination(self) -> None:
        raw = "R100\0protected/gate.py\0allowed/gate.py\0"
        assert _parse_name_status_z(raw) == [
            "protected/gate.py",
            "allowed/gate.py",
        ]

    def test_copy_record_yields_source_and_destination(self) -> None:
        raw = "C087\0protected/gate.py\0allowed/copy.py\0"
        assert _parse_name_status_z(raw) == [
            "protected/gate.py",
            "allowed/copy.py",
        ]

    def test_mixed_records_dedupe_and_preserve_order(self) -> None:
        raw = "M\0a.py\0R100\0old/x.py\0new/x.py\0M\0a.py\0A\0b.py\0D\0gone.py\0"
        assert _parse_name_status_z(raw) == [
            "a.py",
            "old/x.py",
            "new/x.py",
            "b.py",
            "gone.py",
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

    @pytest.mark.parametrize(
        "entry",
        [
            ".kstrl/",
            ".github/",
            "kstrl/",
            "scripts/kstrl/",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
        ],
    )
    def test_each_prompt_exclude_entry_rejected(self, entry: str) -> None:
        errors = _validate_decompose_output(_decompose_payload([entry]))
        assert any("allowedPaths" in e and entry in e for e in errors), errors

    @pytest.mark.parametrize(
        "entry",
        [
            ".kstrl",  # no trailing slash
            "./kstrl/",  # leading ./
            "./.kstrl",  # both
            "scripts/kstrl",  # bare prefix, no slash
        ],
    )
    def test_normalized_variants_rejected(self, entry: str) -> None:
        errors = _validate_decompose_output(_decompose_payload([entry]))
        assert any("allowedPaths" in e for e in errors), errors

    @pytest.mark.parametrize(
        "entry",
        [
            "/etc/passwd",
            "/src/",
            "..",
            "../sibling/",
            "src/../../escape/",
            "/",
            ".",
            "./",
        ],
    )
    def test_structural_hazards_rejected(self, entry: str) -> None:
        errors = _validate_decompose_output(_decompose_payload([entry]))
        assert any("allowedPaths" in e for e in errors), errors

    @pytest.mark.parametrize(
        "entry",
        [
            "src/",
            "tests/",
            "lib/",
            "scripts/kstrl/feature/comp-a/",
            "docs/pyproject.toml",  # manifest NOT at repo root
            "packages/",  # prefix-similar to an excluded name
            "kstrl_docs/",
        ],
    )
    def test_legitimate_entries_accepted(self, entry: str) -> None:
        assert _validate_allowed_path_entry(entry) is None
        assert _validate_decompose_output(_decompose_payload([entry])) == []

    def test_error_message_names_offending_entry(self) -> None:
        """The error feeds the retry prompt, so the architect must be
        told which entry to drop."""
        error = _validate_allowed_path_entry(".kstrl/")
        assert error is not None
        assert ".kstrl/" in error
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
        """First attempt lists `.kstrl/`; the retry prompt must contain
        the rejection so the architect can fix it, and the corrected
        second attempt must succeed."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        bad = json.dumps(_decompose_payload([".kstrl/", "src/"]))
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
        assert ".kstrl/" in agent.prompts[1]
        assert [c.id for c in manifest.components] == ["comp-a"]


class TestDiffScopeFailsClosed:
    """PRD-load failure fails a check of its OWN (#294); unconfigured
    scope still passes with the existing message."""

    def test_allowed_paths_error_fails_check(self) -> None:
        result = check_scope_source("PRD failed to parse: bad JSON")
        assert result.passed is False
        assert result.name == "scope_source"
        assert "failing closed" in result.message
        assert any("PRD failed to parse" in d for d in result.details)

    def test_error_wins_even_with_allowed_paths(self, tmp_path: Path) -> None:
        """A half-loaded state (paths recovered but an error was
        recorded) must still fail closed rather than judge scope on
        possibly-stale paths. The decision moved to ``_scope_checks``
        with #294, so it is exercised through the entry point."""
        result = _scope_checks(
            tmp_path,
            "main",
            allowed_paths=["src/"],
            allowed_paths_error="PRD not found: prd.json",
            harness_paths=None,
            compare=True,
        )
        assert [c.name for c in result] == ["scope_source"]
        assert result[0].passed is False

    def test_unconfigured_scope_still_passes(self, tmp_path: Path) -> None:
        result = check_diff_scope(tmp_path, "main", allowed_paths=None)
        assert result.passed is True
        assert result.message == "No scope constraints (allowed_paths not set)"

    def test_run_mechanical_verification_forwards_error(
        self,
        tmp_path: Path,
    ) -> None:
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "T",
                            "acceptanceCriteria": ["AC"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        config = VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        )
        verification = run_mechanical_verification(
            tmp_path,
            prd_path,
            "main",
            None,
            config,
            allowed_paths_error="PRD failed to parse: bad JSON",
        )
        names = [c.name for c in verification.checks]
        assert "diff_scope" not in names, "the diff comparison had nothing to compare"
        scope_source = next(c for c in verification.checks if c.name == "scope_source")
        assert scope_source.passed is False
        assert verification.passed is False


def _factory_fixtures(tmp_path: Path) -> tuple[Manifest, FactoryConfig, KstrlConfig]:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                "comp-a",
                "Component A",
                "Desc",
                [],
                "scripts/kstrl/feature/comp-a/prd.json",
                "kstrl/factory/comp-a",
            ),
        ],
    )
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=False,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    return manifest, config, base


#: Where _factory_fixtures' single component says its PRD is.
COMP_PRD_REL = "scripts/kstrl/feature/comp-a/prd.json"


class TestFactoryScopeSiteFailsClosed:
    """A pre-run PRD that will not read stops the run BEFORE it spends.

    It used to be forwarded into Phase 1 as ``allowed_paths_error`` and
    failed the diff_scope check closed there, which is still the
    backstop for any caller that reaches Phase 1 with one. But since
    #269 the scope is a plan-time snapshot, fixed for the life of the
    run, so that verdict can never change: the component would pay a
    full engineer loop and fail identically on every retry - #264's
    measured $14.49 and 41 minutes. The preflight refuses it instead
    (#293 review).
    """

    def _refuse(
        self,
        tmp_path: Path,
        prd_body: str | None = None,
    ) -> tuple[FactoryResult, list[str], io.StringIO]:
        """Run the factory and report what it spent. Nothing, ideally.

        ``prd_body`` is written AFTER the fixtures, which own the
        creation of scripts/kstrl.
        """
        manifest, config, base = _factory_fixtures(tmp_path)
        if prd_body is not None:
            write_component_prd(tmp_path, COMP_PRD_REL, body=prd_body)
        called: list[str] = []
        out = io.StringIO()

        def spy_component(*args: Any, **kwargs: Any) -> ComponentResult:
            called.append("engineer")
            return ComponentResult("comp-a", success=True, iterations=1)

        def spy_rmv(*args: Any, **kwargs: Any) -> VerificationResult:
            called.append("verify")
            return VerificationResult(passed=True, checks=[])

        with (
            patch("kstrl.factory._run_component", side_effect=spy_component),
            patch("kstrl.factory.run_mechanical_verification", side_effect=spy_rmv),
        ):
            result = run_factory(
                manifest,
                config,
                base,
                PlainUI(no_color=True, file=out),
                tmp_path,
            )
        return result, called, out

    def test_a_corrupt_prd_is_refused_before_any_engineer_call(
        self,
        tmp_path: Path,
    ) -> None:
        result, called, out = self._refuse(tmp_path, "{not valid json")

        assert result.exit_code == 2
        assert called == [], "the run paid for work it could never pass"
        printed = out.getvalue()
        assert "Refusing to run: components cannot pass the scope check" in printed
        assert "PRD failed to parse" in printed
        assert "scripts/kstrl/feature/comp-a/prd.json" in printed

    def test_a_missing_prd_is_refused_before_any_engineer_call(
        self,
        tmp_path: Path,
    ) -> None:
        result, called, out = self._refuse(tmp_path)

        assert result.exit_code == 2
        assert called == []
        assert "PRD not found" in out.getvalue()

    def test_the_run_wide_flag_does_not_paper_over_it(
        self,
        tmp_path: Path,
    ) -> None:
        """The refusal survives the flag being present (#293 review).

        Only the preflight's half is asserted here.
        ``test_scope_snapshot`` owns what ``ComponentScope.resolve``
        decides and why; this is the layer that has to act on it, and
        it acts on the SAME snapshot object it is handed rather than
        one it resolved for itself.
        """
        manifest, config, base = _factory_fixtures(tmp_path)
        write_component_prd(tmp_path, COMP_PRD_REL, body="{not valid json")
        base.allowed_paths = ["src/"]

        run_scope = RunScope.resolve(manifest, tmp_path, base)
        assert run_scope.for_component("comp-a").source == "unresolved"

        errors = _preflight_component_scope(manifest, run_scope)
        assert len(errors) == 1
        assert "cannot stand in for it" in errors[0]

    def test_legacy_prd_without_allowed_paths_stays_unconstrained(
        self,
        tmp_path: Path,
    ) -> None:
        """The legitimate-disable case: a PRD that loads fine but has
        no allowedPaths field must NOT produce an error."""
        manifest, config, base = _factory_fixtures(tmp_path)
        write_component_prd(tmp_path, COMP_PRD_REL, stories=[PASSING_STORY])

        captured: dict[str, Any] = {}

        def spy_rmv(*args: Any, **kwargs: Any) -> VerificationResult:
            # *args/**kwargs, not the real 13-parameter signature
            # restated: a stub that repeats it fails at call time
            # with a TypeError that reads as a test bug the next time a
            # keyword is added, and this test asserts on three values.
            # allowed_paths is positional index 3 at the one call site
            # (pipeline._phase_verify).
            captured["allowed_paths"] = args[3]
            captured["allowed_paths_error"] = kwargs.get("allowed_paths_error")
            captured["harness_paths"] = kwargs.get("harness_paths")
            return VerificationResult(
                passed=True,
                checks=[CheckResult("diff_scope", True, "ok")],
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
