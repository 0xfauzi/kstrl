"""R0.4: worktree provisioning + diff-scope retry context (integration).

Acceptance scenario is docs/phase-f-e2e-validation-v12.log:

- ``scripts/ralph/`` is gitignored, so a fresh worktree never contains the
  customized prompt.md or the component PRD via git. ``_run_component``
  must copy them (plus CLAUDE.md/AGENTS.md) from ``root_dir`` -- resolved
  against ``root_dir`` explicitly, never the worker's inherited CWD (the
  logged run fell back to the harness DEFAULT_PROMPT, log line 38).
- A diff-scope failure's retry prompt must name the base branch and the
  full allowed-paths list. In the logged run the retry agent guessed
  ``main`` as base, ran ``git checkout main -- kstrl/...`` reverting
  base-branch content, and failed again.

These tests use real git repos and a real fake-agent subprocess
(CustomAgent via ``agent_cmd``); no LLM is involved.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig

CUSTOM_PROMPT = (
    "CUSTOMIZED-PROMPT-MARKER-7f3a\n"
    "\n"
    "Read the PRD at $prd_path and implement one story.\n"
)
CLAUDE_MD_MARKER = "PROJECT-CONTEXT-MARKER-2b9c"
COMPLETE_LINE = "echo '<promise>COMPLETE</promise>'"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30,
    )


def _init_repo(root: Path, allowed_paths: list[str] | None = None) -> None:
    """Real git repo shaped like a ralph project: scripts/ralph/ (prompt +
    PRD), CLAUDE.md, and AGENTS.md are all gitignored, so none of them
    reach a fresh worktree through git -- provisioning must copy them."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / ".gitignore").write_text(
        "scripts/ralph/\nCLAUDE.md\nAGENTS.md\n"
    )
    (root / "README.md").write_text("seed\n")
    _git("add", ".gitignore", "README.md", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)

    ralph_dir = root / "scripts" / "ralph"
    feature_dir = ralph_dir / "feature" / "comp-a"
    feature_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text(CUSTOM_PROMPT)
    prd: dict[str, object] = {
        "branchName": "ralph/factory/comp-a",
        "userStories": [{
            "id": "US-001", "title": "Test",
            "acceptanceCriteria": ["AC1"],
            "priority": 1, "passes": True, "notes": "",
        }],
    }
    if allowed_paths is not None:
        prd["allowedPaths"] = allowed_paths
    (feature_dir / "prd.json").write_text(json.dumps(prd))
    (root / "CLAUDE.md").write_text(f"# {CLAUDE_MD_MARKER}\n")


def _manifest() -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="t",
        base_branch="main", single_pr=False,
        components=[Component(
            id="comp-a", title="A", description="", dependencies=[],
            prd_path="scripts/ralph/feature/comp-a/prd.json",
            branch_name="ralph/factory/comp-a",
        )],
    )


def _factory_config(max_retries: int = 0) -> FactoryConfig:
    return FactoryConfig(
        use_worktrees=True, create_prs=False, max_parallel=1,
        max_retries=max_retries, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_bad_patterns=False,
            subprocess_timeout=30.0,
        ),
    )


def _base_config(root: Path, agent_cmd: str) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0, agent_cmd=agent_cmd,
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


class TestWorktreeProvisioning:
    def test_worktree_run_provisions_prompt_and_context_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A real worktree run has the customized prompt.md, CLAUDE.md, and
        AGENTS.md present, and the engineer runs on the customized prompt
        (not the DEFAULT_PROMPT fallback) -- with the worker's CWD pointed
        somewhere else entirely, proving copies resolve against root_dir."""
        root = tmp_path / "repo"
        _init_repo(root)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        dump = tmp_path / "dump"
        dump.mkdir()
        # The fake agent captures, from INSIDE the worktree: the rendered
        # prompt it received (stdin) and the provisioned files.
        agent_cmd = (
            f"cat > {dump}/prompt-received.txt; "
            f"cp scripts/ralph/prompt.md {dump}/prompt-in-worktree.md; "
            f"cp CLAUDE.md {dump}/claude-in-worktree.md; "
            f"[ -e AGENTS.md ] && echo present > {dump}/agents-present.txt; "
            + COMPLETE_LINE
        )

        result = run_factory(
            _manifest(), _factory_config(), _base_config(root, agent_cmd),
            PlainUI(no_color=True), root,
        )

        assert result.completed == ["comp-a"]
        assert (
            (dump / "prompt-in-worktree.md").read_text() == CUSTOM_PROMPT
        ), "customized prompt.md was not provisioned into the worktree"
        assert CLAUDE_MD_MARKER in (dump / "claude-in-worktree.md").read_text()
        assert (dump / "agents-present.txt").exists()

        received = (dump / "prompt-received.txt").read_text()
        assert "CUSTOMIZED-PROMPT-MARKER-7f3a" in received, (
            "engineer did not run on the customized prompt"
        )
        assert CLAUDE_MD_MARKER in received, (
            "CLAUDE.md project context was not prepended to the prompt"
        )
        out = capsys.readouterr().out
        assert "falling back to harness DEFAULT_PROMPT" not in out


class TestDiffScopeRetryContext:
    def test_retry_prompt_names_base_branch_and_allowed_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attempt 1 commits an out-of-scope file and emits COMPLETE;
        Phase 1 diff_scope fails; attempt 2's prompt must carry the base
        branch name and the full allowed-paths list verbatim, so the retry
        agent never has to guess the base (the logged run guessed wrong)."""
        root = tmp_path / "repo"
        _init_repo(root, allowed_paths=["src/", "tests/"])
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("RALPH_KNOWLEDGE_ENABLED", "0")

        stamp = tmp_path / "attempt1.stamp"
        dump = tmp_path / "retry-prompt.txt"
        agent_cmd = (
            f"if [ ! -f {stamp} ]; then "
            f"touch {stamp}; "
            "echo 'x = 1' > out_of_scope.py; "
            "git add out_of_scope.py; "
            "git commit -q -m oops; "
            "else "
            f"cat > {dump}; "
            "fi; "
            + COMPLETE_LINE
        )

        result = run_factory(
            _manifest(), _factory_config(max_retries=1),
            _base_config(root, agent_cmd),
            PlainUI(no_color=True), root,
        )

        # The retry reuses the component branch, which still carries the
        # out-of-scope commit, so the component ultimately fails -- the
        # assertion under test is the CONTENT of the retry prompt.
        assert "comp-a" in result.failed
        retry_prompt = dump.read_text()
        assert "Verification Failures" in retry_prompt
        assert "Base branch: main" in retry_prompt
        assert "Allowed paths (complete list): src/, tests/" in retry_prompt
        assert "out_of_scope.py" in retry_prompt
