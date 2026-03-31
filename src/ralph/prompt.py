"""Template loading and project scaffolding for Ralph."""

from __future__ import annotations

from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "templates"

SCAFFOLD_FILES: dict[str, str] = {
    "prompt.md": "prompt.md",
    "prd.json": None,  # type: ignore[dict-item]  # generated, not a template
    "progress.txt": "progress.txt",
    "codebase_map.md": "codebase_map.md",
    "understand_prompt.md": "understand_prompt.md",
    "prd_prompt.txt": "prd_prompt.txt",
}

DEFAULT_PRD = """\
{
  "branchName": "ralph/feature",
  "userStories": []
}
"""


def get_template(name: str) -> str:
    """Load a bundled template file by name."""
    path = TEMPLATE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {name}")
    return path.read_text(encoding="utf-8")


def get_template_path(name: str) -> Path:
    """Return the Path to a bundled template file."""
    path = TEMPLATE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {name}")
    return path


def scaffold_project(target_dir: Path) -> list[str]:
    """Copy all Ralph template files to scripts/ralph/ in the target directory.

    Creates the directory structure if it doesn't exist.
    Returns list of created/skipped file messages.
    """
    ralph_dir = target_dir / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)

    messages: list[str] = []

    for dest_name, template_name in SCAFFOLD_FILES.items():
        dest_path = ralph_dir / dest_name
        if dest_path.exists():
            messages.append(f"Exists: {dest_path.relative_to(target_dir)}")
            continue

        if template_name is None:
            # Special case: prd.json gets a generated default
            dest_path.write_text(DEFAULT_PRD, encoding="utf-8")
        else:
            content = get_template(template_name)
            dest_path.write_text(content, encoding="utf-8")

        messages.append(f"Created: {dest_path.relative_to(target_dir)}")

    return messages


def list_templates() -> list[str]:
    """Return names of all available template files."""
    if not TEMPLATE_DIR.exists():
        return []
    return sorted(f.name for f in TEMPLATE_DIR.iterdir() if f.is_file())
