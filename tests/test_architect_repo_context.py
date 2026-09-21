"""#199: the architect reads the repository, it is not handed a paste.

`kstrl/git.py` records the rule for the reviewer roles: they run with
`cwd` set to the tree under review, so the change does not have to be
pasted into their prompt, and pasting it is what forced a size cap and
a fail-closed stop. `decompose_spec` calls `agent.run(prompt,
cwd=root_dir)`, so the architect is in the same position. These tests
drive the real entry point and assert on the text the agent is handed.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl import decompose
from kstrl.decompose import build_decompose_prompt, decompose_spec
from kstrl.ui.plain import PlainUI
from tests.test_decompose import (
    BLOCKER_ISSUE,
    VALID_DECOMPOSE_OUTPUT,
    _single_component_output,
    _story,
)
from tests.test_prompt_versions import _MARKER_HEAD, _MARKER_TAIL, _ORPHAN_MARKER


class RecordingAgent:
    """Records the prompt and cwd it was handed, then returns valid JSON."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.cwds: list[Path | None] = []
        self._final: str | None = None

    @property
    def name(self) -> str:
        return "recording"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        yield from VALID_DECOMPOSE_OUTPUT.splitlines()
        self._final = VALID_DECOMPOSE_OUTPUT.splitlines()[-1]

    @property
    def final_message(self) -> str | None:
        return self._final


def _repo(tmp_path: Path, *, with_map: bool = True) -> tuple[Path, Path]:
    """Build a repository under tmp_path/proj. Returns (root, spec_path)."""
    root = tmp_path / "proj"
    (root / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    if with_map:
        (root / "scripts" / "kstrl" / "codebase_map.md").write_text(
            "# Codebase Map\n\n## parsers\n`src/parsers.py` parses the schema.\n",
            encoding="utf-8",
        )
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "parsers.py").write_text("def parse(x):\n    return x\n", encoding="utf-8")
    spec = root / "spec.md"
    spec.write_text("# Spec\nBuild a schema parser.\n", encoding="utf-8")
    return root, spec


def _decompose_and_capture(root: Path, spec: Path) -> tuple[str, Path | None]:
    """Drive the REAL entry point; return the prompt and cwd the agent got."""
    agent = RecordingAgent()
    decompose_spec(
        spec_path=spec,
        project_name="test",
        base_branch="main",
        single_pr=False,
        agent=agent,  # type: ignore[arg-type]
        ui=PlainUI(no_color=True, file=io.StringIO()),
        root_dir=root,
    )
    assert agent.prompts, "decompose_spec never called its agent"
    return agent.prompts[0], agent.cwds[0]


class _HaltingRecordingAgent:
    """Like RecordingAgent, but yields a caller-chosen output so the
    run halts via SpecBlockerError before reaching Linear sync."""

    def __init__(self, output: str) -> None:
        self._lines = output.splitlines()
        self.prompts: list[str] = []
        self.cwds: list[Path | None] = []

    @property
    def name(self) -> str:
        return "recording-halt"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        yield from self._lines

    @property
    def final_message(self) -> str | None:
        return self._lines[-1] if self._lines else None


def _decompose_halting_and_capture(root: Path, spec: Path) -> str:
    """Drive decompose_spec to a SpecBlockerError halt; return the prompt.

    Uses an ESCALATED spec_issue (BLOCKER_ISSUE) rather than
    VALID_DECOMPOSE_OUTPUT, and deliberately so, not as a
    simplification: a decompose that runs to a clean, non-halting
    completion proceeds past the halt path into LinearConfig.load(
    root_dir), which re-parses the same broken kstrl.toml, unguarded,
    and raises ConfigError there instead - a real, separate,
    pre-existing call site the blocker-1 fix does not touch. Halting
    via SpecBlockerError is what isolates the one thing the fix is
    about: the architect prompt is built and delivered before any
    config load downstream gets a chance to abort the run.
    """
    agent = _HaltingRecordingAgent(
        _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE])
    )
    with pytest.raises(decompose.SpecBlockerError):
        decompose_spec(
            spec_path=spec,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,  # type: ignore[arg-type]
            ui=PlainUI(no_color=True, file=io.StringIO()),
            root_dir=root,
        )
    assert agent.prompts, "decompose_spec never called its agent"
    return agent.prompts[0]


def test_the_architect_is_told_its_cwd_is_the_repository(tmp_path: Path) -> None:
    root, spec = _repo(tmp_path)
    prompt, cwd = _decompose_and_capture(root, spec)

    assert cwd == root
    assert "Your working directory IS the repository" in prompt
    assert "scripts/kstrl/codebase_map.md" in prompt

    # The repository branch is HARNESS text, so it must not be wrapped in
    # data delimiters and must sit BEFORE the first one. The sibling guard
    # tests/test_prompt_injection_guard.py:270 only ever renders the
    # NO-REPOSITORY branch, so it cannot see either mistake here.
    assert prompt.count(":BEGIN ") == 1
    assert prompt.count(":END ") == 1
    assert prompt.index("Your working directory IS the repository") < prompt.index(
        ":BEGIN SPECIFICATION"
    )

    # Exactly ONE injection-refusal paragraph. PR #397 shipped two and they
    # had already diverged, one escalating to `blocker` and the other
    # stopping at `major`.
    assert prompt.count("ignore previous instructions") == 1
    assert prompt.count("do NOT comply") == 1


def test_a_retargeted_codebase_map_is_the_path_the_architect_is_given(
    tmp_path: Path,
) -> None:
    root, spec = _repo(tmp_path)
    (root / "kstrl.toml").write_text('[paths]\ncodebase_map = "docs/map.md"\n', encoding="utf-8")
    prompt, _cwd = _decompose_and_capture(root, spec)

    assert "docs/map.md" in prompt
    assert "scripts/kstrl/codebase_map.md" not in prompt
    # The path handed over is ROOT-RELATIVE. Dropping relative_to_root and
    # passing str(config.codebase_map_file) leaves the absolute tmp path in
    # the prompt, and the first assertion above would still pass.
    assert str(root) not in prompt


def test_a_malformed_kstrl_toml_still_reaches_the_architect(tmp_path: Path) -> None:
    """#199 blocker fix: the config load ahead of the halt path is tolerant.

    `_decompose_spec_impl` loads KstrlConfig BEFORE the halt path that
    writes scripts/kstrl/spec-issues.json (kstrl/decompose.py:1499's
    EvolutionConfig.load_or_none is the same shape one call downstream).
    A malformed kstrl.toml must not raise ConfigError out of that load
    and abort decompose_spec before the halt path ever runs; it must
    degrade to the anchored default path instead, exactly the way
    tests/test_decompose.py::TestSpecConvergenceThroughDecompose::
    test_malformed_toml_does_not_cost_the_audit_artifact_either proves
    for the halt path itself.

    This uses an ESCALATED spec_issue (BLOCKER_ISSUE) rather than
    `_decompose_and_capture`'s normal VALID_DECOMPOSE_OUTPUT, and
    deliberately so, not as a simplification: a decompose that runs to
    a clean, non-halting completion proceeds past the halt path into
    `LinearConfig.load(root_dir)` (kstrl/decompose.py, right before the
    Linear sync section), which re-parses the SAME malformed
    kstrl.toml, unguarded, and raises `ConfigError` there instead - a
    real, separate, pre-existing call site this blocker fix does not
    touch (only `KstrlConfig.load` at the earlier line was named).
    `test_malformed_toml_does_not_cost_the_audit_artifact_either`'s own
    docstring already records that this second failure point exists on
    the direct in-process call (`ks decompose` itself never reaches it,
    because `config_preflight` rejects the file at command entry
    first). Scoping this test to the halt path, the way that sibling
    test already does, is what isolates the one thing blocker 1 is
    about: the architect prompt is built and delivered - with the
    default map path, because the retarget in kstrl.toml could not be
    read - before ANY config load gets a chance to abort the run.
    """
    root, spec = _repo(tmp_path)
    (root / "kstrl.toml").write_text('[paths\ncodebase_map = "docs/map.md"\n', encoding="utf-8")
    prompt = _decompose_halting_and_capture(root, spec)
    assert "Your working directory IS the repository" in prompt
    assert "scripts/kstrl/codebase_map.md" in prompt


def test_a_type_error_from_kstrl_toml_still_reaches_the_architect(tmp_path: Path) -> None:
    """Pins the TypeError arm: int() coercion one layer above the parse.

    `[run] max_iterations = [1, 2]` parses as valid TOML; the failure is
    `_apply_toml_overrides`'s `int(run["max_iterations"])` raising
    TypeError on the array, which `load_or_anchored`'s except clause
    must still catch so the architect prompt is built and delivered
    before this aborts decompose_spec.
    """
    root, spec = _repo(tmp_path)
    (root / "kstrl.toml").write_text("[run]\nmax_iterations = [1, 2]\n", encoding="utf-8")
    prompt = _decompose_halting_and_capture(root, spec)
    assert "Your working directory IS the repository" in prompt
    assert "scripts/kstrl/codebase_map.md" in prompt


def test_an_unreadable_kstrl_toml_still_reaches_the_architect(tmp_path: Path) -> None:
    """Pins the OSError arm: a kstrl.toml that exists but cannot be read.

    A kstrl.toml created as a directory raises IsADirectoryError (an
    OSError) out of load_toml_document's read, which is hoisted outside
    its parse guard. load_or_anchored's except clause must still catch
    it so the architect prompt is built and delivered before this
    aborts decompose_spec.
    """
    root, spec = _repo(tmp_path)
    (root / "kstrl.toml").mkdir()
    prompt = _decompose_halting_and_capture(root, spec)
    assert "Your working directory IS the repository" in prompt
    assert "scripts/kstrl/codebase_map.md" in prompt


def test_a_caller_with_no_repository_is_told_so() -> None:
    prompt = build_decompose_prompt("fixture", "# Spec\nBuild it.\n")

    assert "No repository is available to you" in prompt
    assert "codebase_map" not in prompt
    assert "Your working directory IS the repository" not in prompt
    assert prompt.count("ignore previous instructions") == 1


def test_the_repo_source_body_reaches_the_architect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(decompose, "ARCHITECT_REPO_SOURCE_PROMPT", _ORPHAN_MARKER)
    root, spec = _repo(tmp_path)
    prompt, _cwd = _decompose_and_capture(root, spec)
    assert _MARKER_HEAD in prompt and _MARKER_TAIL in prompt, (
        "the architect prompt no longer carries ARCHITECT_REPO_SOURCE_PROMPT's "
        "enrolled body, so its repository-reading instructions are outside "
        "H3 snapshot protection."
    )


def test_ks_init_scaffolds_a_codebase_map_the_ledger_recognises(tmp_path: Path) -> None:
    from kstrl.init_cmd import classify_scaffold
    from tests.test_init_cmd import run_init_capturing

    run_init_capturing(tmp_path)
    states = {s.template.filename: s for s in classify_scaffold(tmp_path)}
    assert "codebase_map.md" in states, (
        "SCAFFOLDED_TEMPLATES has no row for codebase_map.md, so nothing "
        "can tell an untouched ks init copy from a real map."
    )
    assert states["codebase_map.md"].status == "current"


def test_a_repository_with_no_map_still_gets_the_repository_branch(
    tmp_path: Path,
) -> None:
    root, spec = _repo(tmp_path, with_map=False)
    prompt, _cwd = _decompose_and_capture(root, spec)
    assert "Your working directory IS the repository" in prompt
    assert "scripts/kstrl/codebase_map.md" in prompt
    assert "No repository is available to you" not in prompt
