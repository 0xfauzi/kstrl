"""Phase 0: Feedforward controls - structural analysis and convention extraction.

All analysis is computational (no LLM calls). Builds a context string
to prepend to the agent prompt before each component runs.
"""

from __future__ import annotations

import ast
import json
import os
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

# Directories to always skip during tree walks
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        "venv",
        ".venv",
        ".kstrl",
    }
)

# Source file extensions we care about
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
    }
)

# Max directories in module map to avoid bloat
_MAX_MODULE_MAP_DIRS = 50

# Max files to scan for public interfaces
_MAX_PUBLIC_INTERFACE_FILES = 30

# How far below the repo root a source root may sit. Measured on the
# deckgen monorepo (#378): `packages/<name>/src/<pkg>` sits four levels
# down and is invisible at three, while five and six find nothing more
# on either deckgen or kstrl and cost 15.6 ms against 4.7 ms.
_MAX_SOURCE_ROOT_DEPTH = 4

# Directory names whose subtree is never the source under change. Exact
# names, never a prefix: `testpkg` and `testing` are ordinary packages,
# and a prefix match deletes them from the engineer's view silently.
# The extractor already skips FILES named `test*`; this is what keeps a
# test PACKAGE from spending the whole file budget before any source is
# read.
_TEST_DIR_NAMES = frozenset({"test", "tests"})

# The block's own header and footer, charged once per assembled block.
_HEADER_FOOTER_OVERHEAD = len("=== CODEBASE CONTEXT (auto-generated) ===\n\n") + len(
    "\n=== END CODEBASE CONTEXT ==="
)


@dataclass
class FeedforwardConfig:
    """Configuration for feedforward context generation."""

    enabled: bool = True
    module_map: bool = True  # directory tree with LOC counts
    public_interfaces: bool = True  # extract public symbols
    dependency_graph: bool = True  # import-based dependency analysis
    conventions: bool = True  # extract from config files
    max_context_tokens: int = 4000  # rough cap (estimate 4 chars per token)

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
            "enabled",
            "module_map",
            "public_interfaces",
            "dependency_graph",
            "conventions",
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
            lines.append(f"{indent}{dir_name:<20s} # {file_count} files, {line_count} lines")
        else:
            lines.append(f"{dir_name:<22s} # {file_count} files, {line_count} lines")

    return "\n".join(lines)


def _classify_dir(directory: Path) -> tuple[bool, list[Path], list[Path]]:
    """One directory, read once: (holds .py files, child packages, other children).

    An unreadable directory reads as empty, which is what the caller did
    with a `PermissionError` before #378. A `test`/`tests` child is
    skipped here, not filtered afterward: the walk never descends into
    one, so a test package can never spend the file budget the caller
    reads with, and a package below it is never reached either.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return False, [], []
    holds_py = any(e.suffix == ".py" and e.is_file() for e in entries)
    child_packages: list[Path] = []
    plain_subdirs: list[Path] = []
    for entry in entries:
        if not entry.is_dir() or _should_skip_dir(entry.name) or entry.name in _TEST_DIR_NAMES:
            continue
        if (entry / "__init__.py").is_file():
            child_packages.append(entry)
        else:
            plain_subdirs.append(entry)
    return holds_py, child_packages, plain_subdirs


def _find_top_source_dirs(root: Path) -> list[Path]:
    """Candidate source roots under *root*, in NO meaningful order.

    Every directory holding ``__init__.py`` within
    ``_MAX_SOURCE_ROOT_DEPTH`` levels, or, when the tree has no packages
    at all, every directory below *root* that holds ``.py`` files
    directly. The walk stops at a package rather than descending into
    it, so a subpackage is never a candidate of its own.

    *root* itself is never a loose candidate: the caller rglobs what it
    is given, and rglobbing the repo root would descend into exactly the
    directories ``_should_skip_dir`` exists to keep this walk out of.

    The order is deliberately not meaningful. ``_ordered_source_roots``
    decides the order the budget is spent in, so that it is a property
    of the repo rather than of ``iterdir``.
    """
    packages: list[Path] = []
    loose: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        holds_py, child_packages, plain_subdirs = _classify_dir(directory)
        if holds_py and directory != root:
            loose.append(directory)
        if depth >= _MAX_SOURCE_ROOT_DEPTH:
            continue
        packages.extend(child_packages)
        stack.extend((child, depth + 1) for child in plain_subdirs)
    return packages or loose


def _ordered_source_roots(root: Path) -> list[tuple[Path, list[Path]]]:
    """Each candidate root with its ``.py`` files, biggest root first.

    Ties break on the path, so the whole order is a total order over
    distinct paths and no part of it comes from the filesystem.
    """
    scanned: list[tuple[Path, list[Path]]] = []
    for src_dir in _find_top_source_dirs(root):
        try:
            py_files = sorted(src_dir.rglob("*.py"))
        except OSError:
            py_files = []
        scanned.append((src_dir, py_files))
    scanned.sort(key=lambda item: (-len(item[1]), item[0].as_posix()))
    return scanned


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
    """Body of the "Public interfaces" section: public classes and functions.

    Skips files starting with '_' or 'test'. Reads at most
    ``_MAX_PUBLIC_INTERFACE_FILES`` files, biggest source root first.

    Never returns "". When nothing was extracted the body is one line
    saying why, because an absent section reads exactly like a repo with
    no public symbols: #378 measured a whole run where the engineer was
    told nothing and no artifact recorded that the stage had tried.
    """
    scanned = _ordered_source_roots(root)
    if not scanned:
        return (
            f"(none: no Python source root found under {root}; searched "
            f"{_MAX_SOURCE_ROOT_DEPTH} levels for a package or a directory "
            f"of .py files, excluding tests)"
        )

    # Private and test files are dropped before the budget is spent on them.
    candidates = [f for _, files in scanned for f in files if not f.name.startswith(("_", "test"))]

    file_symbols: list[tuple[str, list[str]]] = []
    for py_file in candidates:
        if len(file_symbols) >= _MAX_PUBLIC_INTERFACE_FILES:
            break
        symbols = _extract_symbols_from_file(py_file)
        if symbols:
            file_symbols.append((py_file.relative_to(root).as_posix(), symbols))

    if not file_symbols:
        names = ", ".join(sorted(d.relative_to(root).as_posix() for d, _ in scanned)[:5])
        return (
            f"(none: no public classes or functions in the first "
            f"{_MAX_PUBLIC_INTERFACE_FILES} files of {len(scanned)} source "
            f"root(s): {names})"
        )

    return "\n".join(f"{filepath}: {', '.join(symbols)}" for filepath, symbols in file_symbols)


def _render_edge(source_module: str, target_module: str, names: Iterable[str]) -> str:
    """Exactly the line the graph renderer emits for one edge.

    *names* is rendered in the order given; the renderer passes it
    already sorted. This is the one place the line's text is written, so
    the renderer and the size accounting in `_record_edge` cannot drift
    apart the way two independent copies of it did (measured on this
    repository: the old separate estimate was 16.7% low).
    """
    names_list = list(names)
    if names_list:
        return f"{source_module} -> {target_module} (imports: {', '.join(names_list)})"
    return f"{source_module} -> {target_module}"


def _record_edge(
    edges: dict[str, dict[str, set[str]]],
    source_module: str,
    target_module: str,
    names: set[str],
) -> int:
    """Record one edge and return the EXACT growth in rendered characters.

    The count must equal the corresponding growth in
    ``"\\n".join(lines)`` exactly, never more: an over-count would stop
    the caller on a graph that would in fact have fitted, which is the
    one error this function must not make. A new edge charges its own
    rendered line plus the joining newline the final ``"\\n".join`` adds
    for it, except for the very first edge the graph has ever recorded,
    which needs no newline before it. Adding names to an edge that
    already exists charges only the difference between the edge's old
    and new rendered line; `set.update` on an empty set is a no-op, so
    this needs no separate guard for "nothing new was imported".
    """
    targets = edges.setdefault(source_module, {})
    is_new_edge = target_module not in targets
    edges_before = sum(len(t) for t in edges.values())
    bucket = targets.setdefault(target_module, set())
    if is_new_edge:
        bucket.update(names)
        after = _render_edge(source_module, target_module, sorted(bucket))
        return len(after) + (1 if edges_before else 0)
    before = _render_edge(source_module, target_module, sorted(bucket))
    bucket.update(names)
    after = _render_edge(source_module, target_module, sorted(bucket))
    return len(after) - len(before)


def build_dependency_graph(root: Path, max_chars: int | None = None) -> str:
    """Build a module-level dependency graph from Python imports.

    Only tracks internal imports (within the project). Parses all .py
    files under the same source roots ``extract_public_interfaces`` uses
    (#378: the top-level-``__init__.py``-or-``src/<pkg>`` rule this
    replaced is silently empty on a ``packages/<name>/src/<pkg>``
    monorepo, the same defect the interface extractor had, in the same
    module).

    *max_chars* is the room left for this section in the caller's context
    budget. Parsing stops as soon as the graph built so far is already
    bigger than that, and the return value says so instead of being a
    graph nobody will be shown (#403). ``None`` means no budget: parse
    everything.
    """
    roots = _find_top_source_dirs(root)
    if not roots:
        return ""
    packages = {d.name for d in roots}

    all_py_files: list[tuple[Path, Path]] = []
    for src_dir in roots:
        try:
            all_py_files.extend((f, src_dir) for f in sorted(src_dir.rglob("*.py")))
        except OSError:
            pass

    # Parse imports from each file
    # edges: dict of (source_module -> dict of target_module -> set of imported names)
    edges: dict[str, dict[str, set[str]]] = {}
    rendered_chars = 0
    parsed = 0

    for py_file, src_dir in all_py_files:
        if max_chars is not None and rendered_chars > max_chars:
            return (
                f"(did not fit: the dependency graph passed the {max_chars} characters "
                f"left in the context budget after {parsed} of {len(all_py_files)} files, "
                f"so it was not built.)"
            )
        parsed += 1
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, Exception):
            continue

        # Determine the module name for this file, relative to the
        # directory that CONTAINS its source root, so the leading part
        # of the relative path is the package name itself.
        source_module = _path_to_module(py_file, src_dir.parent, packages)
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
                for alias in node.names or []:
                    if alias.name != "*":
                        names.add(alias.name)

                rendered_chars += _record_edge(edges, source_module, target_module, names)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    if top_level not in packages:
                        continue
                    target_module = _simplify_module(alias.name, packages)
                    if target_module == source_module:
                        continue

                    rendered_chars += _record_edge(edges, source_module, target_module, set())

    if not edges:
        return ""

    lines: list[str] = []
    for src_mod in sorted(edges):
        for tgt_mod in sorted(edges[src_mod]):
            lines.append(_render_edge(src_mod, tgt_mod, sorted(edges[src_mod][tgt_mod])))

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


def _append_section(
    sections: list[tuple[str, str]],
    heading: str,
    build: Callable[[int], str],
    remaining: int,
) -> None:
    """Run *build* and append its result under *heading*, unless empty.

    *remaining* is how many characters of the context budget are left for
    this section's body. A builder that cannot be sized without doing the
    work uses it to stop early (#403); the others ignore it.

    A crash inside *build* does not take the whole context down; it is
    RECORDED as the section's content instead of dropped (#378: the
    public-interfaces section used to be the only one of the four that
    said what it swallowed).
    """
    try:
        content = build(remaining)
    except Exception as exc:
        content = f"(none: {heading.lower()} failed: {type(exc).__name__}: {exc})"
    if content:
        sections.append((heading, content))


def _dependency_graph_section(
    root: Path, component_id: str, component_deps: list[str] | None, max_chars: int | None
) -> str:
    """The "Dependency graph" body, filtered to *component_id* and its
    direct dependencies when component context is available.

    *max_chars* bounds the graph build; the caller decides whether that
    is a real limit or ``None``, which builds all of it.
    """
    content = build_dependency_graph(root, max_chars)
    if content and component_deps:
        relevant = set(component_deps)
        if component_id:
            relevant.add(component_id)
        filtered_lines = [
            line for line in content.splitlines() if any(dep in line for dep in relevant)
        ]
        if filtered_lines:
            content = "\n".join(filtered_lines)
    return content


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

    max_chars = config.max_context_tokens * 4

    # Build sections in priority order (highest priority first - last to be dropped)
    # Priority: module_map > dependency_graph > public_interfaces > conventions
    sections: list[tuple[str, str]] = []
    builders: list[tuple[bool, str, Callable[[int], str]]] = [
        (config.module_map, "Module map", lambda _left: build_module_map(worktree_path)),
        (
            config.dependency_graph,
            "Dependency graph",
            lambda left: _dependency_graph_section(
                worktree_path,
                component_id,
                component_deps,
                left if sections and not component_deps else None,
            ),
        ),
        (
            config.public_interfaces,
            "Public interfaces",
            lambda _left: extract_public_interfaces(worktree_path),
        ),
        (config.conventions, "Conventions", lambda _left: extract_conventions(worktree_path)),
    ]

    for enabled, heading, build in builders:
        if not enabled:
            continue
        # The budget is spent: _truncate_to_budget drops from the END of
        # this list, so anything built from here on can only be dropped
        # again.
        if _total_chars(sections) > max_chars:
            break
        _append_section(sections, heading, build, _remaining_chars(sections, heading, max_chars))

    if not sections:
        return ""

    # Apply token cap by dropping lowest-priority sections first.
    # Priority order in 'sections' is highest first, so we drop from the end.
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


def _total_chars(sections: list[tuple[str, str]]) -> int:
    """Characters the assembled context block would occupy."""
    total = _HEADER_FOOTER_OVERHEAD
    for heading, content in sections:
        total += len(f"## {heading}\n") + len(content) + len("\n\n")
    return total


def _remaining_chars(sections: list[tuple[str, str]], heading: str, max_chars: int) -> int:
    """Budget left for the BODY of a section called *heading*, never below 0."""
    return max(0, max_chars - _total_chars(sections) - len(f"## {heading}\n") - len("\n\n"))


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
