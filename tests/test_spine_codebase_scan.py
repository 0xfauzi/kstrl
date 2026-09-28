"""#626: the codebase scan the engineer receives on a Rust, Go or TypeScript
repository, captured off the real agent subprocess's stdin.

Each fixture repository is a real git repository shaped like a kstrl project.
``_run_component``, the function the factory scheduler submits to its worker
pool, cuts a real worktree for it and runs a fake agent BINARY that writes the
prompt it received to disk. Every assertion below reads that file, so what is
tested is what the engineer is sent, not what a builder returns.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from kstrl.factory import _run_component, _setup_worktree
from kstrl.ui.plain import PlainUI
from tests.helpers.context_sweeps import section
from tests.spine_utils import COMPLETE_LINE, init_kstrl_repo

pytestmark = pytest.mark.spine

COMP = "comp-a"
BRANCH = f"kstrl/factory/{COMP}"
PRD_REL = f"scripts/kstrl/feature/{COMP}/prd.json"
PROMPT_REL = "scripts/kstrl/prompt.md"
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every section on, at the shipped default budget.
FULL_SCAN: dict[str, object] = {
    "enabled": True,
    "module_map": True,
    "public_interfaces": True,
    "dependency_graph": True,
    "conventions": True,
    "max_context_tokens": 4000,
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _prompt_for(base: Path, write: Callable[[Path], None]) -> str:
    """The prompt the engineer receives for a worktree *write* fills.

    The files are written into the worktree after it is cut, untracked,
    which is how git lists an engineer's new files too.
    """
    root = base / "repo"
    init_kstrl_repo(root, (COMP,))
    worktree = _setup_worktree(
        COMP, BRANCH, "main", root, "spine-run-scan", ui=PlainUI(no_color=True)
    )
    write(worktree)

    capture = base / "capture"
    capture.mkdir()
    agent_bin = base / "bin" / "fake-agent"
    agent_bin.parent.mkdir()
    agent_bin.write_text(f"#!/bin/bash\ncat > '{capture}/prompt.txt'\n{COMPLETE_LINE}\n")
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
        codebase_scan_config_dict=FULL_SCAN,
        run_id="test-run",
    )
    return (capture / "prompt.txt").read_text(encoding="utf-8")


def _rust(root: Path) -> None:
    # rust-version from [package], edition from [workspace.package], so
    # dropping either table from the reader loses a line. [package] also
    # inherits its edition with `edition.workspace = true`, a table and not
    # a value, so a reader that takes any value there prints the table.
    _write(
        root / "Cargo.toml",
        "[package]\n"
        'name = "rustapp"\n'
        'version = "0.1.0"\n'
        "edition.workspace = true\n"
        'rust-version = "1.74"\n'
        "\n"
        "[workspace.package]\n"
        'edition = "2021"\n'
        "\n"
        "[lints.clippy]\n"
        'pedantic = "warn"\n',
    )
    _write(root / "rustfmt.toml", "max_width = 100\n")
    _write(root / "src/main.rs", "mod util;\n\nfn main() {\n    util::run();\n}\n")
    _write(root / "src/util.rs", "pub fn run() {}\n")


def _go(root: Path) -> None:
    _write(root / "go.mod", "module example.com/svc\n\ngo 1.22\n")
    _write(root / "main.go", "package main\n\nfunc main() {}\n")


def _typescript_with_ignored_build_output(root: Path) -> None:
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("dist/\n")
    _write(root / "package.json", '{"name": "webapp", "type": "module"}\n')
    _write(root / "src/app.ts", "export function app(): number {\n  return 1;\n}\n")
    for name in ("a", "b", "c"):
        _write(root / "dist" / f"{name}.js", f"export const {name} = 1;\n")


def _uv_python(root: Path) -> None:
    example = REPO_ROOT / "examples" / "uv-python"
    shutil.copytree(example / "src", root / "src")
    shutil.copytree(example / "tests", root / "tests")
    shutil.copy2(example / "pyproject.toml", root / "pyproject.toml")


@pytest.fixture(scope="module")
def rust_prompt(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _prompt_for(tmp_path_factory.mktemp("rust"), _rust)


@pytest.fixture(scope="module")
def go_prompt(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _prompt_for(tmp_path_factory.mktemp("go"), _go)


def test_a_rust_tree_gets_a_dependency_graph_notice_not_an_absent_section(
    rust_prompt: str,
) -> None:
    """The graph is built from Python imports only. On a tree with none it
    used to return "" and the heading vanished, which reads exactly like a
    stage that never ran."""
    graph = section(rust_prompt, "## Dependency graph")
    assert graph.startswith("(none: "), rust_prompt
    assert "Python" in graph, graph
    assert "0 .py files" in graph, graph


def test_the_source_root_notice_does_not_say_the_repo_lacks_python_when_it_has_rust(
    rust_prompt: str,
) -> None:
    interfaces = section(rust_prompt, "## Public interfaces")
    assert interfaces.startswith("(none: "), rust_prompt
    assert "no Python source root found" not in interfaces, interfaces
    assert "read from Python source only" in interfaces, interfaces
    assert "other languages is not summarised" in interfaces, interfaces


def test_cargo_toml_edition_and_rust_version_are_listed_as_conventions(
    rust_prompt: str,
) -> None:
    conventions = section(rust_prompt, "## Conventions")
    assert "- Rust edition: 2021" in conventions.splitlines(), rust_prompt
    assert "- Minimum Rust version: 1.74" in conventions.splitlines(), rust_prompt


def test_cargo_lint_tables_and_rustfmt_width_are_listed_as_conventions(
    rust_prompt: str,
) -> None:
    conventions = section(rust_prompt, "## Conventions")
    assert conventions.splitlines() == [
        "- Rust edition: 2021",
        "- Minimum Rust version: 1.74",
        "- Cargo lint tables: clippy",
        "- Max width (rustfmt.toml): 100",
    ], rust_prompt


def test_go_mod_version_is_listed_as_a_convention(go_prompt: str) -> None:
    conventions = section(go_prompt, "## Conventions")
    assert conventions.splitlines() == ["- Go version: 1.22"], go_prompt


def test_a_gitignored_dist_directory_is_not_in_the_module_map(tmp_path: Path) -> None:
    """Build output the repository ignores is not source under change. The
    walk reads the filesystem, so before #626 the three ignored dist/*.js
    files were counted as a source directory."""
    prompt = _prompt_for(tmp_path, _typescript_with_ignored_build_output)
    module_map = section(prompt, "## Module map")
    assert "src/" in module_map, prompt
    assert "dist/" not in module_map, module_map


def test_python_context_is_unchanged(tmp_path: Path) -> None:
    """Control: the Python example gets byte-for-byte the block main gave it."""
    prompt = _prompt_for(tmp_path, _uv_python)
    start = prompt.index("=== CODEBASE CONTEXT (auto-generated) ===")
    end = prompt.index("=== END CODEBASE CONTEXT ===") + len("=== END CODEBASE CONTEXT ===")
    assert prompt[start:end] == PYTHON_BLOCK_ON_MAIN, prompt[start:end]


#: Captured from origin/main f2523883 by running this test's fixture there.
PYTHON_BLOCK_ON_MAIN = (
    "=== CODEBASE CONTEXT (auto-generated) ===\n"
    "\n"
    "## Module map\n"
    "    kstrl_uv_example/    # 2 files, 31 lines\n"
    "  tests/               # 1 files, 11 lines\n"
    "\n"
    "## Dependency graph\n"
    "cli -> kstrl_uv_example (imports: greet)\n"
    "\n"
    "## Public interfaces\n"
    "src/kstrl_uv_example/cli.py: def build_parser() -> argparse.ArgumentParser, "
    "def main(argv: list[str] | None) -> int\n"
    "\n"
    "## Conventions\n"
    "- Python version: >=3.11\n"
    "- Line length (ruff): 100\n"
    "- Target version (ruff): py311\n"
    "- Ruff rules: E, F, I, UP, B\n"
    "\n"
    "=== END CODEBASE CONTEXT ==="
)
