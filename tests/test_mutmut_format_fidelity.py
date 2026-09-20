"""The drift guard #391 asks for: the fake mutmut renderer
(:mod:`tests.helpers.fakemutmut`) compared against a REAL mutmut 2.5.1
report, byte for byte where it can be and structurally where it cannot.

:data:`RECORDED_JUNITXML` is the real report, verbatim, produced by:

    mutmut 2.5.1, python 3.12, fixture <lane>/fixtureC (mod.py with add,
    countdown, scale, clamp; test_mod.py at the repo root):
      mutmut run --paths-to-mutate=mod.py --tests-dir=<empty dir> --no-progress \\
        --simple-output --runner="<venv>/python -m pytest -x"   -> exit 2
      mutmut junitxml --untested-policy=error --suspicious-policy=error -> exit 0

Copied exactly: tabs, attribute order, entity escapes (``&gt;``) and the
diff text inside the ``failure``/``error`` elements are all evidence, not
formatting to clean up. The constant sits at column zero - the longest
line is 95 characters, so indenting it would trip ruff's
``line-length = 100`` - and ends with ONE newline after
``</testsuites>``; the capture file itself ends with two, and the extra
blank line is not part of the XML.

A unified diff's blank context lines are one space character, per the
format, not an empty line. Pre-commit's own ``trailing-whitespace`` hook
strips a literal trailing space on sight, which would silently turn this
"copied exactly" claim false, so those twelve lines are spelled ``\x20``
instead: the same character once Python reads the string, invisible to a
hook that only looks at the file's raw bytes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from kstrl.adequacy import parse_mutant_report
from tests.helpers.fakemutmut import junit

RECORDED_JUNITXML = """<?xml version="1.0" ?>
<testsuites disabled="0" errors="2" failures="2" tests="11" time="0.0">
	<testsuite disabled="0" errors="2" failures="2" name="mutmut" skipped="0" tests="11" time="0">
		<testcase name="Mutant #1" file="mod.py" line="2">
			<system-out>    return a + b</system-out>
		</testcase>
		<testcase name="Mutant #2" file="mod.py" line="6">
			<system-out>    while n &gt; 0:</system-out>
		</testcase>
		<testcase name="Mutant #3" file="mod.py" line="6">
			<system-out>    while n &gt; 0:</system-out>
		</testcase>
		<testcase name="Mutant #4" file="mod.py" line="7">
			<error type="timeout" message="bad_timeout">--- mod.py
+++ mod.py
@@ -4,7 +4,7 @@
\x20
 def countdown(n):
     while n &gt; 0:
-        n -= 1
+        n = 1
     return n
\x20
\x20
</error>
			<system-out>        n -= 1</system-out>
		</testcase>
		<testcase name="Mutant #5" file="mod.py" line="7">
			<error type="timeout" message="bad_timeout">--- mod.py
+++ mod.py
@@ -4,7 +4,7 @@
\x20
 def countdown(n):
     while n &gt; 0:
-        n -= 1
+        n += 1
     return n
\x20
\x20
</error>
			<system-out>        n -= 1</system-out>
		</testcase>
		<testcase name="Mutant #6" file="mod.py" line="7">
			<system-out>        n -= 1</system-out>
		</testcase>
		<testcase name="Mutant #7" file="mod.py" line="12">
			<system-out>    return x * 2</system-out>
		</testcase>
		<testcase name="Mutant #8" file="mod.py" line="12">
			<system-out>    return x * 2</system-out>
		</testcase>
		<testcase name="Mutant #9" file="mod.py" line="16">
			<failure type="failure" message="bad_survived">--- mod.py
+++ mod.py
@@ -13,7 +13,7 @@
\x20
\x20
 def clamp(v):
-    if v &gt; 10:
+    if v &gt;= 10:
         return 10
     return v
\x20
</failure>
			<system-out>    if v &gt; 10:</system-out>
		</testcase>
		<testcase name="Mutant #10" file="mod.py" line="16">
			<failure type="failure" message="bad_survived">--- mod.py
+++ mod.py
@@ -13,7 +13,7 @@
\x20
\x20
 def clamp(v):
-    if v &gt; 10:
+    if v &gt; 11:
         return 10
     return v
\x20
</failure>
			<system-out>    if v &gt; 10:</system-out>
		</testcase>
		<testcase name="Mutant #11" file="mod.py" line="17">
			<system-out>        return 10</system-out>
		</testcase>
	</testsuite>
</testsuites>
"""


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
        "no real `untested` rendering could be recorded: mutmut 2.5.1's "
        "junitxml raises ValueError: Obtained null mutant on a cache left "
        "by a killed run (measured twice), and under any other untested "
        "policy an un-run mutant renders exactly like a killed one"
    ),
)
def test_an_untested_rendering_is_recorded() -> None:
    """The DISCLOSED blind spot: the day someone records a real
    ``untested`` rendering, this XPASSes and fails loudly instead of the
    gap staying invisible."""
    assert 'message="untested"' in RECORDED_JUNITXML
