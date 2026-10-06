"""Phase 0: the codebase scan, a module map of the tree pasted into the engineer prompt.

All analysis is computational (no LLM calls). The map counts every file
git lists, whatever its suffix: kstrl reads no source language (#696).
The engineer reads the code itself with its own tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kstrl import git
from kstrl.config_numbers import check_numbers

# Max directories in module map to avoid bloat
_MAX_MODULE_MAP_DIRS = 50

# The block's own header and footer, charged once per assembled block.
_HEADER_FOOTER_OVERHEAD = len("=== CODEBASE CONTEXT (auto-generated) ===\n\n") + len(
    "\n=== END CODEBASE CONTEXT ==="
)


# --- Engineer-facing notice (H3) -------------------------------------------
#
# Everything this module returns is pasted into the engineer prompt, so the
# notice below is read by a model as part of its instructions. #428 enrolled
# the scan's notices so a reword has to move a hash and a version with it.
# 2.0.0 (#696 slice 6): the public interfaces, the dependency graph and the
# conventions are gone, and their six notices with them; this one is left.
CODEBASE_SCAN_NOTICE_PROMPT_VERSION = "2.0.0"

SECTION_FAILED_PROMPT = "(none: {heading} failed: {error_type}: {error})"


@dataclass
class CodebaseScanConfig:
    """Configuration for codebase scan context generation."""

    enabled: bool = True
    module_map: bool = True  # directory tree with file and line counts
    max_context_tokens: int = 4000  # rough cap (estimate 4 chars per token)

    @classmethod
    def from_env(cls) -> CodebaseScanConfig:
        """Load codebase scan config from environment variables only."""
        config = cls()
        _apply_env_overrides(config)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> CodebaseScanConfig:
        """Load codebase scan config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "codebase_scan")
        for key in ("enabled", "module_map"):
            if key in section:
                setattr(config, key, bool(section[key]))
        if "max_context_tokens" in section:
            config.max_context_tokens = int(section["max_context_tokens"])
        _apply_env_overrides(config)
        return check_numbers(config)


_ENV_MAP: dict[str, tuple[str, type]] = {
    "KSTRL_CODEBASE_SCAN_ENABLED": ("enabled", bool),
    "KSTRL_CODEBASE_SCAN_MODULE_MAP": ("module_map", bool),
    "KSTRL_CODEBASE_SCAN_MAX_TOKENS": ("max_context_tokens", int),
}


def _apply_env_overrides(config: CodebaseScanConfig) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""

    for env_key, (field_name, caster) in _ENV_MAP.items():
        if env_key in os.environ:
            raw = os.environ[env_key]
            if caster is bool:
                setattr(config, field_name, raw.lower() in {"1", "true", "yes"})
            else:
                setattr(config, field_name, caster(raw))


def _is_hidden(name: str) -> bool:
    """Check if a file or directory name is hidden (starts with dot)."""
    return name.startswith(".")


def _count_lines(path: Path) -> int:
    """Count lines in a file. Returns 0 on any error."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        return 0


@dataclass(frozen=True)
class _GitListing:
    """What git lists under a root: its files, and every directory holding one."""

    files: frozenset[Path]
    dirs: frozenset[Path]


def _git_listing(root: Path) -> _GitListing | None:
    """The files git lists under *root*, or None when git could not answer.

    What git lists is its own view of the tree: tracked files, and
    untracked files that no ignore rule matches. Build output the
    repository ignores (`dist/`, `target/`) is absent from it (#626).
    """
    listed = git.listed_files(root)
    if listed is None:
        return None
    files: set[Path] = set()
    dirs: set[Path] = set()
    for rel in listed:
        parts = PurePosixPath(rel).parts
        files.add(root.joinpath(*parts))
        dirs.update(root.joinpath(*parts[:depth]) for depth in range(1, len(parts)))
    return _GitListing(frozenset(files), frozenset(dirs))


def _enters(subdir: Path, listing: _GitListing | None) -> bool:
    """Whether the module-map walk descends into *subdir*.

    With an answer from git, only a directory holding a listed file.
    Without one, every directory whose name does not start with a dot.
    """
    if _is_hidden(subdir.name):
        return False
    return listing is None or subdir in listing.dirs


def _counts(path: Path, listing: _GitListing | None) -> bool:
    """Whether the module-map walk counts the file at *path*: every file git
    lists, whatever its suffix, or every file when git could not answer."""
    return listing is None or path in listing.files


def _walk_source_dirs(root: Path) -> list[tuple[Path, int, int]]:
    """Walk directory tree and collect directories with file/LOC counts.

    Returns list of (dir_path, file_count, line_count) tuples,
    sorted by path depth then alphabetically. Capped at _MAX_MODULE_MAP_DIRS.

    Inside a git repository only what git lists is counted, so an
    ignored directory is never entered.
    """
    results: list[tuple[Path, int, int]] = []
    listing = _git_listing(root)

    def _walk(directory: Path) -> None:
        if len(results) >= _MAX_MODULE_MAP_DIRS:
            return

        try:
            entries = sorted(directory.iterdir(), key=lambda e: e.name)
        except PermissionError:
            return

        file_count = 0
        line_count = 0

        subdirs: list[Path] = []
        for entry in entries:
            if entry.is_dir():
                if _enters(entry, listing):
                    subdirs.append(entry)
            elif entry.is_file() and _counts(entry, listing):
                file_count += 1
                line_count += _count_lines(entry)

        if file_count > 0:
            results.append((directory, file_count, line_count))

        for subdir in subdirs:
            _walk(subdir)

    _walk(root)
    return results


def build_module_map(root: Path) -> str:
    """Build an indented tree of directories with file and LOC counts.

    Skips hidden directories (``.git``, ``.kstrl``) and whatever git
    ignores. Caps at 50 directories.
    """
    entries = _walk_source_dirs(root)
    if not entries:
        return ""

    lines: list[str] = []
    for dir_path, file_count, line_count in entries:
        try:
            rel = dir_path.relative_to(root)
        except ValueError:
            continue

        depth = len(rel.parts)
        indent = "  " * depth
        dir_name = rel.as_posix() + "/" if depth > 0 else "./"

        if depth > 0:
            dir_name = rel.name + "/"
            # Build proper indented name
            lines.append(f"{indent}{dir_name:<20s} # {file_count} files, {line_count} lines")
        else:
            lines.append(f"{dir_name:<22s} # {file_count} files, {line_count} lines")

    return "\n".join(lines)


def _module_map_section(root: Path) -> str:
    """The module map's body, or the line recording why building it failed.

    A crash does not take the engineer's prompt down, and it is not
    dropped either: the failure is the section's content (#378).
    """
    try:
        return build_module_map(root)
    except Exception as exc:
        return SECTION_FAILED_PROMPT.format(
            heading="module map", error_type=type(exc).__name__, error=exc
        )


def build_codebase_scan_context(
    worktree_path: Path,
    config: CodebaseScanConfig | None = None,
) -> str:
    """Build the codebase scan context string for agent prompt injection.

    Main entry point: the module map under header and footer markers,
    cut to ``max_context_tokens`` (estimated at 4 characters a token).
    """
    if config is None:
        config = CodebaseScanConfig()

    if not config.enabled or not config.module_map:
        return ""

    content = _module_map_section(worktree_path)
    if not content:
        return ""

    sections = _truncate_to_budget([("Module map", content)], config.max_context_tokens * 4)
    if not sections:
        return ""

    parts: list[str] = ["=== CODEBASE CONTEXT (auto-generated) ===", ""]
    for heading, body in sections:
        parts.append(f"## {heading}")
        parts.append(body)
        parts.append("")
    parts.append("=== END CODEBASE CONTEXT ===")
    return "\n".join(parts)


def _total_chars(sections: list[tuple[str, str]]) -> int:
    """Characters the assembled context block would occupy."""
    total = _HEADER_FOOTER_OVERHEAD
    for heading, content in sections:
        total += len(f"## {heading}\n") + len(content) + len("\n\n")
    return total


def _truncate_to_budget(
    sections: list[tuple[str, str]],
    max_chars: int,
) -> list[tuple[str, str]]:
    """Truncate sections to fit within a character budget.

    Drops lowest-priority sections first (last in list).
    If still over budget after dropping all but one section,
    truncates the remaining section content.
    """
    # Drop lowest-priority sections (end of list) until under budget
    while sections and _total_chars(sections) > max_chars:
        if len(sections) == 1:
            # Last section - truncate content instead of dropping it
            heading, content = sections[0]
            available = max_chars - _HEADER_FOOTER_OVERHEAD - len(f"## {heading}\n") - len("\n\n")
            if available > 100:
                truncated = content[: available - 20] + "\n... (truncated)"
                sections[0] = (heading, truncated)
            else:
                sections = []
            break
        sections.pop()

    return sections
