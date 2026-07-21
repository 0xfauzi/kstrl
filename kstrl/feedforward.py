"""Phase 0: Feedforward controls - structural analysis and convention extraction.

All analysis is computational (no LLM calls). Builds a context string
to prepend to the agent prompt before each component runs.
"""

from __future__ import annotations

import ast
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Directories to always skip during tree walks
_SKIP_DIRS = frozenset({
    "__pycache__", "node_modules", ".git", "venv", ".venv", ".kstrl",
})

# Source file extensions we care about
_SOURCE_EXTENSIONS = frozenset({
    ".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs",
})

# Max directories in module map to avoid bloat
_MAX_MODULE_MAP_DIRS = 50

# Max files to scan for public interfaces
_MAX_PUBLIC_INTERFACE_FILES = 30


@dataclass
class FeedforwardConfig:
    """Configuration for feedforward context generation."""

    enabled: bool = True
    module_map: bool = True          # directory tree with LOC counts
    public_interfaces: bool = True   # extract public symbols
    dependency_graph: bool = True    # import-based dependency analysis
    conventions: bool = True         # extract from config files
    max_context_tokens: int = 4000   # rough cap (estimate 4 chars per token)

    @classmethod
    def from_env(cls) -> FeedforwardConfig:
        """Load feedforward config from environment variables only."""
        config = cls()
        _apply_env_overrides(config)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FeedforwardConfig:
        """Load feedforward config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "feedforward")
        for key in (
            "enabled", "module_map", "public_interfaces",
            "dependency_graph", "conventions",
        ):
            if key in section:
                setattr(config, key, bool(section[key]))
        if "max_context_tokens" in section:
            config.max_context_tokens = int(section["max_context_tokens"])
        _apply_env_overrides(config)
        return config


_ENV_MAP: dict[str, tuple[str, type]] = {
    "KSTRL_FEEDFORWARD_ENABLED": ("enabled", bool),
    "KSTRL_FEEDFORWARD_MODULE_MAP": ("module_map", bool),
    "KSTRL_FEEDFORWARD_PUBLIC_INTERFACES": ("public_interfaces", bool),
    "KSTRL_FEEDFORWARD_DEPENDENCY_GRAPH": ("dependency_graph", bool),
    "KSTRL_FEEDFORWARD_CONVENTIONS": ("conventions", bool),
    "KSTRL_FEEDFORWARD_MAX_TOKENS": ("max_context_tokens", int),
}


def _apply_env_overrides(config: FeedforwardConfig) -> None:
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


def _should_skip_dir(name: str) -> bool:
    """Check if a directory should be skipped during traversal."""
    return name in _SKIP_DIRS or _is_hidden(name)


def _count_lines(path: Path) -> int:
    """Count lines in a file. Returns 0 on any error."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        return 0


def _walk_source_dirs(root: Path) -> list[tuple[Path, int, int]]:
    """Walk directory tree and collect source directories with file/LOC counts.

    Returns list of (dir_path, file_count, line_count) tuples,
    sorted by path depth then alphabetically. Capped at _MAX_MODULE_MAP_DIRS.
    """
    results: list[tuple[Path, int, int]] = []

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
                if not _should_skip_dir(entry.name):
                    subdirs.append(entry)
            elif entry.is_file() and entry.suffix in _SOURCE_EXTENSIONS:
                file_count += 1
                line_count += _count_lines(entry)

        if file_count > 0:
            results.append((directory, file_count, line_count))

        for subdir in subdirs:
            _walk(subdir)

    _walk(root)
    return results


def build_module_map(root: Path) -> str:
    """Build an indented tree of source directories with file and LOC counts.

    Skips hidden dirs, __pycache__, node_modules, .git, venv, .venv, .kstrl.
    Caps at 50 directories.
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
            lines.append(
                f"{indent}{dir_name:<20s} # {file_count} files, {line_count} lines"
            )
        else:
            lines.append(
                f"{dir_name:<22s} # {file_count} files, {line_count} lines"
            )

    return "\n".join(lines)


def _find_top_source_dirs(root: Path) -> list[Path]:
    """Find top-level source directories to scan for public interfaces.

    Looks for common patterns: src/, lib/, or a directory matching the project name.
    Falls back to any directory at root level that contains .py files.
    """
    candidates: list[Path] = []

    for name in ("src", "lib"):
        candidate = root / name
        if candidate.is_dir():
            candidates.append(candidate)

    # Look for package directories (contain __init__.py at root level)
    try:
        for entry in root.iterdir():
            if (
                entry.is_dir()
                and not _should_skip_dir(entry.name)
                and not _is_hidden(entry.name)
                and (entry / "__init__.py").exists()
                and entry not in candidates
            ):
                candidates.append(entry)
    except PermissionError:
        pass

    return candidates


def _extract_symbols_from_file(filepath: Path) -> list[str]:
    """Extract public class and function names from a Python file using ast.

    Returns formatted strings like:
      'class User'
      'def register_routes(app: FastAPI) -> None'
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, Exception):
        return []

    symbols: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                symbols.append(f"class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                sig = _format_function_signature(node)
                symbols.append(sig)

    return symbols


def _format_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format a function node into a readable signature string."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    params: list[str] = []

    for arg in node.args.args:
        name = arg.arg
        if name == "self" or name == "cls":
            continue
        if arg.annotation:
            try:
                ann = ast.unparse(arg.annotation)
                params.append(f"{name}: {ann}")
            except Exception:
                params.append(name)
        else:
            params.append(name)

    sig = f"{prefix} {node.name}({', '.join(params)})"

    if node.returns:
        try:
            ret = ast.unparse(node.returns)
            sig += f" -> {ret}"
        except Exception:
            pass

    return sig


def extract_public_interfaces(root: Path) -> str:
    """Extract public classes and functions from Python files.

    Skips files starting with '_' or 'test'. Only scans top-level source
    directories. Caps at 30 files.
    """
    source_dirs = _find_top_source_dirs(root)
    if not source_dirs:
        return ""

    file_symbols: list[tuple[str, list[str]]] = []

    for src_dir in source_dirs:
        try:
            py_files = sorted(src_dir.rglob("*.py"))
        except PermissionError:
            continue

        for py_file in py_files:
            if len(file_symbols) >= _MAX_PUBLIC_INTERFACE_FILES:
                break

            # Skip private and test files
            if py_file.name.startswith("_") or py_file.name.startswith("test"):
                continue

            symbols = _extract_symbols_from_file(py_file)
            if symbols:
                try:
                    rel = py_file.relative_to(root)
                except ValueError:
                    continue
                file_symbols.append((rel.as_posix(), symbols))

    if not file_symbols:
        return ""

    lines: list[str] = []
    for filepath, symbols in file_symbols:
        lines.append(f"{filepath}: {', '.join(symbols)}")

    return "\n".join(lines)


def _find_project_package_names(root: Path) -> set[str]:
    """Identify the project's own package names for internal import detection.

    Looks for directories with __init__.py and src/ subdirectories.
    """
    packages: set[str] = set()

    # Check for top-level packages
    try:
        for entry in root.iterdir():
            if (
                entry.is_dir()
                and not _should_skip_dir(entry.name)
                and not _is_hidden(entry.name)
                and (entry / "__init__.py").exists()
            ):
                packages.add(entry.name)
    except PermissionError:
        pass

    # Check src/ for packages
    src = root / "src"
    if src.is_dir():
        try:
            for entry in src.iterdir():
                if entry.is_dir() and (entry / "__init__.py").exists():
                    packages.add(entry.name)
        except PermissionError:
            pass

    return packages


def build_dependency_graph(root: Path) -> str:
    """Build a module-level dependency graph from Python imports.

    Only tracks internal imports (within the project). Parses all .py files
    and builds edges between modules.
    """
    packages = _find_project_package_names(root)
    if not packages:
        return ""

    # Collect all .py files in project packages
    all_py_files: list[Path] = []
    for pkg_name in packages:
        pkg_dir = root / pkg_name
        if pkg_dir.is_dir():
            try:
                all_py_files.extend(sorted(pkg_dir.rglob("*.py")))
            except PermissionError:
                pass

    src = root / "src"
    if src.is_dir():
        for pkg_name in packages:
            pkg_dir = src / pkg_name
            if pkg_dir.is_dir():
                try:
                    all_py_files.extend(sorted(pkg_dir.rglob("*.py")))
                except PermissionError:
                    pass

    # Parse imports from each file
    # edges: dict of (source_module -> dict of target_module -> set of imported names)
    edges: dict[str, dict[str, set[str]]] = {}

    for py_file in all_py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, Exception):
            continue

        # Determine the module name for this file
        source_module = _path_to_module(py_file, root, packages)
        if not source_module:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                top_level = node.module.split(".")[0]
                if top_level not in packages:
                    continue

                target_module = _simplify_module(node.module, packages)
                if target_module == source_module:
                    continue

                names = set()
                for alias in (node.names or []):
                    if alias.name != "*":
                        names.add(alias.name)

                if source_module not in edges:
                    edges[source_module] = {}
                if target_module not in edges[source_module]:
                    edges[source_module][target_module] = set()
                edges[source_module][target_module].update(names)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    if top_level not in packages:
                        continue
                    target_module = _simplify_module(alias.name, packages)
                    if target_module == source_module:
                        continue

                    if source_module not in edges:
                        edges[source_module] = {}
                    if target_module not in edges[source_module]:
                        edges[source_module][target_module] = set()

    if not edges:
        return ""

    lines: list[str] = []
    for src_mod in sorted(edges):
        for tgt_mod in sorted(edges[src_mod]):
            sorted_names = sorted(edges[src_mod][tgt_mod])
            if sorted_names:
                lines.append(
                    f"{src_mod} -> {tgt_mod} "
                    f"(imports: {', '.join(sorted_names)})"
                )
            else:
                lines.append(f"{src_mod} -> {tgt_mod}")

    return "\n".join(lines)


def _path_to_module(filepath: Path, root: Path, packages: set[str]) -> str | None:
    """Convert a file path to a simplified module name.

    For example: root/kstrl/factory.py -> 'factory'
    """
    try:
        rel = filepath.relative_to(root)
    except ValueError:
        return None

    parts = list(rel.parts)

    # Strip 'src/' prefix if present
    if parts and parts[0] == "src":
        parts = parts[1:]

    if not parts:
        return None

    # Strip the package name prefix
    if parts[0] in packages:
        parts = parts[1:]

    if not parts:
        return None

    # Convert file to module name
    module_parts = []
    for part in parts:
        if part.endswith(".py"):
            name = part[:-3]
            if name == "__init__":
                continue
            module_parts.append(name)
        else:
            module_parts.append(part)

    return ".".join(module_parts) if module_parts else None


def _simplify_module(module_path: str, packages: set[str]) -> str:
    """Simplify a dotted module path by stripping the top-level package.

    For example: 'kstrl.factory' -> 'factory'
    """
    parts = module_path.split(".")
    if parts and parts[0] in packages:
        parts = parts[1:]
    return ".".join(parts) if parts else module_path


def extract_conventions(root: Path) -> str:
    """Extract coding conventions from config files.

    Reads pyproject.toml, ruff.toml, .editorconfig, tsconfig.json, package.json.
    Returns a bullet-point list of discovered conventions.
    """
    bullets: list[str] = []

    _extract_pyproject_conventions(root, bullets)
    _extract_ruff_toml_conventions(root, bullets)
    _extract_editorconfig_conventions(root, bullets)
    _extract_tsconfig_conventions(root, bullets)
    _extract_package_json_conventions(root, bullets)

    if not bullets:
        return ""

    return "\n".join(f"- {b}" for b in bullets)


def _extract_pyproject_conventions(root: Path, bullets: list[str]) -> None:
    """Extract conventions from pyproject.toml."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    # Python version requirement
    try:
        requires_python = data.get("project", {}).get("requires-python")
        if requires_python:
            bullets.append(f"Python version: {requires_python}")
    except Exception:
        pass

    # Ruff config in pyproject.toml
    try:
        ruff = data.get("tool", {}).get("ruff", {})
        if "line-length" in ruff:
            bullets.append(f"Line length (ruff): {ruff['line-length']}")
        if "target-version" in ruff:
            bullets.append(f"Target version (ruff): {ruff['target-version']}")

        lint = ruff.get("lint", {})
        if "select" in lint:
            bullets.append(f"Ruff rules: {', '.join(lint['select'])}")
    except Exception:
        pass

    # Black config in pyproject.toml
    try:
        black = data.get("tool", {}).get("black", {})
        if "line-length" in black:
            bullets.append(f"Line length (black): {black['line-length']}")
        if "target-version" in black:
            versions = black["target-version"]
            if isinstance(versions, list):
                bullets.append(f"Target versions (black): {', '.join(versions)}")
    except Exception:
        pass


def _extract_ruff_toml_conventions(root: Path, bullets: list[str]) -> None:
    """Extract conventions from ruff.toml."""
    path = root / "ruff.toml"
    if not path.is_file():
        return

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    try:
        if "line-length" in data:
            bullets.append(f"Line length (ruff.toml): {data['line-length']}")
        if "target-version" in data:
            bullets.append(f"Target version (ruff.toml): {data['target-version']}")

        lint = data.get("lint", {})
        if "select" in lint:
            bullets.append(f"Ruff rules (ruff.toml): {', '.join(lint['select'])}")

        format_cfg = data.get("format", {})
        if "quote-style" in format_cfg:
            bullets.append(f"Quote style: {format_cfg['quote-style']}")
    except Exception:
        pass


def _extract_editorconfig_conventions(root: Path, bullets: list[str]) -> None:
    """Extract conventions from .editorconfig."""
    path = root / ".editorconfig"
    if not path.is_file():
        return

    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return

    # Simple .editorconfig parsing - just look for key values
    try:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#") or line.startswith(";") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()

            if key == "indent_style":
                bullets.append(f"Indent style: {value}")
            elif key == "indent_size":
                bullets.append(f"Indent size: {value}")
    except Exception:
        pass


def _extract_tsconfig_conventions(root: Path, bullets: list[str]) -> None:
    """Extract conventions from tsconfig.json."""
    path = root / "tsconfig.json"
    if not path.is_file():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    try:
        compiler = data.get("compilerOptions", {})
        if compiler.get("strict"):
            bullets.append("TypeScript strict mode: enabled")
        if "target" in compiler:
            bullets.append(f"TypeScript target: {compiler['target']}")
    except Exception:
        pass


def _extract_package_json_conventions(root: Path, bullets: list[str]) -> None:
    """Extract conventions from package.json."""
    path = root / "package.json"
    if not path.is_file():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    try:
        module_type = data.get("type")
        if module_type:
            bullets.append(f"Module type: {module_type}")
    except Exception:
        pass


def build_feedforward_context(
    worktree_path: Path,
    config: FeedforwardConfig | None = None,
    component_id: str = "",
    component_deps: list[str] | None = None,
) -> str:
    """Build the full feedforward context string for agent prompt injection.

    Main entry point. Calls each sub-function if enabled, assembles into
    a formatted string with header/footer markers.

    When *component_id* and *component_deps* are provided, the dependency
    graph section is filtered to show only edges relevant to this component
    and its direct dependencies.

    Applies max_context_tokens cap by estimating total chars and truncating
    sections in order of priority (conventions first to drop, module map last).
    """
    if config is None:
        config = FeedforwardConfig()

    if not config.enabled:
        return ""

    # Build sections in priority order (highest priority first - last to be dropped)
    # Priority: module_map > dependency_graph > public_interfaces > conventions
    sections: list[tuple[str, str]] = []

    if config.module_map:
        try:
            content = build_module_map(worktree_path)
            if content:
                sections.append(("Module map", content))
        except Exception:
            pass

    if config.dependency_graph:
        try:
            content = build_dependency_graph(worktree_path)
            # Filter to relevant edges when component context is available
            if content and component_deps:
                relevant = set(component_deps)
                if component_id:
                    relevant.add(component_id)
                filtered_lines = []
                for line in content.splitlines():
                    # Keep lines that mention any relevant component
                    if any(dep in line for dep in relevant):
                        filtered_lines.append(line)
                if filtered_lines:
                    content = "\n".join(filtered_lines)
            if content:
                sections.append(("Dependency graph", content))
        except Exception:
            pass

    if config.public_interfaces:
        try:
            content = extract_public_interfaces(worktree_path)
            if content:
                sections.append(("Public interfaces", content))
        except Exception:
            pass

    if config.conventions:
        try:
            content = extract_conventions(worktree_path)
            if content:
                sections.append(("Conventions", content))
        except Exception:
            pass

    if not sections:
        return ""

    # Apply token cap by dropping lowest-priority sections first.
    # Priority order in 'sections' is highest first, so we drop from the end.
    max_chars = config.max_context_tokens * 4
    sections = _truncate_to_budget(sections, max_chars)

    if not sections:
        return ""

    # Assemble final output
    parts: list[str] = ["=== CODEBASE CONTEXT (auto-generated) ===", ""]

    for heading, content in sections:
        parts.append(f"## {heading}")
        parts.append(content)
        parts.append("")

    parts.append("=== END CODEBASE CONTEXT ===")

    return "\n".join(parts)


def _truncate_to_budget(
    sections: list[tuple[str, str]],
    max_chars: int,
) -> list[tuple[str, str]]:
    """Truncate sections to fit within a character budget.

    Drops lowest-priority sections first (last in list).
    If still over budget after dropping all but one section,
    truncates the remaining section content.
    """
    # Calculate overhead per section (heading + blank lines)
    header_footer_overhead = len("=== CODEBASE CONTEXT (auto-generated) ===\n\n") + len(
        "\n=== END CODEBASE CONTEXT ==="
    )

    def _total_chars(secs: list[tuple[str, str]]) -> int:
        total = header_footer_overhead
        for heading, content in secs:
            total += len(f"## {heading}\n") + len(content) + len("\n\n")
        return total

    # Drop lowest-priority sections (end of list) until under budget
    while sections and _total_chars(sections) > max_chars:
        if len(sections) == 1:
            # Last section - truncate content instead of dropping it
            heading, content = sections[0]
            available = max_chars - header_footer_overhead - len(f"## {heading}\n") - len("\n\n")
            if available > 100:
                truncated = content[:available - 20] + "\n... (truncated)"
                sections[0] = (heading, truncated)
            else:
                sections = []
            break
        sections.pop()

    return sections
