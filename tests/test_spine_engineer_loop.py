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

import io
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from kstrl.context import IterationContext, IterationRecord
from kstrl.factory import _run_component, _setup_worktree
from kstrl.init_cmd import DEFAULT_GOLDEN_PATTERNS, DEFAULT_MEMORY, run_init
from kstrl.operator_context import MEMORY
from kstrl.ui.plain import PlainUI
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
RETRY_END_MARKER = "=== END PREVIOUS CONTEXT ==="
CLAUDE_MD_MARKER = "# Project Context (from CLAUDE.md)"
#: Off the ROW, so a header changed in one place moves this with it.
MEMORY_MARKER = f"=== {MEMORY.header} KSTRL-DATA-"
MEMORY_END_MARKER = f"=== END {MEMORY.header} KSTRL-DATA-"
MEMORY_REL = "scripts/kstrl/memory.md"

FEEDFORWARD_CONFIG: dict[str, object] = {
    "enabled": True,
    "module_map": True,
    "public_interfaces": False,
    "dependency_graph": False,
    "conventions": False,
    "max_context_tokens": 4000,
}


#: The spine fixture and the one call into the worker, at module level
#: rather than on a class. Both test classes below drive the same seam,
#: and the R10.9 class reached these by INSTANTIATING the R10.8 class
#: (`TestGoldenPatternsReachTheEngineer()._prompt_after_run(...)`), which
#: is a second path to one fixture and cost a wrapper method that nothing
#: called. One definition, called by name.
def _repo_with_source(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
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


def _run(
    root: Path,
    worktree: Path,
    agent_bin: Path,
    previous_context_json: str | None = None,
    max_iterations: int = 10,
) -> None:
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
        previous_context_json=previous_context_json,
        max_iterations=max_iterations,
        feedforward_config_dict=FEEDFORWARD_CONFIG,
        knowledge_prefix=KNOWLEDGE_MARKER,
        decisions_prefix=f"{DECISIONS_MARKER}\n\n- encoding: utf-8, named at every read",
    )


def _prompt_after_run(
    tmp_path: Path,
    write: Callable[[Path, Path], None],
    previous_context_json: str | None = None,
) -> str:
    root, worktree, cap, agent = _repo_with_source(tmp_path)
    write(root, worktree)
    _run(root, worktree, agent, previous_context_json)
    return (cap / "prompt.txt").read_text(encoding="utf-8")


class TestGoldenPatternsReachTheEngineer:
    """R10.8, at the seam that decides it: the worker's prefix assembly.

    Not a unit test of the loader (tests/test_operator_context.py does
    that) but of the ORDER the engineer reads, captured off the real
    agent subprocess's stdin.
    """

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
        without = _prompt_after_run(tmp_path / "a", lambda root, wt: None)
        with_scaffold = _prompt_after_run(
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
        authored them, on every attempt of every component forever.
        """
        prompt = _prompt_after_run(
            tmp_path, lambda root, wt: _write(root / GOLDEN_REL, DEFAULT_GOLDEN_PATTERNS)
        )

        assert "=== GOLDEN PATTERNS" not in prompt
        assert "<pattern>: see" not in prompt

    def test_one_edited_line_turns_the_block_on(self, tmp_path: Path) -> None:
        """The other side of the digest rule: the suppression is keyed on
        the body, so touching it is all it takes."""
        prompt = _prompt_after_run(
            tmp_path,
            lambda root, wt: _write(
                root / GOLDEN_REL,
                DEFAULT_GOLDEN_PATTERNS + "\n- atomic writes: see `kstrl/atomicio.py`\n",
            ),
        )

        assert GOLDEN_MARKER in prompt
        assert "- atomic writes: see `kstrl/atomicio.py`" in prompt

    def test_golden_block_between_knowledge_and_feedforward(self, tmp_path: Path) -> None:
        prompt = _prompt_after_run(
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
        prompt = _prompt_after_run(
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
        prompt = _prompt_after_run(
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

        prompt = _prompt_after_run(tmp_path, both)

        assert "- from the repo root" in prompt
        assert "planted by the previous agent" not in prompt


class TestMemoryIsReadAfterTheRetryContext:
    """R10.9 at the seam that decides it: the worker's prefix assembly.

    The ordering IS the feature. `CLAUDE.md` is prepended by `run_loop`
    after the context prefix, so standing feedback placed there sits
    between the retry context and the instructions rather than framing
    how the retry context is acted on. This class captures a real
    engineer prompt off a real agent subprocess's stdin and pins where
    the memory block lands in it.
    """

    @staticmethod
    def _failed_attempt() -> str:
        """A retry context with something in it, as attempt 2 would have."""
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(iteration=1, success=False, error="tests failed"))
        ctx.add_review_finding("the reviewer asked for a narrower guard", attempt=1, phase="review")
        return ctx.to_json()

    def test_memory_is_last_and_precedes_the_claude_md_prepend(self, tmp_path: Path) -> None:
        """Every block present at once, in one assertion.

        Acceptance criterion 1 of the issue, plus the whole chain: a test
        that only checked "memory after retry" would pass with memory
        wedged between knowledge and golden patterns on a later edit.
        """

        def both(root: Path, wt: Path) -> None:
            _write(root / "CLAUDE.md", "# Project rules\n\n- one rule\n")
            git("add", "CLAUDE.md", cwd=root)
            git("commit", "-q", "-m", "claude", cwd=root)
            _write(root / GOLDEN_REL, "# Golden patterns\n\n- atomic writes: see atomicio\n")
            _write(root / MEMORY_REL, "# Memory\n\n## Guidance\n\n- never touch migrations\n")

        prompt = _prompt_after_run(tmp_path, both, self._failed_attempt())

        assert MEMORY_MARKER in prompt
        assert "- never touch migrations" in prompt
        assert (
            prompt.index(KNOWLEDGE_MARKER)
            < prompt.index(GOLDEN_MARKER)
            < prompt.index(DECISIONS_MARKER)
            < prompt.index(FEEDFORWARD_MARKER)
            < prompt.index(RETRY_END_MARKER)
            < prompt.index(MEMORY_MARKER)
            < prompt.index(MEMORY_END_MARKER)
            < prompt.index(CLAUDE_MD_MARKER)
        )
        # rindex, not index: the retry block names its own end delimiter
        # nowhere else, but the assertion the issue asks for is about the
        # LAST one, so it is spelled that way here too.
        assert prompt.rindex(RETRY_END_MARKER) < prompt.index(MEMORY_MARKER)

    def test_memory_absent_leaves_the_prefix_byte_identical(self, tmp_path: Path) -> None:
        """Acceptance criterion 2, compared as BYTES rather than as an
        ordering (nit 14 of #229's round 1: a test named for byte
        identity that asserts delimiter-absence is a weaker claim than
        its own name). The two runs differ only in whether a memory file
        exists at the root, and the fixture is deterministic apart from
        the per-run temp path.
        """
        without = _prompt_after_run(tmp_path / "a", lambda root, wt: None)
        with_scaffold = _prompt_after_run(
            tmp_path / "b", lambda root, wt: _write(root / MEMORY_REL, DEFAULT_MEMORY)
        )

        assert MEMORY.header not in without
        assert without.replace(str(tmp_path / "a"), "") == with_scaffold.replace(
            str(tmp_path / "b"), ""
        )

    def test_an_unedited_ks_init_scaffold_injects_nothing(self, tmp_path: Path) -> None:
        prompt = _prompt_after_run(
            tmp_path, lambda root, wt: _write(root / MEMORY_REL, DEFAULT_MEMORY)
        )

        assert MEMORY.header not in prompt
        assert "Standing feedback for kstrl runs" not in prompt

    def test_one_edited_line_turns_the_block_on(self, tmp_path: Path) -> None:
        prompt = _prompt_after_run(
            tmp_path,
            lambda root, wt: _write(root / MEMORY_REL, DEFAULT_MEMORY + "- one durable rule\n"),
        )

        assert MEMORY_MARKER in prompt
        assert "- one durable rule" in prompt

    def test_the_memory_root_copy_is_read(self, tmp_path: Path) -> None:
        prompt = _prompt_after_run(
            tmp_path, lambda root, wt: _write(root / MEMORY_REL, "- from the repo root\n")
        )

        assert "- from the repo root" in prompt

    def test_a_worktree_memory_copy_is_not_read(self, tmp_path: Path) -> None:
        """S3 of #229's round 1 applies to this file too, and #231 raises
        the stake: the root copy becomes a file the daemon writes on an
        authorised reviewer's say-so, so a component agent that could
        substitute its own copy would be choosing what the next
        component is told under a header naming the operator."""
        prompt = _prompt_after_run(
            tmp_path,
            lambda root, wt: _write(wt / MEMORY_REL, "- planted by the previous agent\n"),
        )

        assert "planted by the previous agent" not in prompt
        assert MEMORY.header not in prompt

    def test_the_worktree_memory_copy_loses_to_the_root_copy(self, tmp_path: Path) -> None:
        def both(root: Path, wt: Path) -> None:
            _write(root / MEMORY_REL, "- from the repo root\n")
            _write(wt / MEMORY_REL, "- planted by the previous agent\n")

        prompt = _prompt_after_run(tmp_path, both)

        assert "- from the repo root" in prompt
        assert "planted by the previous agent" not in prompt

    @pytest.mark.parametrize("absent", [None, ""], ids=["none", "empty-string"])
    def test_a_falsy_retry_context_still_produces_no_block(
        self,
        tmp_path: Path,
        absent: str | None,
    ) -> None:
        """The hoist of ``_retry_block`` out of ``_run_component`` is a
        refactor and this is the equivalence it has to keep.

        Its second "" case, a context that formats to WHITESPACE, is
        preserved verbatim and is not reachable from here: measured,
        ``IterationContext().format_for_prompt()`` always emits the
        ``=== PREVIOUS ATTEMPT CONTEXT (Attempt 1) ===`` header, so an
        empty context renders a block. That guard was already unreachable
        through ``IterationContext`` before this PR; it is kept rather
        than deleted because deleting it would be a behaviour change
        smuggled into a refactor.
        """
        prompt = _prompt_after_run(tmp_path, lambda root, wt: None, absent)

        assert "PREVIOUS ATTEMPT CONTEXT" not in prompt

    def test_an_empty_iteration_context_still_renders_its_header(self, tmp_path: Path) -> None:
        """The other half of the measurement above, so the claim in that
        docstring is checked rather than asserted."""
        prompt = _prompt_after_run(tmp_path, lambda root, wt: None, IterationContext().to_json())

        assert "=== PREVIOUS ATTEMPT CONTEXT (Attempt 1) ===" in prompt


class TestThePromptIsBuiltOncePerAttempt:
    """R10.9 round 1, nit 11. The unit of latency is the ATTEMPT.

    ``loop.run_loop`` substitutes the template, prepends the project
    context and prepends ``context_prefix`` at ``kstrl/loop.py:617-629``,
    and the iteration loop starts at ``:711``. So one prompt object is
    handed to every iteration of an attempt, and an edit to memory.md
    made while a component is running reaches no iteration of the attempt
    that is running.

    Read as a doc correction this is nothing: a prompt IS a per-attempt
    object, so "every subsequent engineer prompt" stays true. Read as a
    NUMBER it is what #231 inherits: the latency of a ``/memory`` write
    is one attempt, and on a ``ks run 10`` that is the whole run. Round 1
    found no test pinning it. This is that test, and it is captured off
    the real agent subprocess's stdin rather than argued from the source
    lines above.
    """

    @staticmethod
    def _numbering_agent(cap: Path, root: Path) -> str:
        """A fake agent that numbers its captures and edits memory.md once.

        Reads stdin FIRST so the parent is never blocked writing into a
        full pipe, then numbers the capture off a counter file. Iteration
        1 appends to the ROOT memory file and does not complete;
        iteration 2 completes.
        """
        return (
            "#!/bin/bash\n"
            f"cat > '{cap}/stdin.txt'\n"
            f"n=$(cat '{cap}/count' 2>/dev/null || echo 0)\n"
            "n=$((n+1))\n"
            f"echo $n > '{cap}/count'\n"
            f"cp '{cap}/stdin.txt' \"{cap}/prompt-$n.txt\"\n"
            'if [ "$n" = "1" ]; then\n'
            f"  printf -- '- rule beta\\n' >> '{root / MEMORY_REL}'\n"
            "  echo one > iteration-1.txt\n"
            "  git add -A && git commit -q -m 'iteration 1'\n"
            "else\n"
            "  echo two > iteration-2.txt\n"
            "  git add -A && git commit -q -m 'iteration 2'\n"
            f"  {COMPLETE_LINE}\n"
            "fi\n"
        )

    def test_an_edit_mid_attempt_reaches_no_iteration_of_that_attempt(self, tmp_path: Path) -> None:
        root, worktree, cap, agent_bin = _repo_with_source(tmp_path)
        _write(root / MEMORY_REL, DEFAULT_MEMORY + "- rule alpha\n")
        agent_bin.write_text(self._numbering_agent(cap, root))
        agent_bin.chmod(0o755)

        _run(root, worktree, agent_bin, max_iterations=2)

        first = (cap / "prompt-1.txt").read_text(encoding="utf-8")
        second = (cap / "prompt-2.txt").read_text(encoding="utf-8")
        assert (cap / "count").read_text().strip() == "2", "the loop ran fewer than 2 iterations"
        assert "- rule beta" in (root / MEMORY_REL).read_text(encoding="utf-8"), (
            "the agent's own append did not land, so the case proves nothing"
        )
        assert "- rule alpha" in first and "- rule alpha" in second
        assert "- rule beta" not in second
        assert first == second, (
            "the prompt is built once per attempt, so iteration 2 must receive the "
            "same bytes iteration 1 received"
        )


#: The sentences round 1 (should-fix 1) measured inside a real engineer
#: prompt, verbatim from the bodies `ks init` shipped at `cbc0f04`. Each
#: is either FALSE where it is read (the file is being injected at the
#: moment it says nothing is injected) or an imperative addressed to
#: somebody who is not reading, which `operator_context`'s docstring
#: records as the line between label glue and a prompt body (H3a).
#:
#: Pinned as data rather than as five assertions so a sixth is one row,
#: and taken from the OLD bodies rather than from the new ones: a list
#: derived from what ships today would pass whatever ships today.
LIFECYCLE_SENTENCES: tuple[str, ...] = (
    "Nothing is injected while",
    "nothing is\ninjected",
    "Keep `## Guidance`\nlast",
    "Keep it short",
    "Replace the placeholders",
    "Read into the engineer's prompt",
)


class TestTheScaffoldPreambleTheEngineerReads:
    """R10.9 round 1, should-fix 1, at the seam where the cost is paid.

    While a scaffold is unchanged its whole body is suppressed, so the
    preamble costs nothing and every gate is green. From the operator's
    FIRST edit onward the whole body reaches the engineer, preamble
    included, under a header saying the operator wrote it. Round 1
    captured that and found two shapes in it: a sentence false where it
    is read, and imperatives about maintaining the file.

    Both scaffolds are edited by ONE appended line, which is the smallest
    thing an operator does and the exact state #231's first `/memory`
    comment produces. The capture is a real `ks init` writing the real
    bodies and a real `_run_component` handing the prompt to a real agent
    subprocess on stdin, so nothing here is asserted against the
    constants.
    """

    @staticmethod
    def _prompt_after_one_appended_line(tmp_path: Path) -> str:
        root, worktree, cap, agent_bin = _repo_with_source(tmp_path)
        assert run_init(root, PlainUI(no_color=True, file=io.StringIO())) == 0
        for rel in (GOLDEN_REL, MEMORY_REL):
            path = root / rel
            assert path.exists(), f"ks init did not scaffold {rel}"
            path.write_text(
                path.read_text(encoding="utf-8") + "- one appended line\n", encoding="utf-8"
            )
        _run(root, worktree, agent_bin)
        return (cap / "prompt.txt").read_text(encoding="utf-8")

    def test_no_lifecycle_sentence_reaches_the_engineer(self, tmp_path: Path) -> None:
        prompt = self._prompt_after_one_appended_line(tmp_path)

        assert "=== MEMORY (standing feedback)" in prompt, "the edit did not turn the block on"
        assert "=== GOLDEN PATTERNS (operator-authored)" in prompt
        assert "- one appended line" in prompt
        for sentence in LIFECYCLE_SENTENCES:
            assert sentence not in prompt, sentence

    def test_what_is_left_is_a_title_and_one_declarative_sentence(self, tmp_path: Path) -> None:
        """The other half: cutting the preamble did not cut the file's
        subject line, which is what tells the engineer whose words these
        are and what they are for. Asserted against the prompt, not
        against the constant."""
        prompt = self._prompt_after_one_appended_line(tmp_path)

        assert "Standing feedback for kstrl runs in this repository" in prompt
        assert "what a good change looks like in this repository" in prompt
