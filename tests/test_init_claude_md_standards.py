"""`ks init` writes coding standards and antipatterns for every language it detects (#633).

Before #633 the generated CLAUDE.md had no "## Coding Standards" and no
"## What NOT To Do" section for a JavaScript project, and no "## What NOT
To Do" section for a Java or Kotlin project, because those languages had
no row in `_LANGUAGE_STANDARDS` / `_LANGUAGE_ANTIPATTERNS`. CLAUDE.md is
prepended into the engineer prompt, so a missing section is text the
engineer never receives.

Every test drives the real CLI (`python -m kstrl init`) in a subprocess
and reads the file it writes.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.gitrepo import git_in
from tests.test_language_ignores import MANIFEST_FOR

STANDARDS = "## Coding Standards"
ANTIPATTERNS = "## What NOT To Do"

#: sha256 of the CLAUDE.md `ks init` wrote for a Python project named
#: "demo" with no framework, captured on a96a986e before #633 changed any
#: text. #633 adds rows for other languages only, so this must not move.
PYTHON_GOLDEN_SHA256 = "5d163483212e415d4d186f8595e09a96994dec62df67528da7a1b0d289b2f76c"


def ks_init(root: Path) -> subprocess.CompletedProcess[str]:
    """`python -m kstrl init <root> --ui plain --no-color`, output merged."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env["KSTRL_NO_TUI"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "kstrl", "init", str(root), "--ui", "plain", "--no-color"],
        cwd=root.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        timeout=120,
    )


def repo_with_manifest(tmp_path: Path, language: str) -> Path:
    """A git repository holding only the build manifest for ``language``."""
    root = tmp_path / "repo"
    root.mkdir()
    git_in(root, "init", "-q", "-b", "main")
    name, body = MANIFEST_FOR[language]
    (root / name).write_text(body, encoding="utf-8")
    return root


def first_line_after(lines: list[str], heading: str) -> str:
    """The first non-empty line below ``heading``."""
    index = lines.index(heading)
    return next(line for line in lines[index + 1 :] if line.strip())


@pytest.mark.parametrize("language", sorted(MANIFEST_FOR))
def test_ks_init_writes_standards_and_antipatterns_for_every_detected_language(
    tmp_path: Path, language: str
) -> None:
    root = repo_with_manifest(tmp_path, language)

    proc = ks_init(root)

    assert proc.returncode == 0, proc.stdout
    assert f"Detected language: {language}" in proc.stdout, proc.stdout
    assert "Created CLAUDE.md" in proc.stdout, proc.stdout
    lines = (root / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    for heading in (STANDARDS, ANTIPATTERNS):
        assert heading in lines, f"{language}: CLAUDE.md has no {heading!r} section"
        assert first_line_after(lines, heading).startswith("- "), (
            f"{language}: {heading!r} is not followed by a bullet list"
        )


def test_ks_init_leaves_an_existing_claude_md_byte_unchanged(tmp_path: Path) -> None:
    root = repo_with_manifest(tmp_path, "JavaScript")
    mine = b"# Hand written\n\nNo standards section here, and no trailing newline"
    (root / "CLAUDE.md").write_bytes(mine)

    proc = ks_init(root)

    assert proc.returncode == 0, proc.stdout
    assert "CLAUDE.md already exists" in proc.stdout, proc.stdout
    assert (root / "CLAUDE.md").read_bytes() == mine


def test_ks_init_python_claude_md_is_the_pre_633_golden(tmp_path: Path) -> None:
    root = repo_with_manifest(tmp_path, "Python")

    proc = ks_init(root)

    assert proc.returncode == 0, proc.stdout
    text = (root / "CLAUDE.md").read_bytes()
    assert hashlib.sha256(text).hexdigest() == PYTHON_GOLDEN_SHA256, text.decode("utf-8")
