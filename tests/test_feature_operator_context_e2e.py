"""#599 end to end: every `ks feature` engineer loop reads the operator's context.

`ks feature` runs up to three engineer loops (understand, implement,
repair) and, until #599, handed none of them a ``context_prefix``. Its
engineer read no knowledge fact, no ``scripts/kstrl/golden-patterns.md``
and no ``scripts/kstrl/memory.md``, while every factory engineer read all
three, and the run never said the memory file had been cut.

So this file runs the real ``run_init``, the real ``ks feature`` command,
and a real agent subprocess that writes each prompt it is given to its
own file and names the file after the loop the prompt belongs to. One
file per call, never one appended file, so each assertion reads one
known prompt (``tests/test_spine_golden_patterns_e2e.py`` records a
capture that held the distiller's prompt instead of the engineer's).
Fake ``claude`` and ``codex`` executables that exit 1 go first on PATH,
so nothing here can reach a real agent. `ks feature` has no reviewer.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.init_cmd import run_init
from kstrl.knowledge import Fact, write_facts
from kstrl.ui.plain import PlainUI
from tests.helpers import gitrepo
from tests.spine_utils import git

pytestmark = pytest.mark.spine

GOLDEN_REL = "scripts/kstrl/golden-patterns.md"
MEMORY_REL = "scripts/kstrl/memory.md"
PRD_REL = "scripts/kstrl/feature/demo/prd.json"

MEMORY_LINE = "- PROBE-MEMORY never touch the migrations directory"
LATER_MEMORY_LINE = "- PROBE-LATER-MEMORY written between two loops"
GOLDEN_LINE = "- PROBE-GOLDEN copy the handler in README.md"
FACT_CLAIM = "PROBE-FACT the README is the seed file."
STALE_CLAIM = "PROBE-STALE the deleted module held the parser."

GOLDEN_OPEN = "=== GOLDEN PATTERNS (operator-authored) KSTRL-DATA-"
MEMORY_OPEN = "=== MEMORY (standing feedback) KSTRL-DATA-"
MEMORY_CLOSE = "=== END MEMORY (standing feedback) KSTRL-DATA-"
CLAUDE_MD = "# Project Context (from CLAUDE.md)"

PRD = {
    "branchName": "kstrl/demo",
    "userStories": [
        {
            "id": "US-001",
            "title": "Do the thing",
            "acceptanceCriteria": ["it is done"],
            "priority": 1,
            "passes": False,
            "notes": "",
        }
    ],
}

#: The agent. Which loop a prompt belongs to is read off its content,
#: measured on the three templates: the understand prompt names the
#: feature understanding it writes, the repair prompt names the repair
#: PRD under ``repairs/``, and the implement prompt is the other one.
#: ``argv[2]`` is ``complete``, or ``fail-implement`` (exit 1 on the
#: implement prompt, which sends the run to the repair loop), and an
#: optional ``argv[3]`` is a line appended to the memory file at
#: ``argv[4]`` before that exit, standing in for an operator editing the
#: file between two loops.
STUB = """\
import os
import sys
import tempfile

capture, mode = sys.argv[1], sys.argv[2]
text = sys.stdin.read()
if "feature understanding" in text:
    role = "understand"
elif "repairs/repair_" in text:
    role = "repair"
else:
    role = "implement"
fd, _name = tempfile.mkstemp(dir=capture, prefix=role + "-", suffix=".txt")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(text)
if role == "implement" and mode == "fail-implement":
    if len(sys.argv) > 4:
        with open(sys.argv[4], "a", encoding="utf-8") as handle:
            handle.write(sys.argv[3] + "\\n")
    sys.exit(1)
print("<promise>COMPLETE</promise>")
"""


@dataclass(frozen=True)
class FeatureRun:
    exit_code: int
    output: str
    prompts: dict[str, list[str]]

    def only(self, role: str) -> str:
        """The one prompt of ``role``; fails if there is not exactly one."""
        found = self.prompts.get(role, [])
        assert len(found) == 1, (
            f"expected one {role} prompt, got {len(found)}; roles seen: "
            f"{ {name: len(texts) for name, texts in self.prompts.items()} }\n{self.output}"
        )
        return found[0]


def _initialised_project(tmp_path: Path, *, allowed_paths: list[str] | None = None) -> Path:
    """A real git repo with the real `ks init`, one line in each operator
    file, one knowledge fact and the feature PRD, whose ``allowedPaths``
    is ``allowed_paths`` when given and absent otherwise."""
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "README.md", cwd=root)
    git("commit", "-q", "-m", "seed", cwd=root)
    assert run_init(root, PlainUI(no_color=True, file=io.StringIO())) == 0

    with (root / MEMORY_REL).open("a", encoding="utf-8") as handle:
        handle.write(MEMORY_LINE + "\n")
    with (root / GOLDEN_REL).open("a", encoding="utf-8") as handle:
        handle.write(GOLDEN_LINE + "\n")
    fact = Fact(
        id="fact-001",
        component_id="prior",
        created_iter=1,
        created_run_id="r",
        scope="invariant",
        evidence=["README.md"],
        confidence="review_passed",
        claim=FACT_CLAIM,
    )
    assert write_facts([fact], root / ".kstrl" / "knowledge", "prior", "r") == 1

    prd = root / PRD_REL
    prd.parent.mkdir(parents=True)
    doc = dict(PRD) if allowed_paths is None else {**PRD, "allowedPaths": allowed_paths}
    prd.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return root


def _run_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    repair: bool = False,
    later_memory_line: str | None = None,
) -> FeatureRun:
    """Run the real `ks feature` once and return every prompt by loop."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        fake = bindir / name
        fake.write_text(f"#!/bin/sh\necho {name} >> {tmp_path / 'paid.txt'}\nexit 1\n")
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("KSTRL_NO_TUI", "1")

    capture = tmp_path / "prompts"
    capture.mkdir()
    stub = tmp_path / "stub.py"
    stub.write_text(STUB, encoding="utf-8")
    argv = [sys.executable, str(stub), str(capture), "fail-implement" if repair else "complete"]
    if later_memory_line is not None:
        argv += [later_memory_line, str(root / MEMORY_REL)]
    agent = shlex.join(argv)

    repair_args = (
        ["--repair-max-runs", "1", "--repair-iterations", "1"]
        if repair
        else [
            "--repair-max-runs",
            "0",
        ]
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        cli,
        [
            "feature",
            "--prd",
            PRD_REL,
            "--implementation-auto-run",
            "--no-verify",
            "--no-tui",
            "--branch",
            "",
            "--understand-iterations",
            "1",
            *repair_args,
            "--ui",
            "plain",
            "--agent-cmd",
            agent,
        ],
    )
    assert not (tmp_path / "paid.txt").exists(), "a real agent binary was called"
    prompts: dict[str, list[str]] = {}
    for path in sorted(capture.iterdir(), key=lambda p: p.stat().st_mtime_ns):
        role = path.name.split("-", 1)[0]
        prompts.setdefault(role, []).append(path.read_text(encoding="utf-8"))
    return FeatureRun(result.exit_code, result.output, prompts)


def _carries_all_three(prompt: str) -> None:
    assert MEMORY_LINE in prompt
    assert GOLDEN_LINE in prompt
    assert FACT_CLAIM in prompt


class TestFeatureEngineerReadsOperatorContext:
    def test_the_implement_prompt_carries_knowledge_golden_patterns_and_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _initialised_project(tmp_path)

        run = _run_feature(tmp_path, monkeypatch, root)

        assert run.exit_code == 0, run.output
        _carries_all_three(run.only("implement"))
        # The knowledge build writes nothing: a one-component manifest with
        # no dependencies excludes none, so no E8 telemetry row.
        assert not (root / ".kstrl" / "knowledge" / "_e8_dependency_scope.jsonl").exists()

    def test_the_understand_prompt_carries_the_same_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _run_feature(tmp_path, monkeypatch, _initialised_project(tmp_path))

        _carries_all_three(run.only("understand"))

    def test_the_repair_prompt_carries_the_same_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _run_feature(tmp_path, monkeypatch, _initialised_project(tmp_path), repair=True)

        _carries_all_three(run.only("repair"))

    def test_memory_is_the_last_block_and_precedes_claude_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #230 order, through the feature path: knowledge, then golden
        patterns, then memory last, then the CLAUDE.md prepend."""
        prompt = _run_feature(tmp_path, monkeypatch, _initialised_project(tmp_path)).only(
            "implement"
        )

        assert (
            prompt.index(FACT_CLAIM)
            < prompt.index(GOLDEN_OPEN)
            < prompt.index(MEMORY_OPEN)
            < prompt.index(MEMORY_CLOSE)
            < prompt.index(CLAUDE_MD)
        )

    def test_a_memory_line_written_between_loops_reaches_the_next_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prefix is built at each loop, not once per run. The stub
        appends a line to memory.md when it sees the implement prompt, the
        way an operator edits the file at the review gate; the repair loop
        must read it and the understand loop, which ran before, cannot."""
        run = _run_feature(
            tmp_path,
            monkeypatch,
            _initialised_project(tmp_path),
            repair=True,
            later_memory_line=LATER_MEMORY_LINE,
        )

        assert LATER_MEMORY_LINE in run.only("repair")
        assert LATER_MEMORY_LINE not in run.only("understand")
        assert LATER_MEMORY_LINE not in run.only("implement")

    def test_an_over_budget_memory_file_is_reported_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The factory's once-per-run notice, on the feature path. Two loops
        read the file here and the operator is told once."""
        root = _initialised_project(tmp_path)
        with (root / MEMORY_REL).open("a", encoding="utf-8") as handle:
            handle.write("".join(f"- entry {i:04d} keep this line short\n" for i in range(200)))

        run = _run_feature(tmp_path, monkeypatch, root)

        assert run.output.count("Memory: truncated") == 1, run.output
        assert MEMORY_OPEN in run.only("implement")

    def test_knowledge_disabled_sends_no_fact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")

        run = _run_feature(tmp_path, monkeypatch, _initialised_project(tmp_path))

        assert all(FACT_CLAIM not in text for texts in run.prompts.values() for text in texts)
        assert MEMORY_LINE in run.only("implement")

    def test_a_knowledge_failure_is_warned_and_every_loop_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-fatal and never silent, as in ``factory._submit_args``. A
        knowledge root this process cannot list makes retrieval raise; each
        loop warns and runs without facts, and the operator files still
        arrive."""
        root = _initialised_project(tmp_path)
        knowledge_root = root / ".kstrl" / "knowledge"
        knowledge_root.chmod(0o000)
        try:
            run = _run_feature(tmp_path, monkeypatch, root)
        finally:
            knowledge_root.chmod(0o755)

        assert run.exit_code == 0, run.output
        assert run.output.count("Knowledge retrieval failed for demo") == 2, run.output
        assert all(FACT_CLAIM not in text for texts in run.prompts.values() for text in texts)
        assert MEMORY_LINE in run.only("implement")

    def test_a_knowledge_error_that_is_not_an_os_error_is_warned_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chmod test above raises ``PermissionError``, an ``OSError``, so
        a handler narrowed to ``OSError`` would pass it. Retrieval is
        swapped for one that raises ``RuntimeError``; the run must still
        warn at each loop and finish."""

        def failing_retrieval(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("PROBE-RETRIEVAL-ERROR")

        monkeypatch.setattr("kstrl.feature_cmd.build_knowledge_context", failing_retrieval)

        run = _run_feature(tmp_path, monkeypatch, _initialised_project(tmp_path))

        assert run.exit_code == 0, run.output
        assert run.output.count("Knowledge retrieval failed for demo: PROBE-RETRIEVAL-ERROR") == 2
        assert MEMORY_LINE in run.only("implement")

    def test_facts_are_ranked_by_the_prd_scope_and_a_stale_fact_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The feature is ranked the way the factory ranks a component: a
        fact citing a path in the PRD's own ``allowedPaths`` is core, and a
        fact from another component whose every cited path is gone from
        the repo root is dropped. Presence alone cannot see either: the
        claim reaches the prompt whatever tier it lands in."""
        root = _initialised_project(tmp_path, allowed_paths=["README.md"])
        stale = Fact(
            id="fact-002",
            component_id="gone",
            created_iter=1,
            created_run_id="r",
            scope="invariant",
            evidence=["src/deleted.py"],
            confidence="review_passed",
            claim=STALE_CLAIM,
        )
        assert write_facts([stale], root / ".kstrl" / "knowledge", "gone", "r") == 1

        run = _run_feature(tmp_path, monkeypatch, root)

        assert run.exit_code == 0, run.output
        prompt = run.only("implement")
        assert (
            f"### Current component (demo)\n- **prior**[invariant] {{review_passed}}: {FACT_CLAIM}"
            in prompt
        ), prompt
        assert STALE_CLAIM not in prompt
