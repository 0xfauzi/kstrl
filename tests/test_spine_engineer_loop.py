"""Spine tier II (R4.2): engineer-loop plumbing, fully unmocked.

One direct ``_run_component`` execution - the exact function the factory
scheduler submits to its worker pool - against a real worktree with a
fake agent BINARY (an executable script on disk, run through the custom
agent adapter as a real subprocess). No ``unittest.mock`` anywhere.

Proven by the artifacts, not by call records:
- worktree in: the agent's own ``pwd`` capture shows it ran inside the
  provisioned worktree, on the component branch;
- provisioning: the per-component PRD and the prompt template (both
  gitignored, so absent from a fresh worktree) were copied in, and the
  prompt the agent RECEIVED is the template with ``$prd_path``
  substituted to the worktree's PRD copy;
- result out: the agent's commit lands on the component branch and
  ``_run_component`` reports success with the true iteration count.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kstrl.factory import _run_component, _setup_worktree
from tests.spine_utils import git, init_ralph_repo

pytestmark = pytest.mark.spine

COMP = "comp-a"
BRANCH = f"ralph/factory/{COMP}"
RUN_ID = "spine-run-engineer"
PRD_REL = f"scripts/ralph/feature/{COMP}/prd.json"
PROMPT_REL = "scripts/ralph/prompt.md"


class TestEngineerLoopPlumbing:
    def test_run_component_end_to_end_with_fake_agent_binary(
        self, tmp_path: Path,
    ) -> None:
        root = tmp_path / "repo"
        init_ralph_repo(root, (COMP,))
        worktree = _setup_worktree(COMP, BRANCH, "main", root, RUN_ID)
        # The gitignored inputs are NOT in the fresh worktree via git;
        # only _run_component's provisioning can put them there.
        assert not (worktree / PRD_REL).exists()
        assert not (worktree / PROMPT_REL).exists()

        cap_dir = tmp_path / "capture"
        cap_dir.mkdir()
        agent_bin = tmp_path / "bin" / "fake-agent"
        agent_bin.parent.mkdir()
        agent_bin.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            cat > '{cap_dir}/prompt.txt'
            pwd > '{cap_dir}/cwd.txt'
            echo implemented > result.txt
            git add result.txt
            git commit -q -m 'engineer output'
            echo '<promise>COMPLETE</promise>'
        """))
        agent_bin.chmod(0o755)

        result = _run_component(
            COMP,
            PRD_REL,
            str(worktree),
            str(root),
            PROMPT_REL,
            str(agent_bin),  # agent_cmd: the fake agent binary
            None,  # model
            None,  # reasoning
            None,  # agent_type
            0.0,   # sleep_seconds
        )

        assert result.success is True
        assert result.component_id == COMP
        assert result.iterations == 1
        assert result.error is None

        # PRD copy present, byte-identical to the root's per-component
        # PRD; prompt copy present likewise.
        assert (worktree / PRD_REL).read_text() == (
            (root / PRD_REL).read_text()
        )
        assert (worktree / PROMPT_REL).read_text() == (
            (root / PROMPT_REL).read_text()
        )

        # Worktree in: the agent subprocess really ran inside the
        # provisioned worktree, on the component branch.
        agent_cwd = (cap_dir / "cwd.txt").read_text().strip()
        assert Path(agent_cwd).resolve() == worktree.resolve()
        assert git("branch", "--show-current", cwd=worktree) == BRANCH

        # The prompt the agent received is the template with $prd_path
        # substituted to the worktree's PRD copy (never the root's).
        prompt = (cap_dir / "prompt.txt").read_text()
        assert "Read the PRD at" in prompt
        assert str(worktree / PRD_REL) in prompt
        assert str(root / PRD_REL) not in prompt
        assert "PREVIOUS ATTEMPT CONTEXT" not in prompt

        # Result out: the engineer's commit is on the component branch.
        assert (worktree / "result.txt").read_text() == "implemented\n"
        assert git("log", "-1", "--format=%s", cwd=worktree) == (
            "engineer output"
        )
        assert git("status", "--porcelain", cwd=worktree) == ""
