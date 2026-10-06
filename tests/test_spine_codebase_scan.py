"""#626 and #696: the codebase scan the engineer receives, captured off the
real agent subprocess's stdin.

Each fixture repository is a real git repository shaped like a kstrl project.
``_run_component``, the function the factory scheduler submits to its worker
pool, cuts a real worktree for it and runs a fake agent BINARY that writes the
prompt it received to disk. Every assertion below reads that file, so what is
tested is what the engineer is sent, not what a builder returns.
"""

from __future__ import annotations

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

#: The scan on, at the shipped default budget.
FULL_SCAN: dict[str, object] = {"enabled": True, "module_map": True, "max_context_tokens": 4000}


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
    _write(root / "Cargo.toml", '[package]\nname = "rustapp"\nversion = "0.1.0"\n')
    _write(root / "src/main.rs", "mod util;\n\nfn main() {\n    util::run();\n}\n")
    _write(root / "src/util.rs", "pub fn run() {}\n")
    _write(root / "src/bin/tool.rs", "fn main() {}\n")


def _typescript_with_ignored_build_output(root: Path) -> None:
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("dist/\n")
    _write(root / "package.json", '{"name": "webapp", "type": "module"}\n')
    _write(root / "src/app.ts", "export function app(): number {\n  return 1;\n}\n")
    for name in ("a", "b", "c"):
        _write(root / "dist" / f"{name}.js", f"export const {name} = 1;\n")


def test_the_module_map_counts_every_file_git_lists_whatever_its_language(
    tmp_path: Path,
) -> None:
    """#696 slice 6: the scan reads no source language. A Rust tree's
    source is counted the way a Python tree's was, and the block holds
    the module map and nothing else: no section that reads one language."""
    prompt = _prompt_for(tmp_path, _rust)
    start = prompt.index("=== CODEBASE CONTEXT (auto-generated) ===")
    end = prompt.index("=== END CODEBASE CONTEXT ===") + len("=== END CODEBASE CONTEXT ===")
    assert prompt[start:end] == RUST_BLOCK, prompt[start:end]


def test_a_gitignored_dist_directory_is_not_in_the_module_map(tmp_path: Path) -> None:
    """Build output the repository ignores is not source under change. The
    walk reads the filesystem, so before #626 the three ignored dist/*.js
    files were counted as a source directory."""
    prompt = _prompt_for(tmp_path, _typescript_with_ignored_build_output)
    module_map = section(prompt, "## Module map")
    assert "src/" in module_map, prompt
    assert "dist/" not in module_map, module_map


#: Captured by running this test's fixture on the #696 slice 6 tree.
RUST_BLOCK = (
    "=== CODEBASE CONTEXT (auto-generated) ===\n"
    "\n"
    "## Module map\n"
    "./                     # 3 files, 6 lines\n"
    "  src/                 # 2 files, 6 lines\n"
    "    bin/                 # 1 files, 1 lines\n"
    "\n"
    "=== END CODEBASE CONTEXT ==="
)
