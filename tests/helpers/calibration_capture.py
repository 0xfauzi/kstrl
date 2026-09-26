"""Drive the real #398 capture harness in a child interpreter.

Moved out of ``tests/test_calibration_incremental.py`` (#398 simplify pass,
group B2): the driver source below reaches five privates of
``tests.test_calibration`` (``RESULTS_DIR``, ``_DetectionReport``,
``_gate_on_consistency``, ``_measure_detection``, ``_measure_false_positives``)
through a module-level string that a child process ``exec``s, so no real
``import`` statement names that dependency anywhere - ruff, mypy and
``tests/test_helper_import_direction.py`` all see nothing. The string-driver
SHAPE has precedent elsewhere in this suite (``test_instance_safety.py``,
``test_spine_crash_recovery.py``, ``test_spine_worktree.py``); what was wrong
was its address. ``tests/helpers/runners.py`` states the rule this follows: a
helper more than one test module might need has exactly one place to be.

Four fixtures, in run order, driven through three different gate helpers so
that a helper nobody instrumented shows up here rather than shipping quietly:
``fx-a`` and ``fx-d`` go through ``_gate_on_consistency``, ``fx-b`` through
``_measure_detection``, ``fx-c`` (a negative fixture) through
``_measure_false_positives``.

Six ways a run can stop, answered differently by the code under test:

- ``complete``: every fixture finishes and teardown (``report.save()``) runs.
- ``sigkill``: ``fx-d`` runs 2 of its ``KSTRL_CALIBRATION_RUNS=2`` runs;
  the first succeeds and reaches ``record``, then SIGKILL lands as the
  second starts, so no teardown of any kind runs. Killing on the SECOND
  run rather than the first is what makes this mode prove anything about
  ``record``'s own flush (#398 A1): a kill on the first run never calls
  ``record`` for ``fx-d`` at all, so it cannot tell a working per-run
  flush from a missing one - only ``begin_fixture``'s flush (which fires
  before either run) would be exercised.
- ``raise``: the same run-2-of-``fx-d`` timing, but an exception fires
  instead of a signal, so teardown DOES run with that fixture left
  dangling.
- ``kill_after_last``: SIGKILL lands immediately after ``fx-d``'s own
  ``complete_fixture`` call returns, before ``report.save()`` at teardown.
  This is the one window ``record``'s per-run flush (#398 A1) does not cover:
  every run's data already reached disk during the loop, but the fact that
  ``fx-d`` graduated from attempted to completed is only written by
  ``complete_fixture``'s own flush. Proves that flush earns its place (#398
  A3) rather than being redundant with the next fixture's ``begin_fixture``,
  which never comes because there is no next fixture.
- ``no_reply``: fx-d's first run returns without asking an agent, so it has
  no reply to keep (#523). The run is refused before it is recorded and the
  capture is saved as partial.
- ``reply_unwritable``: a regular file sits where fx-d's replies directory
  has to go, so writing its first reply fails (#523). Same outcome.

Every other run asks a fake agent through the real ``_collect``, which
answers with :func:`reply_text`, so a kept reply names its fixture and run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Every fixture the child drives, as ``role/fixture_id`` in run order.
#: fx-c goes through the false-positive helper, so it is recorded and
#: completed but never appears in the report's ``fixtures`` list.
FX_A, FX_B, FX_C, FX_D = "security/fx-a", "reviewer/fx-b", "reviewer/fx-c", "security/fx-d"

#: Runs the real harness against a fake agent. ``KSTRL_TEST_MODE`` decides
#: how the run ends. All four helpers are driven so that instrumenting only
#: one of the three gate helpers fails here rather than silently shipping.
_DRIVER = """
from __future__ import annotations

import os
import signal
from pathlib import Path

import tests.test_calibration as tc
from tests.helpers.calibration_capture import reply_text
from tests.helpers.calibration_replies import replies_dir

tc.RESULTS_DIR = Path(os.environ["KSTRL_TEST_RESULTS_DIR"])
MODE = os.environ["KSTRL_TEST_MODE"]
report = tc._DetectionReport()


_calls = {}


# Streams one line and leaves the same text as its final message, so a
# kept reply names the fixture and run it belongs to (#523).
class FakeAgent:
    def __init__(self, text):
        self.text = text
        self.final_message = None

    def run(self, prompt, cwd=None, timeout=None):
        self.final_message = self.text
        yield self.text


def run_once_for(fixture_id):
    def run_once():
        _calls[fixture_id] = _calls.get(fixture_id, 0) + 1
        run = _calls[fixture_id]
        # Kill/raise on fx-d's SECOND run, not its first: the first run
        # must reach `record` and be flushed to disk before the process
        # dies, or this mode proves nothing about `record`'s own flush
        # (#398 A1) - only `begin_fixture`'s, which already fired before
        # either run.
        if fixture_id == "fx-d" and MODE == "sigkill" and run == 2:
            os.kill(os.getpid(), signal.SIGKILL)
        if fixture_id == "fx-d" and MODE == "reply_unwritable" and run == 1:
            # A regular file where fx-d's replies directory has to go.
            blocker = replies_dir(tc.RESULTS_DIR, report.timestamp) / "security" / "fx-d"
            blocker.parent.mkdir(parents=True, exist_ok=True)
            blocker.write_text("not a directory", encoding="utf-8")
        if not (fixture_id == "fx-d" and MODE == "no_reply"):
            tc._collect(FakeAgent(reply_text(fixture_id, run)), "prompt", Path("."))
        # Raised AFTER the agent call, so the run has a reply to keep and only
        # the exception itself keeps it from being recorded. Raised before it,
        # an empty reply would stop the run too and hide a widened except (#523).
        if fixture_id == "fx-d" and MODE == "raise" and run == 2:
            raise RuntimeError("interrupted inside fx-d")
        return True, "fake result for " + fixture_id

    return run_once


try:
    tc._gate_on_consistency(
        "security", "fx-a", report, run_once_for("fx-a"), category="injection"
    )
    tc._measure_detection(
        "reviewer", "fx-b", report, run_once_for("fx-b"), category="scope_creep"
    )
    tc._measure_false_positives("reviewer", "fx-c", report, run_once_for("fx-c"))
    tc._gate_on_consistency(
        "security", "fx-d", report, run_once_for("fx-d"), category="injection"
    )
    if MODE == "kill_after_last":
        os.kill(os.getpid(), signal.SIGKILL)
finally:
    report.save()
"""

#: What each mode's child exits with. ``sigkill``/``kill_after_last`` cannot
#: be caught, so the child dies on the signal and ``subprocess`` reports the
#: negated number.
EXPECTED_EXIT: dict[str, int] = {
    "complete": 0,
    "sigkill": -signal.SIGKILL,
    "raise": 1,
    "kill_after_last": -signal.SIGKILL,
    "no_reply": 1,
    "reply_unwritable": 1,
}


def reply_text(fixture_id: str, run: int) -> str:
    """What the child's fake agent replies on ``run`` of ``fixture_id``."""
    return f"reply of {fixture_id} run {run}"


def capture(tmp_dir: Path, mode: str) -> Path:
    """Run one fake capture in a child and return its single baseline file.

    The child spawns nothing itself, so ``subprocess.run(timeout=)`` bounds
    the only process there is, and a mutation that removes a write shows up
    as a failed assertion rather than as a hang.
    """
    results = tmp_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    driver = tmp_dir / "capture_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["KSTRL_TEST_RESULTS_DIR"] = str(results)
    env["KSTRL_TEST_MODE"] = mode
    # Pinned rather than inherited: the report's model label and the run
    # count both come from the environment, so a developer's exported value
    # would otherwise change what these tests are looking at.
    env["KSTRL_CALIBRATION_RUNS"] = "2"
    env["KSTRL_CALIBRATION_MODEL"] = "haiku"
    env["KSTRL_CALIBRATION_CHANGE_SOURCE"] = "repo"
    env["KSTRL_RUN_CALIBRATION"] = "0"
    env.pop("KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE", None)
    env.pop("KSTRL_CALIBRATION_REVIEWER_MODEL", None)

    proc = subprocess.run(
        [sys.executable, str(driver)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == EXPECTED_EXIT[mode], (
        f"child exit {proc.returncode}, expected {EXPECTED_EXIT[mode]} for mode "
        f"{mode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    files = sorted(results.glob("baseline-*.json"))
    assert len(files) == 1, (
        f"a run owns exactly one file; found {[p.name for p in files]}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return files[0]
