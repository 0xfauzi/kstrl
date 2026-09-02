"""The anti-vacuity body every disclosed limit is pinned with."""

from __future__ import annotations

from collections.abc import Callable

# --- disclosed limits -----------------------------------------------------


def blind_spot(probe: Callable[[str], object], source: str) -> None:
    """The body of a disclosed limit's anti-vacuity test.

    Used under ``@pytest.mark.xfail(strict=True, raises=AssertionError)``.
    The assertion says the walk DOES see the source; the marker says it is
    expected not to. The row passes only while the limit holds, and the
    day somebody widens the walk it XPASSes, which ``strict=True`` makes a
    failure and the disclosure has to be edited in the same diff.
    ``raises=AssertionError`` makes a resolver that CRASHES fail too: #328
    measured an open hole, a closed hole and a resolver raising on entry
    all passing green under a plain non-strict xfail. A disclosure with no
    test behind it rots silently; this is the test.
    """
    assert probe(source), (
        "the walk still cannot see this, which is what the guard's "
        "docstring discloses. If this row now XPASSes the walk got "
        "stronger: move it into the caught set and edit the disclosure."
    )
