"""The drift guard #391 asks for: the fake mutmut renderer
(:mod:`tests.helpers.fakemutmut`) compared against a REAL mutmut 2.5.1
report, byte for byte where it can be and structurally where it cannot.

:data:`RECORDED_JUNITXML` is the real report, verbatim, produced by:

    mutmut 2.5.1, python 3.12, fixture <lane>/fixtureC (mod.py with add,
    countdown, scale, clamp; test_mod.py at the repo root):
      mutmut run --paths-to-mutate=mod.py --tests-dir=<empty dir> --no-progress \\
        --simple-output --runner="<venv>/python -m pytest -x"   -> exit 2
      mutmut junitxml --untested-policy=error --suspicious-policy=error -> exit 0

Read through ``tests/tool_output/``'s own accessor (#258; #391 simplify
pass on PR #392, B7) rather than a Python string constant in this module:
that directory is the house convention for captured real tool output,
with its provenance and normalisation policy written once in
``tests/helpers/tool_output.py`` rather than re-solved here. Its trailing
whitespace and blank-line normalisation are already the policy that
directory's own docstring states - a unified diff's blank context lines
losing their one-space padding costs nothing here, because nothing in
this module reads the diff bodies' exact text, only element tags and
``type``/``message`` attributes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from kstrl.adequacy import parse_mutant_report
from tests.helpers.fakemutmut import _STATUS_ELEMENT, junit
from tests.helpers.tool_output import tool_output

RECORDED_JUNITXML = tool_output("mutmut-2.5.1-junitxml.txt")


#: The PARSER's word for a status (:func:`kstrl.adequacy.parse_mutant_report`
#: - ``killed``, ``survived``, ``untested``, ``inconclusive``), mapped back
#: to the FAKE's word for the element that produces it
#: (:func:`tests.helpers.fakemutmut.junit` - ``killed``, ``survived``,
#: ``untested``, ``timeout``). ``inconclusive`` is the parser deliberately
#: refusing to count a ``bad_timeout`` as a detection
#: (:func:`kstrl.adequacy._mutant_status`), and ``timeout`` is the fake's
#: name for the element that renders it. Feeding a recording's parsed rows
#: straight into :func:`junit` without this map raises
#: ``KeyError: 'inconclusive'``: the two vocabularies are different sets.
PARSED_TO_FAKE: dict[str, str] = {
    "killed": "killed",
    "survived": "survived",
    "untested": "untested",
    "inconclusive": "timeout",
}


def _child_signature(testcase: ET.Element) -> tuple[str, str, str] | None:
    """``(tag, type, message)`` of a testcase's ``failure``/``error``
    child, or ``None`` when it has neither (a killed mutant)."""
    for tag in ("failure", "error"):
        child = testcase.find(tag)
        if child is not None:
            return (tag, child.get("type", ""), child.get("message", ""))
    return None


def test_the_fake_renders_what_real_mutmut_renders() -> None:
    """Round-trip the recording through the real parser and the fake
    renderer: parse the recorded report, render the same rows back with
    :func:`junit`, parse that, and the two tuples of ``Mutant`` must be
    equal.

    The totality assertion runs BEFORE the round trip: a parsed status
    this map does not cover must fail loudly here, never be silently
    dropped by a ``KeyError`` deep in :func:`junit`.
    """
    recorded = parse_mutant_report(RECORDED_JUNITXML)
    statuses = {m.status for m in recorded}
    unmapped = statuses - set(PARSED_TO_FAKE)
    assert not unmapped, f"parsed status(es) with no PARSED_TO_FAKE entry: {sorted(unmapped)}"
    # The recording must keep its variety, or this test would pass
    # vacuously on a recording that lost it.
    assert {"killed", "survived", "inconclusive"} <= statuses

    rendered_xml = junit(
        *[(m.mutant_id, m.path, m.line, PARSED_TO_FAKE[m.status]) for m in recorded]
    )
    rendered = parse_mutant_report(rendered_xml)
    assert recorded == rendered


#: Statuses :func:`tests.helpers.fakemutmut.junit` can render that no
#: real mutmut 2.5.1 recording exists for (#391 simplify pass on PR
#: #392, A4): ``untested`` - mutmut's junitxml raises ``ValueError:
#: Obtained null mutant`` on a cache left by a killed run under
#: ``--untested-policy=error`` (measured twice), and under any other
#: policy an un-run mutant renders exactly like a killed one, so there is
#: no untested cache this driver could ever read to record one from. A
#: DISCLOSED blind spot, not a silent one: the test below is a CENSUS
#: over :data:`_STATUS_ELEMENT`'s own keys, not a fixed list of three, so
#: a future FIFTH status the fake grows must be matched against a
#: recording or added here, or that census goes red rather than silently
#: passing - the exact shape of #391 itself, recurring one layer in.
_NO_RECORDING_EXISTS_FOR: frozenset[str] = frozenset({"untested"})


def test_the_status_elements_the_fake_writes_are_the_ones_in_the_recording() -> None:
    """Independent of :func:`parse_mutant_report`, and compared as XML
    rather than as bytes: the fake writes a SELF-CLOSING timeout element
    (``<error type="timeout" message="bad_timeout"/>``) where the
    recording writes one with a diff body and a closing tag. Those are
    the same element, and a byte comparison would be a false failure.
    """
    recorded_root = ET.fromstring(RECORDED_JUNITXML)
    testcases = list(recorded_root.iter("testcase"))
    recorded_signatures = {_child_signature(tc) for tc in testcases}

    expected: dict[str, tuple[str, str, str] | None] = {
        "killed": None,
        "survived": ("failure", "failure", "bad_survived"),
        "timeout": ("error", "timeout", "bad_timeout"),
    }
    # A CENSUS over the fake's own status vocabulary (#391 simplify pass
    # on PR #392, A4), not a hand-picked list of three: every key
    # `_STATUS_ELEMENT` defines must appear either in `expected` above
    # (matched against the recording, below) or in
    # `_NO_RECORDING_EXISTS_FOR` (an explicit, disclosed blind spot). This
    # guard CLEARS - it reports the fake's rendering as pinned to the
    # recording - so where it cannot PROVE that, it must flag rather than
    # stay silent: a fifth invented status entering the fake with neither
    # would otherwise leave this assertion green.
    fake_statuses = set(_STATUS_ELEMENT)
    covered = set(expected) | _NO_RECORDING_EXISTS_FOR
    assert fake_statuses == covered, (
        f"unaccounted fake status(es) {sorted(fake_statuses - covered)}: match against "
        "the recording above or add to _NO_RECORDING_EXISTS_FOR with a measured reason"
    )

    for status, signature in expected.items():
        rendered_xml = junit((1, "mod.py", 1, status))
        rendered_root = ET.fromstring(rendered_xml)
        rendered_testcase = next(rendered_root.iter("testcase"))
        assert _child_signature(rendered_testcase) == signature
        assert signature in recorded_signatures

    # Every testcase in the recording carries a system-out child, killed
    # ones included - not only the ones without a failure/error child,
    # which is a strictly weaker claim than the recording supports.
    assert all(tc.find("system-out") is not None for tc in testcases)
    assert 'message="bad_survived"' in RECORDED_JUNITXML
    assert 'message="bad_timeout"' in RECORDED_JUNITXML


@pytest.mark.xfail(
    strict=True,
    reason=(
        "no real rendering could be recorded for "
        + ", ".join(sorted(_NO_RECORDING_EXISTS_FOR))
        + ": mutmut 2.5.1's junitxml raises ValueError: Obtained null mutant on a "
        "cache left by a killed run (measured twice), and under any other untested "
        "policy an un-run mutant renders exactly like a killed one"
    ),
)
def test_an_untested_rendering_is_recorded() -> None:
    """The DISCLOSED blind spot: the day someone records a real
    ``untested`` rendering, this XPASSes and fails loudly instead of the
    gap staying invisible. Keyed off :data:`_NO_RECORDING_EXISTS_FOR`
    (#391 simplify pass on PR #392, A4) rather than the bare literal, so
    the two cannot drift apart."""
    assert all(f'message="{status}"' in RECORDED_JUNITXML for status in _NO_RECORDING_EXISTS_FOR)
