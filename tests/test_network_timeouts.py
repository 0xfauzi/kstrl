"""Every ``urlopen`` in kstrl/ carries a real timeout (#155, R8.8 slice 1).

Measured: a ``urlopen`` with no ``timeout=`` was caught by zero of 7289
passing tests on the unguarded tree. This guard FLAGS rather than
clears - over-matching costs a false positive somebody reads; a guard
that clears would delete the mechanism if it over-matched instead.

The ``seen`` tuple below is re-derived by RUNNING the walk, never typed
from a design document: reading a pin is not running a guard.
"""

from __future__ import annotations

import ast

from tests.helpers import astwalk
from tests.helpers.astwalk import calls_to, label, leaf_name, module_name, package_sources, parse

#: Every ``urllib.request.urlopen`` call site in kstrl/, re-derived by
#: running the walk below. The undecided half is the four shared sites
#: every other guard built on ``tests.helpers.astwalk`` already pins.
EXPECTED_SEEN: tuple[str, ...] = (
    "licensing.py:162 urllib.request.urlopen",
    "linear.py:314 urllib.request.urlopen",
    "signals.py:482 urllib.request.urlopen",
)

EXPECTED_UNDECIDED: tuple[str, ...] = (
    "gateparse.py:112 TOOL_PARSERS[chosen]",
    "gateparse.py:114 TOOL_PARSERS[name]",
    "tui/app.py:363 initial_screens_for_kind(kind, observe_only=True)",
    "tui/app.py:435 initial_screens_for_kind(kind, observe_only=False)",
)


def _missing_timeout(source: str) -> list[str]:
    """Every ``urlopen`` call in ``source`` with no real ``timeout=``.

    Flags both an absent ``timeout`` keyword AND an explicit
    ``timeout=None``: the latter is the same unbounded read as the
    former, and a guard that only checks for the keyword's PRESENCE is
    one ``= None`` default away from clearing the defect it exists to
    catch.
    """
    tree = parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if leaf_name(node.func) != "urlopen":
            continue
        timeout_kw = next((kw for kw in node.keywords if kw.arg == "timeout"), None)
        if timeout_kw is None:
            hits.append(f"{node.lineno}")
            continue
        if isinstance(timeout_kw.value, ast.Constant) and timeout_kw.value.value is None:
            hits.append(f"{node.lineno}")
    return hits


class TestEveryUrlopenCarriesATimeout:
    def test_every_urlopen_in_the_package_passes_a_timeout(self) -> None:
        offenders: list[str] = []
        for source in package_sources():
            hits = _missing_timeout(source.read_text(encoding="utf-8"))
            offenders.extend(f"{label(source)}:{line}" for line in hits)

        assert offenders == [], (
            "these urlopen calls have no real timeout (absent, or explicitly "
            f"None), which is an unbounded network read: {offenders}"
        )

    def test_the_census_of_urlopen_sites_is_the_pinned_one(self) -> None:
        found = astwalk.Sites()
        for source in package_sources():
            tree = parse(source.read_text(encoding="utf-8"))
            found += calls_to(
                tree,
                {"urllib.request.urlopen"},
                where=label(source),
                module=module_name(source),
            )

        astwalk.assert_sites(
            found.sorted(),
            seen=EXPECTED_SEEN,
            undecided=EXPECTED_UNDECIDED,
            message="the set of urlopen call sites in kstrl/ moved.",
        )

    def test_the_walk_flags_a_missing_timeout_and_clears_a_present_one(self) -> None:
        assert _missing_timeout("urlopen(req)\n") == ["1"]
        assert _missing_timeout("urlopen(req, timeout=1)\n") == []

    def test_the_walk_flags_an_explicit_none_timeout(self) -> None:
        assert _missing_timeout("urlopen(req, timeout=None)\n") == ["1"]
