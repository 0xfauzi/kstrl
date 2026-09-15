"""#204: the keepalive loop's pacing, measured through the real CLI.

`docs/continuous-intake.md` section 4 tells an operator that keepalive
mode paces itself from `[serve] poll_interval_seconds`. Nothing pinned
what that costs, so the doc's arithmetic - N cycles cost N-1 polls - was
prose. The first two tests are that arithmetic, run against the real `ks
serve` process in a real temp root.

Both bounds are expressed in POLLS, not in seconds, so the assertions do
not depend on how loaded the machine is. Measured: N=1 costs 0.52s and
N=3 costs 11.0s at a 5s poll, against bounds of 5.0 and [10.0, 15.0).

`tests/test_serve.py::TestServeLoop::test_the_loop_sleeps_between_cycles`
already pins the CALL, and does it faster. It cannot pin the BINDING:
every test in that class passes `sleeper=`, so the production
`sleep = sleeper or time.sleep` is the one line in the loop nothing
executes. Measured: replacing that fallback with a no-op leaves
`tests/test_serve.py`, `tests/test_serve_cli.py` and
`tests/test_serve_seam.py` at 317 passed, and fails the second test
below in 1.33s. That gap is the whole reason this file runs a real
process.

The third test is the regression test for the documentation defect
itself. Section 4 quotes two spellings out of `kstrl/serve.py`, and a
quote nobody checks is a claim that rots. Measured: at cb3b88d, before
the section 4 edit, it fails on the doc half in 0.06s.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

#: Large enough that startup plus three no-op cycles (measured 0.52s
#: idle, 2.5s at load 105) sits well inside one poll, so the upper bound
#: below catches an extra poll rather than a slow machine.
POLL_SECONDS = 5.0

#: A fuse, not a bound on the expected runtime. A mutation that removes a
#: sleep fails an assertion; a mutation that makes the loop never return
#: would otherwise hang the suite, so it is failed explicitly instead.
FUSE_SECONDS = 120.0

_CYCLES_LINE = re.compile(r"^\s*cycles:\s+(\d+)\s*$", re.MULTILINE)

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The two spellings section 4 of `docs/continuous-intake.md` quotes out
#: of `kstrl/serve.py`. Both halves are asserted: the source must still
#: spell them, and the doc must still quote them.
_QUOTED_CALLS = (
    "sleep = sleeper or time.sleep",
    "sleep(cfg.poll_interval_seconds)",
)


def _serve_root(tmp_path: Path) -> Path:
    """A root `ks serve` will poll without spending or reaching the network.

    The queue is empty, so no item is ever claimed. `max_open_prs = 0`
    switches off the flow-control bound, which would otherwise shell out
    to `gh` from a directory that is not a checkout.
    """
    (tmp_path / "kstrl.toml").write_text(
        "[serve]\n"
        f"poll_interval_seconds = {POLL_SECONDS:.0f}\n"
        "caffeinate = false\n"
        "max_open_prs = 0\n",
        encoding="utf-8",
    )
    return tmp_path


def _run_serve(root: Path, cycles: int) -> tuple[str, float]:
    """Run the real CLI to completion; return its output and wall time.

    `python -m kstrl` is the same entry point as the `ks` console script
    (`kstrl/__main__.py` imports `kstrl.cli:main`, which is what
    `pyproject.toml` maps `ks` to) and needs no installed script on PATH.

    `start_new_session=True` plus `killpg` because `subprocess`'s own
    timeout kills only the process it spawned, and this one is an
    interpreter that may have children of its own.
    """
    argv = [
        sys.executable,
        "-m",
        "kstrl",
        "serve",
        "--root",
        str(root),
        "--max-cycles",
        str(cycles),
        "--no-color",
    ]
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=FUSE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        pytest.fail(f"ks serve --max-cycles {cycles} did not exit within {FUSE_SECONDS}s")
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, out
    match = _CYCLES_LINE.search(out)
    assert match is not None, f"no cycles line in:\n{out}"
    assert int(match.group(1)) == cycles, out
    return out, elapsed


def test_one_cycle_costs_no_poll(tmp_path: Path) -> None:
    """The poll is BETWEEN cycles, so a single cycle waits for nothing."""
    out, elapsed = _run_serve(_serve_root(tmp_path), 1)
    assert elapsed < POLL_SECONDS, f"one cycle waited a poll ({elapsed:.3f}s)\n{out}"


def test_three_cycles_cost_exactly_two_polls(tmp_path: Path) -> None:
    """N cycles cost N-1 polls: the claim section 4 of the runbook makes."""
    out, elapsed = _run_serve(_serve_root(tmp_path), 3)
    assert elapsed >= 2 * POLL_SECONDS, f"three cycles skipped a poll ({elapsed:.3f}s)\n{out}"
    assert elapsed < 3 * POLL_SECONDS, f"three cycles cost three polls ({elapsed:.3f}s)\n{out}"


def test_the_doc_quotes_the_poll_sleep_that_is_actually_there() -> None:
    """Section 4 quotes serve.py, so a rename would make the doc false.

    Neither e2e test above can see that: a rewrite that keeps the timing
    and changes the spelling leaves both green. This is the guard for
    the change the `Do not` list forbids, a monotonic deadline loop.
    """
    source = (_REPO_ROOT / "kstrl" / "serve.py").read_text(encoding="utf-8")
    doc = (_REPO_ROOT / "docs" / "continuous-intake.md").read_text(encoding="utf-8")
    for spelling in _QUOTED_CALLS:
        assert spelling in source, f"kstrl/serve.py no longer spells {spelling!r}"
        assert spelling in doc, f"docs/continuous-intake.md no longer quotes {spelling!r}"
