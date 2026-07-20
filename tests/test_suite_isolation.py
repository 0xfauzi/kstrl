"""Tests for the R4.1 suite-isolation fixtures in conftest.py.

Proves two things independently:

1. The autouse redirect works: code writing through the DEFAULT
   (CWD-relative) evolution/experiments/knowledge paths lands in tmp_path,
   not in the repository's real ``.kstrl/``.
2. The session guard works: a nested pytest run whose test mutates the
   guarded ``.kstrl/`` exits nonzero with a loud message (and a control
   run that does not mutate exits zero).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kstrl.evolution import EvolutionConfig, EvolutionJournal
from kstrl.factory import FactoryResult
from kstrl.knowledge import Fact, KnowledgeConfig, write_facts
from kstrl.manifest import Component, ComponentStatus, Manifest
from tests.conftest import (
    RALPH_ENV_PREFIXES,
    REPO_ROOT,
    _clear_ralph_env,
    describe_snapshot_diff,
    snapshot_ralph_dir,
)

_RUN_MARKER = "run-isolation-proof"


def _make_manifest_with_one_component() -> Manifest:
    component = Component(
        id="iso",
        title="Component iso",
        description="isolation probe",
        dependencies=[],
        prd_path="prd/iso.json",
        branch_name="kstrl/iso",
        status=ComponentStatus.COMPLETED.value,
        error="",
        retries=0,
        duration_seconds=1.0,
        iteration_count=1,
    )
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="isolation-proof",
        base_branch="main",
        single_pr=False,
        components=[component],
    )


class TestRedirect:
    """The autouse isolate_ralph_state fixture redirects default writes."""

    def test_cwd_is_redirected_to_tmp_path(self, tmp_path: Path) -> None:
        assert Path.cwd() == tmp_path
        assert Path.cwd() != REPO_ROOT

    def test_default_evolution_journal_writes_land_in_tmp(
        self, tmp_path: Path
    ) -> None:
        """Deliberate journal write through the DEFAULT config.

        This is the exact shape of the historical pollution: run_factory
        constructs ``EvolutionConfig()`` (relative ``.kstrl/...`` paths)
        and records the run. With the redirect the entries must land under
        tmp_path and never in the repo's real journal.
        """
        config = EvolutionConfig()  # defaults: relative .kstrl/ paths
        journal = EvolutionJournal(config)
        manifest = _make_manifest_with_one_component()
        result = FactoryResult(completed=["iso"], failed=[], skipped=[])

        journal.record_run(_RUN_MARKER, manifest, result)

        tmp_journal = tmp_path / ".kstrl" / "evolution.jsonl"
        tmp_experiments = tmp_path / ".kstrl" / "experiments.tsv"
        assert tmp_journal.exists()
        assert _RUN_MARKER in tmp_journal.read_text()
        assert tmp_experiments.exists()
        assert _RUN_MARKER in tmp_experiments.read_text()

        repo_journal = REPO_ROOT / ".kstrl" / "evolution.jsonl"
        if repo_journal.exists():  # archived away by R4.1; belt and braces
            assert _RUN_MARKER not in repo_journal.read_text()

    def test_default_knowledge_root_resolves_and_writes_into_tmp(
        self, tmp_path: Path
    ) -> None:
        # load(None) resolves against Path.cwd(), which the redirect owns.
        assert KnowledgeConfig.load(None).knowledge_root == (
            tmp_path / ".kstrl" / "knowledge"
        )

        fact = Fact(
            id="iso-fact-1",
            component_id="iso",
            created_iter=1,
            created_run_id=_RUN_MARKER,
            scope="component",
            evidence=["src/iso.py"],
            confidence="asserted",
            claim="isolation probe fact",
        )
        written = write_facts(
            [fact],
            KnowledgeConfig().knowledge_root,  # default: relative path
            component_id="iso",
            run_id=_RUN_MARKER,
        )
        assert written == 1
        component_dir = tmp_path / ".kstrl" / "knowledge" / "iso"
        assert component_dir.is_dir()
        assert any(component_dir.rglob("*.md"))

    def test_ambient_config_env_is_cleared_for_every_test(self) -> None:
        for prefix in RALPH_ENV_PREFIXES:
            leaked = [var for var in os.environ if var.startswith(prefix)]
            assert leaked == [], f"ambient {prefix}* env leaked into test"

    def test_clear_ralph_env_covers_every_documented_family(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        family_probes = [prefix + "PROBE" for prefix in RALPH_ENV_PREFIXES]
        for var in family_probes:
            monkeypatch.setenv(var, "leaked")
        monkeypatch.setenv("RALPH_BRANCH", "leaked")  # legacy exact name
        monkeypatch.setenv("UNRELATED_PROBE", "kept")

        inner = pytest.MonkeyPatch()
        try:
            _clear_ralph_env(inner)
            for var in [*family_probes, "RALPH_BRANCH"]:
                assert var not in os.environ, f"{var} survived _clear_ralph_env"
            assert os.environ["UNRELATED_PROBE"] == "kept"
        finally:
            inner.undo()


class TestGuardHelpers:
    """Unit tests for the snapshot/diff logic the session guard runs on."""

    def test_missing_dir_snapshots_empty_on_both_sides(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope" / ".kstrl"
        assert snapshot_ralph_dir(missing) == snapshot_ralph_dir(missing) == {}

    def test_detects_created_modified_and_deleted_entries(
        self, tmp_path: Path
    ) -> None:
        ralph_dir = tmp_path / ".kstrl"
        ralph_dir.mkdir()
        (ralph_dir / "evolution.jsonl").write_text('{"run_id": "real"}\n')
        (ralph_dir / "experiments.tsv").write_text("header\n")
        before = snapshot_ralph_dir(ralph_dir)

        (ralph_dir / "evolution.jsonl").write_text('{"run_id": "junk"}\n')
        (ralph_dir / "experiments.tsv").unlink()
        (ralph_dir / "proposals").mkdir()
        after = snapshot_ralph_dir(ralph_dir)

        assert before != after
        diff = describe_snapshot_diff(before, after)
        assert "modified: evolution.jsonl" in diff
        assert "deleted:  experiments.tsv" in diff
        assert "created:  proposals" in diff

    def test_identical_content_compares_equal(self, tmp_path: Path) -> None:
        ralph_dir = tmp_path / ".kstrl"
        ralph_dir.mkdir()
        (ralph_dir / "evolution.jsonl").write_text('{"run_id": "real"}\n')
        before = snapshot_ralph_dir(ralph_dir)
        # Rewrite the same bytes: mtime moves, content does not.
        (ralph_dir / "evolution.jsonl").write_text('{"run_id": "real"}\n')
        assert snapshot_ralph_dir(ralph_dir) == before


class TestGuardEndToEnd:
    """Exercise the real guard fixture via a nested pytest session."""

    def _run_nested_pytest(
        self, tmp_path: Path, guarded_repo: Path, test_body: str
    ) -> subprocess.CompletedProcess[str]:
        nested = tmp_path / "nested_suite"
        nested.mkdir()
        shutil.copy(Path(__file__).parent / "conftest.py", nested / "conftest.py")
        (nested / "test_nested.py").write_text(textwrap.dedent(test_body))

        env = os.environ.copy()
        env["KSTRL_SUITE_GUARD_ROOT"] = str(guarded_repo)
        env["KSTRL_GUARDED_JOURNAL"] = str(
            guarded_repo / ".kstrl" / "evolution.jsonl"
        )
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(nested), "-q",
             "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    @pytest.fixture
    def guarded_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "synthetic_repo"
        (repo / ".kstrl").mkdir(parents=True)
        (repo / ".kstrl" / "evolution.jsonl").write_text(
            '{"run_id": "real-data"}\n'
        )
        return repo

    def test_guard_fails_the_run_when_a_test_mutates_ralph_dir(
        self, tmp_path: Path, guarded_repo: Path
    ) -> None:
        result = self._run_nested_pytest(
            tmp_path,
            guarded_repo,
            """
            import os

            def test_mutates_the_guarded_journal() -> None:
                with open(os.environ["KSTRL_GUARDED_JOURNAL"], "a") as f:
                    f.write('{"run_id": "pollution"}\\n')
            """,
        )
        assert result.returncode != 0, (
            f"guard did not fail the run\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "mutated the repository's real .kstrl/" in result.stdout
        assert "modified: evolution.jsonl" in result.stdout

    def test_guard_passes_a_run_that_leaves_ralph_dir_alone(
        self, tmp_path: Path, guarded_repo: Path
    ) -> None:
        result = self._run_nested_pytest(
            tmp_path,
            guarded_repo,
            """
            def test_touches_nothing() -> None:
                assert True
            """,
        )
        assert result.returncode == 0, (
            f"guard false-positived on a clean run\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
