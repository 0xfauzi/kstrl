"""#199: the architect prompt carries the codebase map and the
extracted public interfaces, under the same budget and delimiter
machinery the engineer's feedforward block already uses.

End to end means driving the real entry point, ``decompose_spec``
(through ``_capture_architect_prompt``), and asserting on the prompt
string the agent actually received - not on the private helpers alone.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from kstrl import decompose, init_cmd
from kstrl.decompose import decompose_spec
from kstrl.feedforward import FeedforwardConfig, build_feedforward_context
from kstrl.init_cmd import DEFAULT_CODEBASE_MAP, SCAFFOLDED_TEMPLATES, ScaffoldedTemplate
from kstrl.ui.plain import PlainUI
from tests.test_decompose import VALID_DECOMPOSE_OUTPUT
from tests.test_prompt_injection_guard import _TOKEN_RE


class Recorder:
    """An agent double that records every prompt it was given.

    ``tests.test_decompose.MockDecomposeAgent`` cannot be reused here:
    its ``run`` discards the prompt argument, so it cannot answer "what
    did the architect actually see". Copied from the plan's verified
    reproduction rather than written fresh.
    """

    def __init__(self, output: str) -> None:
        self._output = output
        self.prompts: list[str] = []
        self._final: str | None = None

    @property
    def name(self) -> str:
        return "recorder"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        yield from self._output.splitlines()
        self._final = self._output.splitlines()[-1]

    @property
    def final_message(self) -> str | None:
        return self._final


def _capture_architect_prompt(root: Path, spec: Path) -> str:
    """Drive the real ``decompose_spec`` and return the prompt the agent got."""
    agent = Recorder(VALID_DECOMPOSE_OUTPUT)
    decompose_spec(
        spec_path=spec,
        project_name="test",
        base_branch="main",
        single_pr=False,
        agent=agent,  # type: ignore[arg-type]
        ui=PlainUI(no_color=True),
        root_dir=root,
    )
    return agent.prompts[0]


def _sentinel_repo(root: Path) -> Path:
    """A repo with a populated map and a real ``src/parser`` package,
    plus ``spec.md``. Shared by E1, E3 and E11."""
    (root / "scripts" / "kstrl").mkdir(parents=True)
    (root / "scripts" / "kstrl" / "codebase_map.md").write_text(
        "# Codebase Map\n\nSENTINEL_MAP_FACT: the parser lives in src/parser/\n",
        encoding="utf-8",
    )
    pkg = root / "src" / "parser"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "class SentinelParser:\n    pass\n\n\ndef sentinel_parse(x):\n    return x\n",
        encoding="utf-8",
    )
    spec = root / "spec.md"
    spec.write_text("# Spec\n\nBuild a document parser.\n", encoding="utf-8")
    return spec


# ---------------------------------------------------------------------------
# E1
# ---------------------------------------------------------------------------


def test_a_populated_map_and_the_interfaces_reach_the_architect_prompt(tmp_path: Path) -> None:
    spec = _sentinel_repo(tmp_path)

    prompt = _capture_architect_prompt(tmp_path, spec)

    assert "SENTINEL_MAP_FACT" in prompt
    assert "## Public interfaces" in prompt
    assert "SentinelParser" in prompt

    # The map fact and the interfaces sit inside ONE repository-context
    # section whose two delimiter lines carry the SAME token, checked
    # with a backreference so a block closed by a different token fails.
    m = re.search(
        r"<<<(KSTRL-DATA-[0-9a-f]{32}):BEGIN REPOSITORY CONTEXT>>>\n(.*?)\n"
        r"<<<\1:END REPOSITORY CONTEXT>>>",
        prompt,
        re.DOTALL,
    )
    assert m is not None
    assert "SENTINEL_MAP_FACT" in m.group(2)
    assert "SentinelParser" in m.group(2)

    # The block belongs AFTER the spec: that is where the template's
    # slot is. An implementation that prepended it would otherwise pass
    # every other assertion here.
    assert prompt.index("END SPECIFICATION") < prompt.index("BEGIN REPOSITORY CONTEXT")


# ---------------------------------------------------------------------------
# E2: an untouched scaffold map is not injected
# ---------------------------------------------------------------------------


def test_an_untouched_scaffold_map_is_not_injected_end_to_end(tmp_path: Path) -> None:
    """E2a. The real ledger, driven through the real entry point."""
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    (tmp_path / "scripts" / "kstrl" / "codebase_map.md").write_text(
        DEFAULT_CODEBASE_MAP, encoding="utf-8"
    )
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nBuild something.\n", encoding="utf-8")

    prompt = _capture_architect_prompt(tmp_path, spec)

    assert "REPOSITORY MAP (agent-maintained, untrusted)" not in prompt
    assert "(add directories/files that are fragile or off-limits)" not in prompt
    # "BEGIN REPOSITORY CONTEXT" MAY appear: feedforward still contributes
    # (this repo has no other Python, so in this fixture it does not, but
    # the assertion is deliberately about the MAP header, not the block).


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_an_untouched_scaffold_map_is_suppressed_across_the_whole_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2b. The synthetic-ledger idiom ``tests/test_prompt_staleness.py``
    already uses (:77, monkeypatching ``init_cmd.SCAFFOLDED_TEMPLATES``),
    proving the suppression reads the WHOLE history and not only the
    newest row - the property plant P2 attacks - without shipping three
    historical bodies or depending on git."""
    old_body = "# Codebase Map (Old Skeleton)\n\n- [ ] fill this in\n"
    one_row = ScaffoldedTemplate(
        filename="codebase_map.md",
        constant_name="DEFAULT_CODEBASE_MAP",
        body=DEFAULT_CODEBASE_MAP,
        history=(
            (_sha256(old_body), "8.0.0"),
            (_sha256(DEFAULT_CODEBASE_MAP), "9.0.0"),
        ),
    )
    monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (one_row,))

    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    (tmp_path / "scripts" / "kstrl" / "codebase_map.md").write_text(old_body, encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nBuild something.\n", encoding="utf-8")

    prompt = _capture_architect_prompt(tmp_path, spec)

    assert "Old Skeleton" not in prompt


def test_the_codebase_map_history_holds_four_digests_ending_at_the_current_body() -> None:
    """A cheap ledger assertion, not a behaviour test: the four digests
    graft 4 requires are present as DATA."""
    row = next(t for t in SCAFFOLDED_TEMPLATES if t.filename == "codebase_map.md")
    assert len(row.history) == 4
    assert row.history[-1] == (_sha256(DEFAULT_CODEBASE_MAP), row.current_label)


# ---------------------------------------------------------------------------
# E3
# ---------------------------------------------------------------------------


def test_the_repository_block_and_the_spec_carry_different_tokens(tmp_path: Path) -> None:
    spec = _sentinel_repo(tmp_path)

    prompt = _capture_architect_prompt(tmp_path, spec)

    spec_line = next(ln for ln in prompt.splitlines() if "BEGIN SPECIFICATION" in ln)
    repo_line = next(ln for ln in prompt.splitlines() if "BEGIN REPOSITORY CONTEXT" in ln)
    spec_token = _TOKEN_RE.search(spec_line)
    repo_token = _TOKEN_RE.search(repo_line)
    assert spec_token is not None and repo_token is not None
    assert spec_token.group(0) != repo_token.group(0)

    # Three distinct tokens in this prompt, not two: the spec block, the
    # repository-context block, and the nested map block that
    # ``load_operator_file`` frames with a token of its own.
    assert len(_TOKEN_RE.findall(prompt)) >= 3


# ---------------------------------------------------------------------------
# E4
# ---------------------------------------------------------------------------


def test_an_over_budget_map_is_cut_and_the_cut_is_stated(tmp_path: Path) -> None:
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    content = "".join(f"line {i:04d}\n" for i in range(2000))
    assert len(content) == 20000
    (tmp_path / "scripts" / "kstrl" / "codebase_map.md").write_text(content, encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nBuild something.\n", encoding="utf-8")

    prompt = _capture_architect_prompt(tmp_path, spec)

    assert (
        "truncated: 11999 of 20000 characters shown from scripts/kstrl/codebase_map.md, "
        "keeping the start of the file and dropping the end" in prompt
    )
    assert "line 0000" in prompt
    assert "line 1999" not in prompt


# ---------------------------------------------------------------------------
# E10
# ---------------------------------------------------------------------------


def test_a_repository_with_nothing_to_say_gets_todays_prompt_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's own greenfield acceptance criterion, as a behaviour
    rather than as a digest. A tmp repo holding only ``spec.md`` and
    ``scripts/kstrl/`` - no map, no Python file."""
    monkeypatch.setattr(decompose, "generate_data_delimiter", lambda: "KSTRL-DATA-" + "0" * 32)

    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nBuild something.\n", encoding="utf-8")

    prompt = _capture_architect_prompt(tmp_path, spec)

    expected = decompose.build_decompose_prompt("test", spec.read_text(encoding="utf-8"))
    assert prompt == expected


# ---------------------------------------------------------------------------
# E11
# ---------------------------------------------------------------------------


def test_the_architect_gets_the_interfaces_the_engineers_config_would_evict(
    tmp_path: Path,
) -> None:
    spec = _sentinel_repo(tmp_path)
    # A sibling module so a dependency graph exists to be rendered.
    (tmp_path / "src" / "parser" / "helpers.py").write_text(
        "from parser.core import SentinelParser\n\n\ndef helper():\n    return SentinelParser()\n",
        encoding="utf-8",
    )
    (tmp_path / "kstrl.toml").write_text(
        "[feedforward]\ndependency_graph = true\nmax_context_tokens = 1\n",
        encoding="utf-8",
    )

    # CONTROL, so the negative is not vacuous: an ordinary engineer-shaped
    # config with the graph on and room to render it DOES show one.
    control_config = replace(
        FeedforwardConfig.load(tmp_path),
        dependency_graph=True,
        max_context_tokens=6000,
    )
    control = build_feedforward_context(tmp_path, control_config)
    assert "## Dependency graph" in control

    prompt = _capture_architect_prompt(tmp_path, spec)

    assert "## Public interfaces" in prompt
    assert "SentinelParser" in prompt
    assert "## Dependency graph" not in prompt
