"""#204: the poll's cost, measured through the real CLI.

`docs/continuous-intake.md` section 4 says keepalive mode paces itself
with one `sleep(cfg.poll_interval_seconds)` after each cycle, and section
7 rests its arithmetic on `--max-cycles N` costing N-1 polls. The first
test is that arithmetic, run against the real `ks serve` process in a
real temp root, with bounds in POLLS so the assertion does not depend on
machine load.

`tests/test_serve.py::TestServeLoop::test_the_loop_sleeps_between_cycles`
already pins the CALL. It cannot pin the BINDING: every test in that
class passes `sleeper=`, so the production `sleep = sleeper or time.sleep`
is the one line in the loop nothing executes. Measured: replacing that
fallback with a no-op leaves `tests/test_serve.py`, `tests/test_serve_cli.py`
and `tests/test_serve_seam.py` at 317 passed, and fails the N=3 case
below in about 1.3s. That gap is why this file runs a real process.

The second test is the regression test for the documentation defect: the
doc quotes a spelling out of `kstrl/serve.py`, and a quote nobody checks
rots. It is a rename tripwire, nothing more: it cannot tell which of the
two loop branches the spelling survives in.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.helpers.procs import kill_group

#: Large enough that startup plus three no-op cycles (measured 0.33s
#: idle, 2.5s at load 105) sits inside one poll, so the upper bound
#: catches an extra poll rather than a slow machine.
POLL_SECONDS = 5

#: The spelling section 4 of `docs/continuous-intake.md` quotes out of
#: `kstrl/serve.py`. Both halves are asserted: the source must still
#: spell it, and the doc must still quote it.
QUOTED_CALL = "sleep(cfg.poll_interval_seconds)"


def _run_serve(root: Path, cycles: int) -> tuple[str, float]:
    """Run the real CLI to completion in `root`; return output and wall time.

    The queue is empty, so no item is ever claimed. `max_open_prs = 0`
    switches off the flow-control bound, which would otherwise shell out
    to `gh` from a directory that is not a checkout. `python -m kstrl` is
    the entry point `pyproject.toml` maps `ks` to, and needs nothing on
    PATH.

    The fuse is a failure path, not a bound on the expected runtime: a
    mutation that makes the loop never return fails loudly instead of
    hanging the suite. The child runs in its own session and the group is
    killed on every exit path, because `subprocess`'s own timeout kills
    only the interpreter it spawned.
    """
    (root / "kstrl.toml").write_text(
        f"[serve]\npoll_interval_seconds = {POLL_SECONDS}\nmax_open_prs = 0\n",
        encoding="utf-8",
    )
    argv = [sys.executable, "-m", "kstrl", "serve", "--root", str(root)]
    argv += ["--max-cycles", str(cycles), "--no-color"]
    fuse = (cycles - 1) * POLL_SECONDS + 30.0
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=fuse)
    except subprocess.TimeoutExpired:
        pytest.fail(f"ks serve --max-cycles {cycles} did not exit within {fuse}s")
    finally:
        kill_group(proc.pid)
        proc.kill()
        proc.wait(timeout=10)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, out
    assert re.search(rf"^\s*cycles:\s+{cycles}\s*$", out, re.MULTILINE), out
    return out, elapsed


@pytest.mark.parametrize("cycles", [1, 3])
def test_n_cycles_cost_n_minus_one_polls(tmp_path: Path, cycles: int) -> None:
    """The poll is BETWEEN cycles: one cycle waits for nothing, three wait twice.

    N=3 rather than N=2 because N=2 cannot separate "one poll per gap" from
    "one poll ever", which is what a sleep hoisted out of the loop looks like.
    """
    out, elapsed = _run_serve(tmp_path, cycles)
    assert (cycles - 1) * POLL_SECONDS <= elapsed < cycles * POLL_SECONDS, (
        f"{cycles} cycles cost {elapsed / POLL_SECONDS:.2f} polls\n{out}"
    )


def test_the_doc_quotes_the_poll_sleep_that_is_actually_there() -> None:
    """Section 4 quotes serve.py, so a rename would make the doc false."""
    source = (REPO_ROOT / "kstrl" / "serve.py").read_text(encoding="utf-8")
    doc = (REPO_ROOT / "docs" / "continuous-intake.md").read_text(encoding="utf-8")
    assert QUOTED_CALL in source, f"kstrl/serve.py no longer spells {QUOTED_CALL!r}"
    assert QUOTED_CALL in doc, f"docs/continuous-intake.md no longer quotes {QUOTED_CALL!r}"
