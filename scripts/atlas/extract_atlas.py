"""Extract the derived tier of the system atlas from the working tree.

Everything here is a fact read out of the code: module graph, public surface,
owned types, refusals, module-level constants, and the tests that guard each
component. No prose is invented; what the system is MEANT to do lives in
logical_model.py, and the page styles the two differently on purpose.

Ported from the deckgen repository's atlas tooling.

Usage:  uv run python scripts/atlas/extract_atlas.py [--out docs/atlas/atlas.json]
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", "build", "dist"}
SPEC_PATH = Path("docs/control-loop-design.md")
TESTS_DIR = Path("tests")
TESTS_KEPT = 25


def source_roots(repo: Path) -> list[tuple[Path, str]]:
    """kstrl is one package at the repo root."""
    pkg = repo / "kstrl"
    if pkg.is_dir() and (pkg / "__init__.py").exists():
        return [(pkg, "kstrl")]
    return []


def module_of(path: Path, pkg_dir: Path, pkg_name: str) -> str:
    rel = path.relative_to(pkg_dir).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join([pkg_name, *parts]) if parts else pkg_name


def plane_of(module: str) -> str:
    """The architectural plane a module belongs to, from its dotted path."""
    parts = module.split(".")
    if len(parts) > 2 and parts[1] in ("agents", "tui", "ui"):
        return parts[1]
    return "core"


def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except (AttributeError, ValueError):
        args = "..."
    ret = ""
    if node.returns is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except (AttributeError, ValueError):
            ret = ""
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({args}){ret}"


def opening(text: str | None) -> str:
    """The first paragraph of a docstring, capped.

    The atlas is read for orientation, not as a mirror of the source: the page
    shows a module's opening line, and the file itself is one click away. Storing
    every docstring in full doubled atlas.json for text nobody rendered.
    """
    if not text:
        return ""
    para = text.strip().split("\n\n")[0]
    para = " ".join(para.split())
    return para if len(para) <= 320 else para[:319] + "\u2026"


def first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def sentence(test_name: str) -> str:
    body = test_name[5:] if test_name.startswith("test_") else test_name
    return body.replace("_", " ").strip() or test_name


def parse_surface(raw: str) -> dict[str, Any] | None:
    """The public surface of one module's source, or None if it cannot parse.

    This is the ONE definition of "public surface" in the atlas: the extractor
    stores it and the change detector diffs it between two git blobs, so the
    two can never disagree about what a module exports.
    """
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError):
        return None
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    constants: list[dict[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("_"):
                continue
            functions.append(
                {
                    "name": node.name,
                    "signature": signature(node),
                    "doc": first_line(ast.get_docstring(node)),
                }
            )
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            bases = []
            for b in node.bases:
                with contextlib.suppress(AttributeError, ValueError):
                    bases.append(ast.unparse(b))
            is_error = node.name.endswith(("Error", "Exception")) or any(
                "Error" in b or "Exception" in b for b in bases
            )
            record = {
                "name": node.name,
                "doc": first_line(ast.get_docstring(node)),
                # Full signatures, not names: a class's surface includes what
                # its methods accept and return, so a parameter change is a
                # change to the type.
                "methods": [
                    signature(n)
                    for n in node.body
                    if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                    and not n.name.startswith("_")
                ],
            }
            (errors if is_error else classes).append(record)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            # This repo declares measured caps as `NAME: Final = ...`, which is
            # an AnnAssign; handling only Assign missed every tuning module.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", target.id):
                    continue
                if node.value is None:
                    continue
                try:
                    value = ast.unparse(node.value)
                except (AttributeError, ValueError):
                    value = "?"
                constants.append({"name": target.id, "value": value[:80]})
    return {
        "docstring": opening(ast.get_docstring(tree)),
        "functions": functions,
        "classes": classes,
        "errors": errors,
        "constants": constants,
    }


def collect_module(path: Path, repo: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    surface = parse_surface(raw)
    if surface is None:
        return None
    return {
        "file": str(path.relative_to(repo)),
        "loc": len(raw.splitlines()),
        # A change detector for the atlas, never a security claim: usedforsecurity=False
        # states that and keeps the digest byte-identical, so committed atlas.json files
        # stay comparable across the flag.
        "sha": hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12],
        **surface,
        "constants": surface["constants"][:14],
    }


# In a `uses` mapping, this marks "the whole module": `import m` gives access
# to every name in m, so no list of names would be honest.
WHOLE_MODULE = "*"


def internal_uses(
    raw: str,
    prefixes: set[str],
    known: set[str],
    module: str = "",
    is_package: bool = False,
) -> dict[str, set[str]]:
    """target module -> the names this source takes from it.

    A value containing WHOLE_MODULE means the source imported the module
    itself, so any name in it may be used. Relative imports resolve against
    `module` (the importer's own dotted name); without it they are skipped.
    """
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError):
        return {}

    def resolve(name: str) -> str | None:
        if name in known:
            return name
        parts = name.split(".")
        while len(parts) > 1:
            parts.pop()
            candidate = ".".join(parts)
            if candidate in known:
                return candidate
        return None

    uses: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in prefixes:
                    target = resolve(alias.name)
                    if target:
                        uses[target].add(WHOLE_MODULE)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if not module:
                    continue
                # The anchor package: a module's own package, or the package
                # itself when the source is an __init__. Each further level
                # climbs one package up.
                anchor = module.split(".")
                if not is_package:
                    anchor = anchor[:-1]
                if node.level > 1:
                    anchor = anchor[: len(anchor) - (node.level - 1)]
                if not anchor:
                    continue
                parts = [*anchor, *node.module.split(".")] if node.module else anchor
                src = ".".join(parts)
            else:
                if not node.module:
                    continue
                src = node.module
            if src.split(".")[0] not in prefixes:
                continue
            base = resolve(src)
            for alias in node.names:
                if alias.name == "*":
                    if base:
                        uses[base].add(WHOLE_MODULE)
                    continue
                # Exact membership only: resolve() walks prefixes upward, so it
                # would resolve a class name to its module and misread every
                # from-import as a whole-module import.
                if f"{src}.{alias.name}" in known:
                    uses[f"{src}.{alias.name}"].add(WHOLE_MODULE)
                elif base:
                    uses[base].add(alias.name)
    return dict(uses)


def internal_imports(path: Path, prefixes: set[str], known: set[str]) -> set[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(internal_uses(raw, prefixes, known))


def test_names(raw: str) -> list[str]:
    """Test functions in one test file's source, at any nesting depth."""
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError):
        return []
    return [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
    ]


def collect_tests(
    repo: Path, prefixes: set[str], known: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """module -> the behaviours asserted by tests that import it."""
    guards: dict[str, list[dict[str, Any]]] = defaultdict(list)
    test_dir = repo / TESTS_DIR
    if not test_dir.is_dir():
        return guards
    for path in sorted(test_dir.rglob("test_*.py")):
        if any(p in SKIP_PARTS for p in path.parts):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        targets = set(internal_uses(raw, prefixes, known))
        if not targets:
            continue
        names = test_names(raw)
        stem = path.stem[5:] if path.stem.startswith("test_") else path.stem
        for target in targets:
            # A test file named after the module is its primary guard; a file
            # that merely imports it exercises it. Both are true, and the
            # distinction is what stops a shared helper from claiming every test.
            primary = stem == target.split(".")[-1]
            for name in names:
                guards[target].append({"name": name, "primary": primary})
    return guards


def spec_sections(repo: Path) -> list[dict[str, str]]:
    path = repo / SPEC_PATH
    if not path.is_file():
        return []
    out: list[dict[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for match in re.finditer(r"^(#{2,4})\s+(.+)$", text, re.M):
        out.append({"level": str(len(match.group(1))), "title": match.group(2).strip()})
    return out


def build(repo: Path) -> dict[str, Any]:
    roots = source_roots(repo)
    prefixes = {name for _, name in roots}
    paths: dict[str, Path] = {}
    for pkg_dir, pkg_name in roots:
        for path in pkg_dir.rglob("*.py"):
            if any(p in SKIP_PARTS for p in path.parts):
                continue
            paths[module_of(path, pkg_dir, pkg_name)] = path
    known = set(paths)

    components: dict[str, Any] = {}
    imports: dict[str, set[str]] = {}
    for module, path in sorted(paths.items()):
        record = collect_module(path, repo)
        if record is None:
            continue
        record["id"] = module
        record["package"] = module.split(".")[0]
        record["plane"] = plane_of(module)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        uses = internal_uses(
            raw, prefixes, known, module=module, is_package=path.name == "__init__.py"
        )
        uses.pop(module, None)
        record["uses"] = {
            target: [WHOLE_MODULE] if WHOLE_MODULE in names else sorted(names)
            for target, names in sorted(uses.items())
        }
        imports[module] = set(uses)
        components[module] = record

    importers: dict[str, set[str]] = defaultdict(set)
    for module, deps in imports.items():
        for dep in deps:
            importers[dep].add(module)
    for module, record in components.items():
        record["imports"] = sorted(imports.get(module, set()))
        record["imported_by"] = sorted(importers.get(module, set()))

    guards = collect_tests(repo, prefixes, known)
    for module, record in components.items():
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in guards.get(module, []):
            if item["name"] in seen:
                continue
            seen.add(item["name"])
            unique.append(item)
        unique.sort(key=lambda t: (not t["primary"], t["name"]))
        record["tests_total"] = len(unique)
        record["tests_primary"] = sum(1 for t in unique if t["primary"])
        # The full list is recoverable from the tree; the atlas keeps a sample so
        # a committed file that changes on every merge stays diffable by eye.
        # Names only: the sentence is derived where it is displayed.
        record["tests"] = [t["name"] for t in unique[:TESTS_KEPT]]

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    return {
        "version": 1,
        "built_at_commit": head.stdout.strip() if head.returncode == 0 else "",
        "packages": sorted(prefixes),
        "spec_sections": spec_sections(repo),
        "components": components,
    }


def drift(fresh: dict[str, Any], stored: dict[str, Any]) -> list[str]:
    """Ways the stored atlas no longer describes the tree. Empty means current."""
    out: list[str] = []
    new_c, old_c = fresh["components"], (stored or {}).get("components", {})
    added = sorted(set(new_c) - set(old_c))
    gone = sorted(set(old_c) - set(new_c))
    moved = sorted(m for m in set(new_c) & set(old_c) if new_c[m]["sha"] != old_c[m]["sha"])
    # A source module's hash says nothing about the TESTS that guard it. A new
    # test file changes what the system guarantees without touching a single
    # module, and comparing only shas would let that pass as current. `fresh`
    # is a full rebuild, so the attribution is authoritative here.
    guards_changed = sorted(
        m
        for m in set(new_c) & set(old_c)
        if new_c[m].get("tests_total") != old_c[m].get("tests_total")
    )
    for m in added:
        out.append(f"missing from the atlas: {m}")
    for m in gone:
        out.append(f"in the atlas but gone from the tree: {m}")
    for m in moved:
        out.append(f"code changed since the atlas was built: {m}")
    for m in guards_changed:
        was = (old_c[m] or {}).get("tests_total", 0)
        now = new_c[m].get("tests_total", 0)
        out.append(f"tests guarding it changed ({was} -> {now}): {m}")
    return out


def mapping_drift(fresh: dict[str, Any]) -> list[str]:
    """Modules the logical model claims but the tree does not have.

    The component-to-module mapping is the one hand-authored input the atlas
    has. Left unchecked, a rename would silently downgrade a built component to
    "not built" instead of failing loudly. A component that carries a
    `tracker` is a roadmap item whose module is allowed to be missing; the
    layout itself is checked too, since a card drawn outside its band is the
    same kind of quiet lie.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from logical_model import COMPONENTS, layout_problems
    except ImportError:
        return []
    known = set(fresh["components"])
    out: list[str] = []
    for c in COMPONENTS:
        if c.get("tracker"):
            continue
        for m in c.get("implemented_by", []):
            if m.startswith("kstrl") and m not in known:
                out.append(f"{c['id']} claims {m}, which does not exist")
    out.extend(f"layout: {p}" for p in layout_problems())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/atlas/atlas.json")
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the stored atlas no longer matches the tree",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    out_path = repo / args.out
    existing: dict[str, Any] = {}
    if out_path.is_file():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}

    fresh = build(repo)

    if args.check:
        problems = drift(fresh, existing) + mapping_drift(fresh)
        if not existing:
            problems.insert(0, "no atlas has been built yet")
        if problems:
            noun = "problem" if len(problems) == 1 else "problems"
            print(f"atlas is out of date ({len(problems)} {noun}):")
            for line in problems[:25]:
                print(f"  {line}")
            if len(problems) > 25:
                print(f"  ... and {len(problems) - 25} more")
            print("run: uv run python scripts/atlas/extract_atlas.py")
            return 1
        print(f"atlas is current: {len(fresh['components'])} components match the tree")
        return 0

    atlas = fresh
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(atlas, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    comps = atlas["components"]
    print(f"modules: {len(comps)}")
    print(f"  public functions: {sum(len(c['functions']) for c in comps.values())}")
    print(f"  types:            {sum(len(c['classes']) for c in comps.values())}")
    print(f"  refusals:         {sum(len(c['errors']) for c in comps.values())}")
    guarded = sum(c["tests_total"] for c in comps.values())
    primary = sum(c["tests_primary"] for c in comps.values())
    print(f"  guarded by:       {guarded} tests ({primary} primary)")
    mapping = mapping_drift(atlas)
    for line in mapping:
        print(f"  warning: {line}")
    print(f"written to {out_path.relative_to(repo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
