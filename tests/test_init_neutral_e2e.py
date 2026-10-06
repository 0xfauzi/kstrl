"""`ks init` writes and prints the same thing on every tree (#696 slice 6).

kstrl holds no language-specific code. `ks init` reads no build manifest,
so a Cargo repository, a pyproject repository and an empty one get the
same transcript, the same files and nothing staged. Each seed also holds
an untracked lockfile, which an init that read a language staged.

Every test drives the real CLI (`python -m kstrl init`) in a subprocess
and reads what it printed, the files it wrote and git's index.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from tests.helpers.gitrepo import git_in, set_identity

#: Seed name -> (files committed before init, files left untracked).
SEEDS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "cargo": (
        {"Cargo.toml": '[package]\nname = "rustapp"\nversion = "0.1.0"\n'},
        {"Cargo.lock": "version = 3\n"},
    ),
    "pyproject": (
        {"pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n'},
        {"uv.lock": "version = 1\n"},
    ),
    "empty": ({}, {}),
}


def ks_init(root: Path) -> subprocess.CompletedProcess[str]:
    """`python -m kstrl init . --ui plain --no-color` in ``root``, output merged."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env["KSTRL_NO_TUI"] = "1"
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "80"
    return subprocess.run(
        [sys.executable, "-m", "kstrl", "init", ".", "--ui", "plain", "--no-color"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        timeout=120,
    )


def seeded_repo(parent: Path, committed: dict[str, str], untracked: dict[str, str]) -> Path:
    """A git repository named ``proj``, so the project name cannot differ."""
    root = parent / "proj"
    root.mkdir(parents=True)
    git_in(root, "init", "-q", "-b", "main")
    set_identity(root)
    for rel, text in committed.items():
        (root / rel).write_text(text, encoding="utf-8")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-q", "--allow-empty", "-m", "seed")
    for rel, text in untracked.items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


@dataclass(frozen=True)
class InitSurface:
    """What one `ks init` printed, wrote and staged."""

    exit: int
    transcript: str
    files: dict[str, str]
    staged: str


def init_surface(root: Path, seeded: set[str]) -> InitSurface:
    """Everything `ks init` said and did in ``root``, with the path made neutral."""
    proc = ks_init(root)
    transcript = proc.stdout
    for spelling in sorted({str(root.resolve()), str(root)}, key=len, reverse=True):
        transcript = transcript.replace(spelling, "<root>")
    written: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/") or rel in seeded or path.is_dir():
            continue
        written[rel] = (
            f"-> {os.readlink(path)}" if path.is_symlink() else path.read_text(encoding="utf-8")
        )
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    ).stdout
    return InitSurface(proc.returncode, transcript, written, staged)


def _lines(value: object) -> list[str]:
    """``value`` as lines a diff can read: a file table is one block per file."""
    if isinstance(value, dict):
        return [line for rel, text in value.items() for line in (f"--- {rel}", *text.splitlines())]
    return str(value).splitlines()


def test_ks_init_is_identical_on_cargo_pyproject_and_empty_repos(tmp_path: Path) -> None:
    surfaces = {
        name: init_surface(
            seeded_repo(tmp_path / name, committed, untracked), {*committed, *untracked}
        )
        for name, (committed, untracked) in SEEDS.items()
    }

    cargo = surfaces["cargo"]
    assert cargo.exit == 0, cargo.transcript
    assert "Created CLAUDE.md" in cargo.transcript, cargo.transcript
    assert {"kstrl.toml", "CLAUDE.md", "AGENTS.md", ".gitignore"} <= set(cargo.files), cargo.files
    assert cargo.staged == "", cargo.staged
    for name in ("pyproject", "empty"):
        mine, theirs = asdict(cargo), asdict(surfaces[name])
        for key, value in mine.items():
            if theirs[key] != value:
                diff = difflib.unified_diff(
                    _lines(value),
                    _lines(theirs[key]),
                    "cargo",
                    name,
                    lineterm="",
                    n=1,
                )
                pytest.fail(f"ks init {key} differs between cargo and {name}:\n" + "\n".join(diff))


def test_ks_init_leaves_an_existing_claude_md_byte_unchanged(tmp_path: Path) -> None:
    root = seeded_repo(tmp_path, {}, {})
    mine = b"# Hand written\n\nNo standards section here, and no trailing newline"
    (root / "CLAUDE.md").write_bytes(mine)

    proc = ks_init(root)

    assert proc.returncode == 0, proc.stdout
    assert "CLAUDE.md already exists" in proc.stdout, proc.stdout
    assert (root / "CLAUDE.md").read_bytes() == mine
