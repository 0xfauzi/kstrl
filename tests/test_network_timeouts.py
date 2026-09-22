"""Every ``urlopen`` in kstrl/ carries a real timeout (#155, R8.8 slice 1).

Measured: a ``urlopen`` with no ``timeout=`` was caught by zero of 7289
passing tests on the unguarded tree. This guard FLAGS rather than
clears - over-matching costs a false positive somebody reads; a guard
that clears would delete the mechanism if it over-matched instead.

The ``seen`` tuple below is re-derived by RUNNING the walk, never typed
from a design document: reading a pin is not running a guard.

The timeout check resolves the callee through
``astwalk.resolved_calls`` rather than a bare ``leaf_name`` walk (#155
fix round, A2b). ``leaf_name`` reads only the last identifier WRITTEN at
the call site, so ``_open = urllib.request.urlopen`` followed by
``_open(request)`` never spells ``"urlopen"`` and a walk keyed on that
identifier alone never even recognises the call, let alone checks its
timeout. Measured with exactly that alias planted in ``signals.py`` (the
timeout dropped in the same edit): the leaf-name walk reported zero
offenders, and ``resolved_calls`` (which resolves the alias back to
``urllib.request.urlopen`` through the same binding table
``calls_to`` uses) reported the site.
"""

from __future__ import annotations

import ast

from tests.helpers import astwalk
from tests.helpers.astwalk import (
    calls_to,
    label,
    module_name,
    package_sources,
    parse,
    resolved_calls,
)

#: Every ``urllib.request.urlopen`` call site in kstrl/, re-derived by
#: running the walk below. The undecided half is the four shared sites
#: every other guard built on ``tests.helpers.astwalk`` already pins.
EXPECTED_SEEN: tuple[str, ...] = (
    "licensing.py:162 urllib.request.urlopen",
    "linear.py:314 urllib.request.urlopen",
    "signals.py:554 urllib.request.urlopen",
)

EXPECTED_UNDECIDED: tuple[str, ...] = (
    "gateparse.py:112 TOOL_PARSERS[chosen]",
    "gateparse.py:114 TOOL_PARSERS[name]",
    "tui/app.py:363 initial_screens_for_kind(kind, observe_only=True)",
    "tui/app.py:435 initial_screens_for_kind(kind, observe_only=False)",
)


def _missing_timeout(source: str, module: str = "") -> list[str]:
    """Every ``urlopen`` call in ``source`` with no real ``timeout=``,
    resolved through :func:`~tests.helpers.astwalk.resolved_calls` so a
    call reached through a local alias is still caught (#155 fix round
    A2b; see module docstring).

    Flags both an absent ``timeout`` keyword AND an explicit
    ``timeout=None``: the latter is the same unbounded read as the
    former, and a guard that only checks for the keyword's PRESENCE is
    one ``= None`` default away from clearing the defect it exists to
    catch.
    """
    tree = parse(source)
    hits: list[str] = []
    for node, _origin in resolved_calls(tree, {"urllib.request.urlopen"}, module=module):
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
        sites_seen = 0
        for source in package_sources():
            text = source.read_text(encoding="utf-8")
            mod = module_name(source)
            sites_seen += len(resolved_calls(parse(text), {"urllib.request.urlopen"}, module=mod))
            hits = _missing_timeout(text, module=mod)
            offenders.extend(f"{label(source)}:{line}" for line in hits)

        # If the walk ever finds nothing, the audit itself broke (import
        # style changed, package moved, the resolver stopped seeing the
        # target) - fail loudly, never read a missing scan as a clean one.
        assert sites_seen >= 3, (
            f"resolved_calls only found {sites_seen} urlopen call sites; "
            "the walk is broken, not the code clean"
        )
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
        no_timeout = "import urllib.request\nurllib.request.urlopen(req)\n"
        with_timeout = "import urllib.request\nurllib.request.urlopen(req, timeout=1)\n"
        assert _missing_timeout(no_timeout, module="probe") == ["2"]
        assert _missing_timeout(with_timeout, module="probe") == []

    def test_the_walk_flags_an_explicit_none_timeout(self) -> None:
        source = "import urllib.request\nurllib.request.urlopen(req, timeout=None)\n"
        assert _missing_timeout(source, module="probe") == ["2"]

    def test_the_walk_resolves_a_call_reached_through_a_local_alias(self) -> None:
        """The regression this guard exists to catch (#155 fix round A2b).

        A plain ``leaf_name`` walk never matches a call made through a
        local alias, so it clears a timeout-less ``urlopen`` it never
        even recognised as an ``urlopen`` call in the first place -
        measured on this exact shape planted in ``signals.py`` (module
        docstring)."""
        source = (
            "import urllib.request\n"
            "_open = urllib.request.urlopen\n"
            "with _open(req) as response:\n"
            "    pass\n"
        )
        assert _missing_timeout(source, module="probe") == ["3"]
