"""The engineer is shown the repo's own source, in an order the repo
decides, and is told when it is shown nothing (#378).

These drive `build_feedforward_context`, which is the function
`factory._run_component` calls to build the block the engineer
receives, rather than `extract_public_interfaces` alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.feedforward import _MAX_SOURCE_ROOT_DEPTH, FeedforwardConfig, build_feedforward_context
from tests.test_context import section


def _package(root: Path, name: str, count: int) -> Path:
    """A package *name* under *root* holding *count* modules, ``mod00.py``
    and up, each with one public class so it counts against the file
    budget."""
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for i in range(count):
        (pkg / f"mod{i:02d}.py").write_text(f"class C{i:02d}:\n    pass\n", encoding="utf-8")
    return pkg


def _listed(context: str) -> list[str]:
    """The file names named in the "Public interfaces" section body."""
    body = section(context, "## Public interfaces")
    return [line.split(":", 1)[0] for line in body.splitlines() if line.strip()]


def _reversed_iterdir_context(tmp_path: Path) -> str:
    """`build_feedforward_context(tmp_path)` with `Path.iterdir` reversed,
    the patch undone before this returns. `Path.rglob` does not go
    through `Path.iterdir`, so the only thing perturbed is which
    candidate root the walk visits first."""
    original = Path.iterdir

    def reversed_iterdir(self: Path):  # type: ignore[no-untyped-def]
        return reversed(list(original(self)))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "iterdir", reversed_iterdir)
        return build_feedforward_context(tmp_path, FeedforwardConfig())


def test_a_packages_src_monorepo_reaches_the_engineer(tmp_path: Path) -> None:
    """deckgen's layout: packages/<name>/src/<pkg>. Neither `src/` nor
    `lib/` nor a top-level `__init__.py`, so the pre-#378 discovery
    returned no root and the section never appeared."""
    pkg = tmp_path / "packages" / "demo" / "src" / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(
        "class Deck:\n    pass\n\n\ndef build_deck(name: str) -> Deck:\n    return Deck()\n",
        encoding="utf-8",
    )
    (pkg / "service.py").write_text("class Renderer:\n    pass\n", encoding="utf-8")

    body = section(build_feedforward_context(tmp_path, FeedforwardConfig()), "## Public interfaces")

    assert "packages/demo/src/demo_pkg/models.py" in body
    assert "class Deck" in body
    assert "def build_deck(name: str) -> Deck" in body
    assert "class Renderer" in body


def test_a_packages_src_monorepo_dependency_graph_reaches_the_engineer(tmp_path: Path) -> None:
    """The dependency graph had the same top-level-only blind spot as the
    interface extractor (#378, second instance in the same module): on
    a `packages/<name>/src/<pkg>` layout it found no packages and the
    section was silently absent, the same defect and the same fix."""
    pkg = tmp_path / "packages" / "demo" / "src" / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    (pkg / "api.py").write_text(
        "from demo_pkg.models import Thing\n\n\ndef get(t: Thing) -> Thing:\n    return t\n",
        encoding="utf-8",
    )

    body = section(build_feedforward_context(tmp_path, FeedforwardConfig()), "## Dependency graph")

    assert "api" in body
    assert "models" in body
    assert "Thing" in body
    assert body.splitlines()[0].startswith("api -> models"), body
    assert "packages.demo.src.demo_pkg" not in body, body


def test_the_file_budget_is_spent_on_source_and_not_on_directory_order(
    tmp_path: Path,
) -> None:
    """Two plants in one fixture. `tests/` holds more modules than the
    budget, so a walk that reaches it first shows the engineer none of
    the source; `aaa_extras` sorts before `myproj` and is smaller, so a
    walk that spends the budget in discovery order shows part of it.
    The reversed-`iterdir` half is what makes the second one fail
    whichever way the filesystem happens to enumerate."""
    _package(tmp_path, "myproj", 35)
    _package(tmp_path, "aaa_extras", 2)
    _package(tmp_path, "tests", 40)

    forward = build_feedforward_context(tmp_path, FeedforwardConfig())
    listed = _listed(forward)

    assert listed, forward
    assert all(name.startswith("myproj/") for name in listed), listed
    assert _reversed_iterdir_context(tmp_path) == forward


def test_an_empty_section_records_why_it_is_empty(tmp_path: Path) -> None:
    """A repo with no Python in it. Before #378 the section was absent
    and nothing recorded that the stage had tried."""
    (tmp_path / "README.md").write_text("no python here\n", encoding="utf-8")

    context = build_feedforward_context(tmp_path, FeedforwardConfig())
    body = section(context, "## Public interfaces")

    assert "## Public interfaces" in context
    assert "no Python source root found" in body
    assert str(tmp_path) in body


def test_two_roots_of_the_same_size_are_ordered_by_path(tmp_path: Path) -> None:
    """Equal file counts leave the size key indifferent, so the tie-break on
    the path is the only thing between the engineer and `iterdir` order.
    Twenty modules each and a budget of thirty, so which root is read first
    decides ten of the thirty lines."""
    _package(tmp_path, "alpha_pkg", 20)
    _package(tmp_path, "beta_pkg", 20)

    forward = build_feedforward_context(tmp_path, FeedforwardConfig())
    listed = _listed(forward)

    assert len(listed) == 30, listed
    assert listed[0].startswith("alpha_pkg/"), listed[0]
    assert sum(name.startswith("alpha_pkg/") for name in listed) == 20, listed
    assert _reversed_iterdir_context(tmp_path) == forward


def test_a_package_whose_name_starts_with_test_is_still_source(tmp_path: Path) -> None:
    """`testpkg` is not a test directory. The exclusion keys on the exact
    names `test` and `tests`; a prefix match would delete a package called
    `testkit`, `testing` or `testpkg` from the engineer's view, and nothing
    else in this file would notice."""
    pkg = tmp_path / "testpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("class Widget:\n    pass\n", encoding="utf-8")

    body = section(build_feedforward_context(tmp_path, FeedforwardConfig()), "## Public interfaces")

    assert "testpkg/core.py" in body
    assert "class Widget" in body


def test_a_directory_of_py_files_with_no_init_is_the_loose_tier(tmp_path: Path) -> None:
    """No package anywhere in the tree: the loose tier is what the walk
    falls back to. A project that is "just scripts", with no
    `__init__.py` anywhere, is not a repo with no Python in it."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "build.py").write_text("def release() -> None:\n    pass\n", encoding="utf-8")

    body = section(build_feedforward_context(tmp_path, FeedforwardConfig()), "## Public interfaces")

    assert "scripts/build.py" in body
    assert "def release()" in body


def test_a_package_past_the_depth_bound_is_invisible(tmp_path: Path) -> None:
    """`_MAX_SOURCE_ROOT_DEPTH` is a wall, not a suggestion: a package one
    level past it is never reached, the same as no Python at all. The
    mechanism is pinned, not the number: this builds exactly one level
    deeper than the constant, whatever it is set to."""
    parts = [f"level{i}" for i in range(_MAX_SOURCE_ROOT_DEPTH)]
    pkg = tmp_path.joinpath(*parts, "toodeep")
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("class Widget:\n    pass\n", encoding="utf-8")

    body = section(build_feedforward_context(tmp_path, FeedforwardConfig()), "## Public interfaces")

    assert "no Python source root found" in body


def test_a_crash_in_extraction_is_recorded_and_does_not_take_the_context_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in the walk must not take the whole context down, and it must
    not go silent either: the assembly block records what it swallowed
    instead of dropping the section (#378)."""

    def boom(root: Path) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("kstrl.feedforward.extract_public_interfaces", boom)

    context = build_feedforward_context(tmp_path, FeedforwardConfig())
    body = section(context, "## Public interfaces")

    assert "## Public interfaces" in context
    assert "public interfaces failed: RuntimeError: boom" in body
