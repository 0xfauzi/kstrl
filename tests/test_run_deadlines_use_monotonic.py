"""#203: the deadlines of a run read a clock that does not count a suspend.

``docs/continuous-intake.md`` section 7 decides that a run which starts in a
dark wake can suspend, and that it resumes and completes because its
deadlines do not count the time asleep. That holds only while each deadline
reads ``time.monotonic()`` (``mach_absolute_time()`` on macOS) and not a
wall clock. A deadline moved to ``time.time()`` would kill a resumed run for
the seconds it was asleep, and no behaviour test notices, because no test
can suspend the machine: measured on 2026-10-09, moving
``kstrl/agents/proc.py::DeadlineStreamer`` to ``time.time()`` left 247 tests
in nine agent and process suites green.

WHAT IT PINS. Every call to a wall clock in the modules that hold the run
deadlines the section names, keyed by enclosing scope, and no undecided
call. A new wall-clock read in one of them is a new row and goes red.

WHAT IT DOES NOT SEE. A deadline built on a wall-clock HELPER, such as
``kstrl/serve.py::_utc_now``, is a call to that helper and not to a clock,
so it is not a row. ``_utc_now`` itself is pinned, and the lease and the
schedule gates read it on purpose.
"""

from __future__ import annotations

import subprocess
import time

from tests.helpers import astwalk

#: The modules that hold a run deadline (docs/continuous-intake.md section 7).
RUN_DEADLINE_MODULES = (
    "agents/proc.py",
    "agents/leash.py",
    "agents/liveness.py",
    "serve.py",
)

#: The wall clocks a deadline must not read.
WALL_CLOCKS = frozenset(
    {
        "time.time",
        "time.time_ns",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
    }
)

#: Re-derived by running the walk on 2026-10-09. Both are date stamps, not
#: deadlines: the lease and schedule wall time, and the local day name.
EXPECTED_WALL_CLOCK_SITES = (
    "serve.py::_local_today datetime.datetime.now",
    "serve.py::_utc_now datetime.datetime.now",
)


def _wall_clock_calls() -> astwalk.Sites:
    found = astwalk.Sites()
    for relative in RUN_DEADLINE_MODULES:
        source_file = astwalk.KSTRL_PACKAGE / relative
        tree = astwalk.parsed(source_file)
        found += astwalk.calls_to(
            tree,
            WALL_CLOCKS,
            where=astwalk.label(source_file),
            module=astwalk.module_name(source_file),
            owner=astwalk.scope_of(tree, lambdas=True),
        )
    return found.sorted()


class TestRunDeadlinesReadAClockThatSkipsSleep:
    def test_the_walk_sees_a_planted_wall_clock_deadline(self) -> None:
        """Control, per #324: an empty pin alone cannot tell a narrow walk
        from a switched-off one."""
        tree = astwalk.parse("import time\ndef wait():\n    deadline = time.time() + 5\n")
        found = astwalk.calls_to(tree, WALL_CLOCKS, owner=astwalk.scope_of(tree, lambdas=True))
        assert found.seen == ("::wait time.time",)

    def test_no_run_deadline_module_reads_a_new_wall_clock(self) -> None:
        astwalk.assert_sites(
            _wall_clock_calls(),
            seen=EXPECTED_WALL_CLOCK_SITES,
            undecided=(),
            message=(
                "A run-deadline module reads a wall clock that is not pinned. "
                "A wall clock counts the seconds a suspend lasts, so a deadline on "
                "it kills a run that resumes. Use time.monotonic() (#203, "
                "docs/continuous-intake.md section 7)."
            ),
        )

    def test_the_subprocess_timeout_reads_the_monotonic_clock(self) -> None:
        """The run timeout is ``Popen.communicate(timeout=)``. Section 7
        rests on CPython checking it against ``time.monotonic``."""
        assert subprocess._time is time.monotonic  # type: ignore[attr-defined]
