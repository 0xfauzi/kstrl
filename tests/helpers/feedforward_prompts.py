"""Tables for the six #428 engineer-facing notices in ``kstrl/feedforward.py``.

Everything ``build_codebase_scan_context`` returns is pasted into the engineer
prompt, so each of these sentences is read by a model as part of its
instructions. PR #417 removed "Raise codebase_scan.max_context_tokens to see
it." from the dependency graph's notice by hand and left no guard behind;
measured on 6a354cc, putting a short imperative back into the #420 notice
leaves the whole suite green (7203 passed, 0 failed). Enrolling the bodies is
what makes a reword move a hash and a version with it.

They live here rather than in ``tests/test_prompt_versions.py`` for the reason
``tests/helpers/builder_prompts.py`` gives: that file is close to the repo's
800-line ratchet and these rows do not fit inside the remaining headroom.

ONE VERSION FOR THE SIX, as the #303 builder fragments do. The unit is the
notice vocabulary one module delivers to one role, so a reword of any of them
bumps ``CODEBASE_SCAN_NOTICE_PROMPT_VERSION``.

H2/H3 SCOPE. These are engineer-facing CONTEXT, like ``DECISIONS_CONTEXT_PROMPT``
and the 53 #303 builder fragments: the calibration suite scores the architect,
reviewer, security and distiller roles against planted-bug fixtures and has no
fixture that scores a codebase scan notice. They carry the H3 obligation (body,
version and snapshot move together) and no H2 obligation the suite can
discharge.

NOT RENDER-EXEMPT. Unlike the #303 fragments, each of these six IS returned
verbatim by exactly one production path, so each has a real entry in
``_RENDERERS`` and gets the exact-equality orphan guard rather than an
exemption with a reason that would not be true.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from kstrl import feedforward

#: One row, read the way ``builder_prompts._BUILDERS`` is read: the module,
#: the name of its shared version constant, and the names it covers.
_NOTICES: tuple[tuple[ModuleType, str, tuple[str, ...]], ...] = (
    (
        feedforward,
        "CODEBASE_SCAN_NOTICE_PROMPT_VERSION",
        (
            "NO_SOURCE_ROOT_PROMPT",
            "NO_PUBLIC_SYMBOLS_PROMPT",
            "INTERFACES_DID_NOT_FIT_PROMPT",
            "GRAPH_DID_NOT_FIT_PROMPT",
            "SECTION_FAILED_PROMPT",
            "SECTION_DID_NOT_FIT_PROMPT",
        ),
    ),
)

NOTICE_PROMPTS: dict[str, str] = {
    name: getattr(module, name) for module, _version, names in _NOTICES for name in names
}

NOTICE_VERSIONS: dict[str, str] = {
    name: getattr(module, version) for module, version, names in _NOTICES for name in names
}


# --- production renderers --------------------------------------------------
#
# Each returns the notice through the real function that emits it, so
# ``test_renderer_renders_the_enrolled_body`` can patch the constant to a
# fieldless marker and demand the production path return THAT and nothing
# else. ``str.format`` ignores keyword arguments a template does not use, so
# one fieldless marker works for all six.


def _package(root: Path, files: dict[str, str]) -> Path:
    """A one-package tree under *root*, and the root itself."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    return root


def _no_source_root(tmp_path: Path) -> str:
    """No package and no loose .py anywhere: the extractor says so."""
    empty = tmp_path / "empty"
    empty.mkdir()
    return feedforward.extract_public_interfaces(empty)


def _no_public_symbols(tmp_path: Path) -> str:
    """A source root that exists and holds nothing public."""
    root = _package(tmp_path / "nosym", {"data.py": "X = 1\n"})
    return feedforward.extract_public_interfaces(root)


def _interfaces_did_not_fit(tmp_path: Path) -> str:
    """Two files with symbols and no room: the first is charged, the
    second finds the budget already spent."""
    root = _package(
        tmp_path / "iface",
        {"a.py": "class A:\n    pass\n", "b.py": "class B:\n    pass\n"},
    )
    return feedforward.extract_public_interfaces(root, 0)


def _graph_did_not_fit(tmp_path: Path) -> str:
    """Files are parsed in sorted order, so b.py records the edge that
    spends the budget and c.py is the iteration that reports it."""
    root = _package(
        tmp_path / "graph",
        {
            "a.py": "class A:\n    pass\n",
            "b.py": "from pkg.a import A\n",
            "c.py": "from pkg.a import A\n",
        },
    )
    return feedforward.build_dependency_graph(root, 0)


def _section_failed(_tmp_path: Path) -> str:
    sections: list[tuple[str, str]] = []

    def boom(_left: int) -> str:
        raise RuntimeError("boom")

    feedforward._append_section(sections, "Public interfaces", boom, 10_000)
    return sections[-1][1]


def _section_did_not_fit(_tmp_path: Path) -> str:
    sections: list[tuple[str, str]] = [("Module map", "x")]
    feedforward._append_section(sections, "Public interfaces", lambda _left: "z" * 50, 10)
    return sections[-1][1]


NOTICE_RENDERERS: dict[str, tuple[ModuleType, Callable[[Path], str]]] = {
    "NO_SOURCE_ROOT_PROMPT": (feedforward, _no_source_root),
    "NO_PUBLIC_SYMBOLS_PROMPT": (feedforward, _no_public_symbols),
    "INTERFACES_DID_NOT_FIT_PROMPT": (feedforward, _interfaces_did_not_fit),
    "GRAPH_DID_NOT_FIT_PROMPT": (feedforward, _graph_did_not_fit),
    "SECTION_FAILED_PROMPT": (feedforward, _section_failed),
    "SECTION_DID_NOT_FIT_PROMPT": (feedforward, _section_did_not_fit),
}

#: The pin. Not derived from ``_NOTICES``, and nothing here computes a hash.
NOTICE_SNAPSHOTS: dict[str, tuple[str, str]] = {
    "GRAPH_DID_NOT_FIT_PROMPT": (
        "f31be0604c56acc2338ee9603645c876ed3c1df396108022f0d4332366ed757b",
        "1.0.0",
    ),
    "INTERFACES_DID_NOT_FIT_PROMPT": (
        "76ab4b18c8a64f2a93075c64ce2d12397109b4edae26fb7945f45804914eb83e",
        "1.0.0",
    ),
    "NO_PUBLIC_SYMBOLS_PROMPT": (
        "85750feb7dea708a905a38041de3d4d2799ac8aa226f029aff22658fe9781d83",
        "1.0.0",
    ),
    "NO_SOURCE_ROOT_PROMPT": (
        "c1dbd8724ce354346d4f75dbc8e1820f4b2420889001409282916b62cfa0504f",
        "1.0.0",
    ),
    "SECTION_DID_NOT_FIT_PROMPT": (
        "c5fbfcd0d5bbb09d7f7b870252de50ea36ac7b2835ab72af0951a5d95f8a4bcb",
        "1.0.0",
    ),
    "SECTION_FAILED_PROMPT": (
        "dcfe42ac8897607f28e9296af3f08b55953f35357c168ad11b7987d7ec4ea2ee",
        "1.0.0",
    ),
}

assert set(NOTICE_PROMPTS) == set(NOTICE_SNAPSHOTS) == set(NOTICE_RENDERERS), (
    "the three tables name different notices: "
    f"in _NOTICES only: {sorted(set(NOTICE_PROMPTS) - set(NOTICE_SNAPSHOTS))}; "
    f"in NOTICE_SNAPSHOTS only: {sorted(set(NOTICE_SNAPSHOTS) - set(NOTICE_PROMPTS))}; "
    f"missing a renderer: {sorted(set(NOTICE_PROMPTS) - set(NOTICE_RENDERERS))}. "
    "A new notice needs a _NOTICES entry, a snapshot row and a renderer."
)
