"""Spine tier II (R4.2): contract execution against real git.

tests/test_contract_safety.py proves the temp-worktree mechanics with
synthetic repos but drives ``run_tier_check`` directly; these tests close
the factory-level boundary: ``run_factory`` in deferred-merge mode
(``create_prs=False``), real ``bash -lc`` engineers committing on real
branches, and the contract phase REALLY merging those branches in a
detached temp worktree and running a real shell test command there.

Covered, with wave-2 (R0.3) semantics asserted:
- passing tiers: the per-tier contract test observes exactly the files
  the merged branches contribute (tier 0 sees alpha only, tier 1 sees
  alpha + beta), proving real incremental merges;
- conflicted tier: the breaker is attributed, the failure is terminal
  when retries are exhausted, and the user's checkout is BYTE-IDENTICAL
  afterward (no merge debris, no contract temp worktrees left behind);
- breaker re-run: a contract test failure bisects to the breaker, the
  breaker re-enters scheduling, its retry context carries the contract
  failure output into the next prompt, and the re-run completes.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kstrl.contract import ContractConfig
from kstrl.factory import FactoryResult, run_factory
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    git,
    init_kstrl_repo,
    make_manifest,
)

pytestmark = pytest.mark.spine


def _checkout_state(root: Path) -> dict[str, Any]:
    """Byte-level fingerprint of the user's checkout.

    Hashes every file under ``root`` except ``.git/`` and ``.kstrl/``
    (branches and run state legitimately change during a factory run;
    the user's working tree must not), plus the git-visible state of the
    checkout itself: HEAD, current branch, and porcelain status.
    """
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in (".git", ".kstrl"):
            continue
        if path.is_file():
            files[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "files": files,
        "head": git("rev-parse", "HEAD", cwd=root),
        "branch": git("branch", "--show-current", cwd=root),
        "status": git("status", "--porcelain", cwd=root),
    }


def _contract_events(progress_path: Path) -> list[tuple[int, bool, str | None]]:
    """(tier, passed, breaker) for each contract_result progress event."""
    events = [
        json.loads(line)
        for line in progress_path.read_text().splitlines()
        if line.strip()
    ]
    return [
        (e["data"]["tier"], e["data"]["passed"], e["data"]["breaker"])
        for e in events
        if e["event"] == "contract_result"
    ]


def _assert_no_contract_debris(root: Path) -> None:
    """No temp worktree survived under .kstrl/contract/ and git tracks
    only the main checkout afterward."""
    contract_dir = root / ".kstrl" / "contract"
    if contract_dir.exists():
        assert list(contract_dir.iterdir()) == []
    listing = git("worktree", "list", "--porcelain", cwd=root)
    worktree_lines = [
        line for line in listing.splitlines() if line.startswith("worktree ")
    ]
    assert worktree_lines == [f"worktree {root}"]


def _run(
    root: Path,
    manifest: Manifest,
    agent_cmd: str,
    contract_test_cmd: str,
    progress_path: Path,
    max_retries: int = 0,
) -> FactoryResult:
    return run_factory(
        manifest,
        factory_config(
            max_retries=max_retries,
            contract_config=ContractConfig(
                mode="tier", test_command=contract_test_cmd, timeout=30.0,
            ),
            progress_log_path=progress_path,
        ),
        base_config(root, agent_cmd),
        PlainUI(no_color=True),
        root,
        # Outside the repo so the byte-identical checkout assertions
        # cover everything except kstrl's declared state dirs.
        manifest_path=progress_path.parent / "manifest.json",
    )


# Engineer that commits one file named after its component. The worktree
# basename is the component id, so one command string serves every
# component without mocks.
_FILE_PER_COMPONENT_ENGINEER = textwrap.dedent("""\
    comp="$(basename "$PWD")"
    echo "$comp" > "$comp.txt"
    git add "$comp.txt"
    git commit -q -m "add $comp.txt"
    echo '<promise>COMPLETE</promise>'
""")

# Engineer that commits the SAME file with per-component content: any
# two components in one tier conflict at contract merge time.
_CONFLICTING_ENGINEER = textwrap.dedent("""\
    comp="$(basename "$PWD")"
    echo "content from $comp" > conflict.txt
    git add conflict.txt
    git commit -q -m "$comp writes conflict.txt"
    echo '<promise>COMPLETE</promise>'
""")


class TestContractPassingTier:
    def test_tier_checks_merge_real_branches_incrementally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two-tier manifest (beta depends on alpha): each tier's
        contract test runs in a temp worktree that REALLY contains the
        merged branches - tier 0 sees alpha.txt only, tier 1 sees
        alpha.txt (prior branch) plus beta.txt - and the run exits 0."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"))
        manifest = make_manifest(
            [component("alpha"), component("beta", ["alpha"])]
        )
        before = _checkout_state(root)
        obs_log = tmp_path / "contract-observations.log"
        # The contract command is a real check AND a real observer: it
        # fails if no merged .txt exists, and records which ones each
        # tier's merged temp worktree actually contains.
        contract_cmd = f"ls *.txt >> '{obs_log}' && echo -- >> '{obs_log}'"
        progress_path = tmp_path / "progress.jsonl"

        result = _run(
            root, manifest, _FILE_PER_COMPONENT_ENGINEER, contract_cmd,
            progress_path,
        )

        assert result.exit_code == 0
        assert result.completed == ["alpha", "beta"]
        assert result.failed == []
        assert result.contract_failures == []
        for comp_id in ("alpha", "beta"):
            comp = manifest.get_component(comp_id)
            assert comp is not None
            assert comp.status == ComponentStatus.COMPLETED.value

        # Both tiers ran and passed, with no breaker.
        assert _contract_events(progress_path) == [
            (0, True, None), (1, True, None),
        ]

        # The merges were real: tier 0's temp worktree held alpha's file
        # only; tier 1's held alpha's (prior branch) plus beta's.
        blocks: list[list[str]] = [[]]
        for line in obs_log.read_text().splitlines():
            if line == "--":
                blocks.append([])
            else:
                blocks[-1].append(line)
        assert [b for b in blocks if b] == [
            ["alpha.txt"], ["alpha.txt", "beta.txt"],
        ]

        assert _checkout_state(root) == before
        _assert_no_contract_debris(root)


class TestContractConflictedTier:
    def test_merge_conflict_attributes_breaker_and_leaves_checkout_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same-tier components whose branches conflict: the later
        merge (manifest order) is blamed, the failure is terminal with
        max_retries=0 (wave-2: recorded loudly, nonzero exit), and the
        user's checkout is byte-identical afterward - the conflict
        happened ONLY in a temp worktree that no longer exists."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"))
        manifest = make_manifest([component("alpha"), component("beta")])
        before = _checkout_state(root)
        progress_path = tmp_path / "progress.jsonl"

        result = _run(
            root, manifest, _CONFLICTING_ENGINEER, "true", progress_path,
        )

        # Wave-2 recovery semantics: the run completes (no exception),
        # fails loudly, and blames the conflicting merge.
        assert result.exit_code == 1
        assert result.completed == ["alpha"]
        assert result.failed == ["beta"]
        assert len(result.contract_failures) == 1
        assert "tier 0" in result.contract_failures[0]
        assert "beta" in result.contract_failures[0]

        alpha = manifest.get_component("alpha")
        beta = manifest.get_component("beta")
        assert alpha is not None and beta is not None
        assert alpha.status == ComponentStatus.COMPLETED.value
        assert beta.status == ComponentStatus.FAILED.value
        assert beta.error == "Contract test failed at tier 0 (retries exhausted)"

        assert _contract_events(progress_path) == [(0, False, "beta")]

        # The user's checkout is byte-identical: same file bytes, same
        # HEAD, same branch, still clean. The conflicted merge never
        # touched it and its temp worktree is gone.
        assert _checkout_state(root) == before
        _assert_no_contract_debris(root)
        assert not (root / ".git" / "MERGE_HEAD").exists()
        assert not (root / "conflict.txt").exists()


class TestContractBreakerRerun:
    def test_breaker_reenters_scheduling_and_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A contract TEST failure (clean merges, broken integration)
        bisects to beta; beta is reset to PENDING, its retry prompt
        carries the contract failure output, its engineer re-runs and
        fixes the branch, and the second contract pass completes the
        run (R0.3: the promised breaker retry actually runs)."""
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, ("alpha", "beta"))
        manifest = make_manifest([component("alpha"), component("beta")])
        cap_dir = tmp_path / "prompts"
        cap_dir.mkdir()
        marker = tmp_path / "beta-first-attempt-done"
        progress_path = tmp_path / "progress.jsonl"

        # Stateful engineer: beta's first attempt commits broken.txt
        # (integration-breaking); its second attempt - on the SAME
        # branch, resumed with the broken commit - removes it and
        # commits beta.txt instead. Every attempt captures the prompt
        # it received on stdin, mock-free.
        engineer = textwrap.dedent(f"""\
            comp="$(basename "$PWD")"
            if [ "$comp" = beta ] && [ -f '{marker}' ]; then
              cat > '{cap_dir}/beta-attempt2.prompt'
              git rm -q broken.txt
              echo beta > beta.txt
              git add beta.txt
              git commit -q -m 'fix integration: replace broken.txt'
            else
              cat > '{cap_dir}'/"$comp"-attempt1.prompt
              if [ "$comp" = beta ]; then
                touch '{marker}'
                echo broken > broken.txt
                git add broken.txt
                git commit -q -m 'beta adds broken.txt'
              else
                echo alpha > alpha.txt
                git add alpha.txt
                git commit -q -m 'alpha adds alpha.txt'
              fi
            fi
            echo '<promise>COMPLETE</promise>'
        """)
        contract_cmd = (
            "test ! -f broken.txt || "
            "{ echo 'integration broken: broken.txt present'; exit 1; }"
        )

        result = _run(
            root, manifest, engineer, contract_cmd, progress_path,
            max_retries=1,
        )

        assert result.exit_code == 0
        assert result.completed == ["alpha", "beta"]
        assert result.failed == []
        assert result.contract_failures == []

        beta = manifest.get_component("beta")
        assert beta is not None
        assert beta.status == ComponentStatus.COMPLETED.value
        assert beta.retries == 1

        # First pass failed and bisected to beta; the re-run passed.
        assert _contract_events(progress_path) == [
            (0, False, "beta"), (0, True, None),
        ]

        # The breaker's engineer REALLY re-ran (exactly once), alpha's
        # did not - proven by the engineers' own capture files.
        assert sorted(p.name for p in cap_dir.iterdir()) == [
            "alpha-attempt1.prompt",
            "beta-attempt1.prompt",
            "beta-attempt2.prompt",
        ]

        # Retry-context propagation for contract failures: attempt 2's
        # prompt names the failing contract output.
        attempt2 = (cap_dir / "beta-attempt2.prompt").read_text()
        assert "## Contract Test Failures" in attempt2
        assert "integration broken: broken.txt present" in attempt2
        assert "PREVIOUS ATTEMPT CONTEXT" in attempt2
        attempt1 = (cap_dir / "beta-attempt1.prompt").read_text()
        assert "PREVIOUS ATTEMPT CONTEXT" not in attempt1

        # The fix landed on beta's real branch: beta.txt in, broken.txt
        # gone.
        branch_files = git(
            "ls-tree", "--name-only", "kstrl/factory/beta", cwd=root,
        ).splitlines()
        assert "beta.txt" in branch_files
        assert "broken.txt" not in branch_files

        _assert_no_contract_debris(root)
