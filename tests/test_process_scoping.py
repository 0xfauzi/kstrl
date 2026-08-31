"""#292 as a class: no test may search the whole machine for a process.

The issue's own framing is "a machine-wide ``pgrep`` in a test suite is a
class, not an instance". Fixing the one test that had it leaves nothing
stopping the next one, and this repo's rule is that if there is no
mechanism there is no plan. So the rule stated in
``tests/helpers/procs.py`` is enforced here rather than only written
down:

    A test may assert on a process it can name. It may not assert on
    what else the machine happens to be running.

What that rule bought, concretely: ``pgrep -f "sleep 60"`` matches an
unrelated ``sleep 600`` in any other session, because the pattern is a
substring of the full command line. Roughly six agents diagnosed the
resulting failure separately, several with controlled A/B runs against a
clean ref, and every one correctly concluded it was unrelated to their
diff. The cost was not the bug, it was that the failure mode pointed
nowhere near its cause.

Swept when this landed: ``test_shutdown.py`` was the only instance in the
suite. Every other process test already names a pid it spawned
(``test_timeout_enforcement`` via a pidfile, ``test_serve`` via a printed
pid, ``test_spine_crash_recovery`` via ``killpg`` on its own child). The
net is here to keep that true.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: Commands that answer "what else is this machine running?". A test
#: asking that is asserting about processes it did not start.
MACHINE_WIDE_PROCESS_TOOLS = frozenset({"pgrep", "pkill", "killall", "pidof"})

#: The one deliberate exception. #292's regression test runs the OLD
#: machine-wide assertion against the same machine state as the new
#: group-scoped one, because running both is what proves they differ. Any
#: OTHER file is a finding.
ALLOWED_FILES = frozenset({"test_shutdown.py"})


def _string_constants(source: Path) -> Iterator[tuple[str, int]]:
    """Every string literal in ``source``, with its line.

    AST-walked rather than grepped: this module's own docstring names
    every forbidden command, and so does the one allowlisted test, so a
    text search would need a suppression list that rots. A literal in the
    tree is the actual claim.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node.lineno


def _machine_wide_hits(source: Path) -> list[str]:
    """Literals in ``source`` that name a machine-wide process search."""
    hits: list[str] = []
    for value, lineno in _string_constants(source):
        words = value.split()
        if words and words[0] in MACHINE_WIDE_PROCESS_TOOLS:
            hits.append(f"{source.name}:{lineno} {value!r}")
    return hits


class TestNoTestSearchesTheWholeMachineForAProcess:
    def test_the_suite_is_clean(self) -> None:
        offenders: list[str] = []
        for source in sorted(TESTS_DIR.rglob("*.py")):
            if source.name in ALLOWED_FILES or source.name == Path(__file__).name:
                continue
            offenders.extend(_machine_wide_hits(source))
        assert offenders == [], (
            f"{offenders} search the whole machine for a process. That is "
            f"#292: `pgrep -f 'sleep 60'` matches an unrelated `sleep 600` "
            f"in any other session, and the failure points nowhere near its "
            f"cause. Use tests/helpers/procs.py and assert on a pid or pgid "
            f"the test itself created."
        )

    def test_the_net_walks_a_real_suite(self) -> None:
        """Without this the sweep could be passing over nothing."""
        assert len(list(TESTS_DIR.rglob("*.py"))) > 50

    def test_the_allowlisted_file_still_needs_its_exemption(self) -> None:
        """An allowlist that outlives its reason is how a net rots.

        If #292's deliberate old-versus-new demonstration is ever
        removed, this fails and the entry should be deleted with it.
        """
        for name in ALLOWED_FILES:
            assert _machine_wide_hits(TESTS_DIR / name), (
                f"{name} is allowlisted but no longer contains a "
                f"machine-wide process search; drop it from ALLOWED_FILES"
            )
