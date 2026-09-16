"""The engineer is shown the repo's own source, in an order the repo
decides, and is told when it is shown nothing (#378).

These drive `build_feedforward_context`, which is the function
`factory._run_component` calls to build the block the engineer
receives, rather than `extract_public_interfaces` alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.feedforward import FeedforwardConfig, build_feedforward_context


def _section(context: str, heading: str) -> str:
    """The body of one `## heading` section of a context block."""
    lines = context.splitlines()
    try:
        start = lines.index(f"## {heading}")
    except ValueError:
        return ""
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## ") or line.startswith("=== END"):
            break
        body.append(line)
    return "\n".join(body).strip()


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

    section = _section(
        build_feedforward_context(tmp_path, FeedforwardConfig()), "Public interfaces"
    )

    assert "packages/demo/src/demo_pkg/models.py" in section
    assert "class Deck" in section
    assert "def build_deck(name: str) -> Deck" in section
    assert "class Renderer" in section


def test_the_file_budget_is_spent_on_source_and_not_on_directory_order(
    tmp_path: Path,
) -> None:
    """Two plants in one fixture. `tests/` holds more modules than the
    budget, so a walk that reaches it first shows the engineer none of
    the source; `aaa_extras` sorts before `myproj` and is smaller, so a
    walk that spends the budget in discovery order shows part of it.
    The reversed-`iterdir` half is what makes the second one fail
    whichever way the filesystem happens to enumerate."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "__init__.py").write_text("", encoding="utf-8")
    for i in range(35):
        (proj / f"mod{i:02d}.py").write_text(f"class Core{i:02d}:\n    pass\n", encoding="utf-8")

    extras = tmp_path / "aaa_extras"
    extras.mkdir()
    (extras / "__init__.py").write_text("", encoding="utf-8")
    for name in ("alpha", "beta"):
        (extras / f"{name}.py").write_text(
            f"class Extra{name.title()}:\n    pass\n", encoding="utf-8"
        )

    tests_pkg = tmp_path / "tests"
    tests_pkg.mkdir()
    (tests_pkg / "__init__.py").write_text("", encoding="utf-8")
    for i in range(40):
        (tests_pkg / f"helper{i:02d}.py").write_text(
            f"class Helper{i:02d}:\n    pass\n", encoding="utf-8"
        )

    forward = build_feedforward_context(tmp_path, FeedforwardConfig())
    section = _section(forward, "Public interfaces")
    listed = [line.split(":", 1)[0] for line in section.splitlines() if line.strip()]

    assert listed, section
    assert all(name.startswith("myproj/") for name in listed), listed

    original = Path.iterdir

    def reversed_iterdir(self: Path):  # type: ignore[no-untyped-def]
        return reversed(list(original(self)))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "iterdir", reversed_iterdir)
        reverse = build_feedforward_context(tmp_path, FeedforwardConfig())

    assert reverse == forward


def test_an_empty_section_records_why_it_is_empty(tmp_path: Path) -> None:
    """A repo with no Python in it. Before #378 the section was absent
    and nothing recorded that the stage had tried."""
    (tmp_path / "README.md").write_text("no python here\n", encoding="utf-8")

    context = build_feedforward_context(tmp_path, FeedforwardConfig())
    section = _section(context, "Public interfaces")

    assert "## Public interfaces" in context
    assert "no Python source root found" in section
    assert str(tmp_path) in section


def test_two_roots_of_the_same_size_are_ordered_by_path(tmp_path: Path) -> None:
    """Equal file counts leave the size key indifferent, so the tie-break on
    the path is the only thing between the engineer and `iterdir` order.
    Twenty modules each and a budget of thirty, so which root is read first
    decides ten of the thirty lines."""
    for name in ("alpha_pkg", "beta_pkg"):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        stem = name.split("_")[0].title()
        for i in range(20):
            (pkg / f"mod{i:02d}.py").write_text(
                f"class {stem}{i:02d}:\n    pass\n", encoding="utf-8"
            )

    forward = build_feedforward_context(tmp_path, FeedforwardConfig())
    listed = [
        line.split(":", 1)[0]
        for line in _section(forward, "Public interfaces").splitlines()
        if line.strip()
    ]

    assert len(listed) == 30, listed
    assert listed[0].startswith("alpha_pkg/"), listed[0]
    assert sum(name.startswith("alpha_pkg/") for name in listed) == 20, listed

    original = Path.iterdir

    def reversed_iterdir(self: Path):  # type: ignore[no-untyped-def]
        return reversed(list(original(self)))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "iterdir", reversed_iterdir)
        reverse = build_feedforward_context(tmp_path, FeedforwardConfig())

    assert reverse == forward


def test_a_package_whose_name_starts_with_test_is_still_source(tmp_path: Path) -> None:
    """`testpkg` is not a test directory. The exclusion keys on the exact
    names `test` and `tests`; a prefix match would delete a package called
    `testkit`, `testing` or `testpkg` from the engineer's view, and nothing
    else in this file would notice."""
    pkg = tmp_path / "testpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("class Widget:\n    pass\n", encoding="utf-8")

    section = _section(
        build_feedforward_context(tmp_path, FeedforwardConfig()), "Public interfaces"
    )

    assert "testpkg/core.py" in section
    assert "class Widget" in section
