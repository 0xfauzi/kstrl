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
from collections.abc import Callable
from pathlib import Path

import pytest

from kstrl.factory import _run_component, _setup_worktree
from kstrl.init_cmd import DEFAULT_GOLDEN_PATTERNS
from tests.spine_utils import COMPLETE_LINE, git, init_kstrl_repo


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


pytestmark = pytest.mark.spine

COMP = "comp-a"
BRANCH = f"kstrl/factory/{COMP}"
RUN_ID = "spine-run-engineer"
PRD_REL = f"scripts/kstrl/feature/{COMP}/prd.json"
PROMPT_REL = "scripts/kstrl/prompt.md"


class TestEngineerLoopPlumbing:
    def test_run_component_end_to_end_with_fake_agent_binary(
        self,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        worktree = _setup_worktree(COMP, BRANCH, "main", root, RUN_ID)
        # The gitignored inputs are NOT in the fresh worktree via git;
        # only _run_component's provisioning can put them there.
        assert not (worktree / PRD_REL).exists()
        assert not (worktree / PROMPT_REL).exists()

        cap_dir = tmp_path / "capture"
        cap_dir.mkdir()
        agent_bin = tmp_path / "bin" / "fake-agent"
        agent_bin.parent.mkdir()
        agent_bin.write_text(
            textwrap.dedent(f"""\
            #!/bin/bash
            cat > '{cap_dir}/prompt.txt'
            pwd > '{cap_dir}/cwd.txt'
            echo implemented > result.txt
            git add result.txt
            git commit -q -m 'engineer output'
            echo '<promise>COMPLETE</promise>'
        """)
        )
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
            0.0,  # sleep_seconds
        )

        assert result.success is True
        assert result.component_id == COMP
        assert result.iterations == 1
        assert result.error is None

        # PRD copy present, byte-identical to the root's per-component
        # PRD; prompt copy present likewise.
        assert (worktree / PRD_REL).read_text() == ((root / PRD_REL).read_text())
        assert (worktree / PROMPT_REL).read_text() == ((root / PROMPT_REL).read_text())

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
        assert git("log", "-1", "--format=%s", cwd=worktree) == ("engineer output")
        assert git("status", "--porcelain", cwd=worktree) == ""

    def test_an_existing_worktree_prd_is_not_reseeded_from_the_root(
        self,
        tmp_path: Path,
    ) -> None:
        """Provisioning is seed-once, not sync.

        #260 routes the architect's non-blocker findings into the PRD as
        an informational ``specIssues`` block and documents that an
        engineer may edit or delete it: nothing checks, and a deletion
        is not undone on a later iteration. That last clause is this
        guard. Overwrite the worktree copy per iteration and the
        engineer's edits would be reverted underneath it, and a block it
        had already dealt with would come back every time.
        """
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        worktree = _setup_worktree(COMP, BRANCH, "main", root, RUN_ID)

        # The state a second iteration starts from: a copy already in
        # place, differing from the root's.
        edited = '{"branchName": "kstrl/factory/comp-a", "userStories": []}\n'
        (worktree / PRD_REL).parent.mkdir(parents=True, exist_ok=True)
        (worktree / PRD_REL).write_text(edited, encoding="utf-8")
        assert (root / PRD_REL).read_text(encoding="utf-8") != edited

        agent_bin = tmp_path / "bin" / "fake-agent"
        agent_bin.parent.mkdir()
        agent_bin.write_text("#!/bin/bash\ncat >/dev/null\necho '<promise>COMPLETE</promise>'\n")
        agent_bin.chmod(0o755)

        _run_component(
            COMP,
            PRD_REL,
            str(worktree),
            str(root),
            PROMPT_REL,
            str(agent_bin),
            None,  # model
            None,  # reasoning
            None,  # agent_type
            0.0,  # sleep_seconds
        )

        assert (worktree / PRD_REL).read_text(encoding="utf-8") == edited


KNOWLEDGE_MARKER = "=== KNOWLEDGE (spine) ==="
DECISIONS_MARKER = "## Architect Decisions"
FEEDFORWARD_MARKER = "=== CODEBASE CONTEXT (auto-generated) ==="
#: The delimiter carries a per-build random token (S4), so the spine
#: matches its fixed prefix and the unit tests check the token's shape.
GOLDEN_MARKER = "=== GOLDEN PATTERNS (operator-authored) KSTRL-DATA-"
GOLDEN_REL = "scripts/kstrl/golden-patterns.md"

FEEDFORWARD_CONFIG: dict[str, object] = {
    "enabled": True,
    "module_map": True,
    "public_interfaces": False,
    "dependency_graph": False,
    "conventions": False,
    "max_context_tokens": 4000,
}


class TestGoldenPatternsReachTheEngineer:
    """R10.8, at the seam that decides it: the worker's prefix assembly.

    Not a unit test of the loader (tests/test_operator_context.py does
    that) but of the ORDER the engineer reads, captured off the real
    agent subprocess's stdin.
    """

    def _repo_with_source(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        """A spine repo whose worktree has a module for feedforward to see.

        Committed on main BEFORE the worktree is cut, because feedforward
        reads the worktree, and an empty module map would make the
        feedforward block "" and the ordering assertion vacuous.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        (root / "src").mkdir()
        (root / "src" / "mod.py").write_text("def f() -> int:\n    return 1\n")
        git("add", "src/mod.py", cwd=root)
        git("commit", "-q", "-m", "src", cwd=root)
        worktree = _setup_worktree(COMP, BRANCH, "main", root, RUN_ID)

        cap = tmp_path / "capture"
        cap.mkdir()
        agent_bin = tmp_path / "bin" / "fake-agent"
        agent_bin.parent.mkdir()
        agent_bin.write_text(f"#!/bin/bash\ncat > '{cap}/prompt.txt'\n{COMPLETE_LINE}\n")
        agent_bin.chmod(0o755)
        return root, worktree, cap, agent_bin

    def _run(self, root: Path, worktree: Path, agent_bin: Path) -> None:
        _run_component(
            COMP,
            PRD_REL,
            str(worktree),
            str(root),
            PROMPT_REL,
            str(agent_bin),
            None,  # model
            None,  # reasoning
            None,  # agent_type
            0.0,  # sleep_seconds
            feedforward_config_dict=FEEDFORWARD_CONFIG,
            knowledge_prefix=KNOWLEDGE_MARKER,
            decisions_prefix=f"{DECISIONS_MARKER}\n\n- encoding: utf-8, named at every read",
        )

    def _prompt_after_run(self, tmp_path: Path, write: Callable[[Path, Path], None]) -> str:
        root, worktree, cap, agent = self._repo_with_source(tmp_path)
        write(root, worktree)
        self._run(root, worktree, agent)
        return (cap / "prompt.txt").read_text(encoding="utf-8")

    def test_golden_block_absent_leaves_the_prefix_byte_identical(self, tmp_path: Path) -> None:
        """With no file the engineer sees exactly what it saw without the
        feature, compared as BYTES rather than as an ordering.

        The two runs differ only in whether a golden-patterns file exists
        at the root, and the fixture is deterministic apart from the
        per-run temp path, so a difference of one character is a
        difference this feature introduced. Nit 14: the round-1 test was
        named for byte-identity and asserted delimiter-absence plus an
        ordering, which is a weaker claim than its own name.
        """
        without = self._prompt_after_run(tmp_path / "a", lambda root, wt: None)
        with_scaffold = self._prompt_after_run(
            tmp_path / "b", lambda root, wt: _write(root / GOLDEN_REL, DEFAULT_GOLDEN_PATTERNS)
        )

        assert "=== GOLDEN PATTERNS" not in without
        assert without.replace(str(tmp_path / "a"), "") == with_scaffold.replace(
            str(tmp_path / "b"), ""
        )

    def test_an_unedited_ks_init_scaffold_injects_nothing(self, tmp_path: Path) -> None:
        """BLOCKER 1 from review round 1, at the seam that decides it.

        The scaffold `ks init` writes is not absent, not empty and not
        whitespace-only, so before the digest check it reached the
        engineer: 479 characters of angle-bracket placeholders and one
        operator-facing instruction, under a header saying the operator
        authored them, on every iteration of every component forever.
        """
        prompt = self._prompt_after_run(
            tmp_path, lambda root, wt: _write(root / GOLDEN_REL, DEFAULT_GOLDEN_PATTERNS)
        )

        assert "=== GOLDEN PATTERNS" not in prompt
        assert "<pattern>: see" not in prompt

    def test_one_edited_line_turns_the_block_on(self, tmp_path: Path) -> None:
        """The other side of the digest rule: the suppression is keyed on
        the body, so touching it is all it takes."""
        prompt = self._prompt_after_run(
            tmp_path,
            lambda root, wt: _write(
                root / GOLDEN_REL,
                DEFAULT_GOLDEN_PATTERNS + "\n- atomic writes: see `kstrl/atomicio.py`\n",
            ),
        )

        assert GOLDEN_MARKER in prompt
        assert "- atomic writes: see `kstrl/atomicio.py`" in prompt

    def test_golden_block_between_knowledge_and_feedforward(self, tmp_path: Path) -> None:
        prompt = self._prompt_after_run(
            tmp_path,
            lambda root, wt: _write(
                root / GOLDEN_REL,
                "# Golden patterns\n\n- atomic writes: see `kstrl/atomicio.py`\n",
            ),
        )

        assert GOLDEN_MARKER in prompt
        assert "- atomic writes: see `kstrl/atomicio.py`" in prompt
        assert (
            prompt.index(KNOWLEDGE_MARKER)
            < prompt.index(GOLDEN_MARKER)
            < prompt.index(DECISIONS_MARKER)
            < prompt.index(FEEDFORWARD_MARKER)
        )

    def test_the_root_copy_is_read(self, tmp_path: Path) -> None:
        """The only copy read. `ks run` sets use_worktrees=False so the
        root IS the worktree there, and under the factory the root is the
        tree no component agent can write to."""
        prompt = self._prompt_after_run(
            tmp_path, lambda root, wt: _write(root / GOLDEN_REL, "- from the repo root\n")
        )

        assert "- from the repo root" in prompt

    def test_a_worktree_copy_is_not_read(self, tmp_path: Path) -> None:
        """S3 from review round 1. The worktree is the tree the engineer
        has been writing to, and `guards.enforce_allowed_paths` returns
        immediately when ``allowed_paths`` is empty, which is the
        documented default. Reading the worktree copy therefore let one
        component's agent choose what the next component's agent is told,
        unfiltered, under a header asserting the operator wrote it.
        """
        prompt = self._prompt_after_run(
            tmp_path,
            lambda root, wt: _write(wt / GOLDEN_REL, "- planted by the previous agent\n"),
        )

        assert "planted by the previous agent" not in prompt
        assert "=== GOLDEN PATTERNS" not in prompt

    def test_the_worktree_copy_loses_to_the_root_copy(self, tmp_path: Path) -> None:
        """Not merely "the root is a fallback": the root wins outright."""

        def both(root: Path, wt: Path) -> None:
            _write(root / GOLDEN_REL, "- from the repo root\n")
            _write(wt / GOLDEN_REL, "- planted by the previous agent\n")

        prompt = self._prompt_after_run(tmp_path, both)

        assert "- from the repo root" in prompt
        assert "planted by the previous agent" not in prompt
