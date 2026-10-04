"""#204: the poll's cost, measured through the real CLI.

`docs/continuous-intake.md` section 4 says keepalive mode paces itself
with one `sleep(cfg.poll_interval_seconds)` after each cycle, and section
7 rests its arithmetic on `--max-cycles N` costing N-1 polls. The first
test is that arithmetic, run against the real `ks serve` process in a
real temp root.

It COUNTS the polls rather than timing them (#704). The first version
asserted `elapsed < N * POLL_SECONDS`, which charged process startup and
the cycles' own work against one poll; at load 12 and load 32 that work
passed 5s and the merge gate refused two unrelated PRs. The count comes
from a `sitecustomize` the child imports at startup, which wraps
`time.sleep` before `kstrl` is imported, so the production fallback
`sleep = sleeper or time.sleep` is the wrapped call and every poll is
recorded with its argument. The wrapper calls the real sleep, so the
lower bound on wall time still proves the polls were waited out.

`tests/test_serve.py::TestServeLoop::test_the_loop_sleeps_between_cycles`
already pins the CALL. It cannot pin the BINDING: every test in that
class passes `sleeper=`, so the production `sleep = sleeper or time.sleep`
is the one line in the loop nothing executes. That gap is why this file
runs a real process.

The second test is the regression test for the documentation defect: the
doc quotes a spelling out of `kstrl/serve.py`, and a quote nobody checks
rots. It is a rename tripwire, nothing more: it cannot tell which of the
two loop branches the spelling survives in.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.helpers.procs import run_serve_subprocess

#: The poll interval the child runs with. No other `time.sleep` on the
#: serve path takes this argument (the others are sub-second retries), so
#: a recorded sleep of exactly this many seconds is a poll.
POLL_SECONDS = 5.0

#: The spelling section 4 of `docs/continuous-intake.md` quotes out of
#: `kstrl/serve.py`. Both halves are asserted: the source must still
#: spell it, and the doc must still quote it.
QUOTED_CALL = "sleep(cfg.poll_interval_seconds)"

#: Env var naming the file the tap appends to.
SLEEP_LOG_ENV = "SERVE_PACING_SLEEP_LOG"

#: Imported by the child at startup because its directory is first on
#: PYTHONPATH. Writes `loaded` once, so a tap that never ran reads as a
#: failure and not as zero polls, then one `sleep <seconds>` line per call.
TAP_SOURCE = f"""\
import os
import time

_LOG = os.environ[{SLEEP_LOG_ENV!r}]
_real_sleep = time.sleep


def _append(line):
    with open(_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\\n")


def _tapped_sleep(seconds):
    _append(f"sleep {{float(seconds)!r}}")
    _real_sleep(seconds)


time.sleep = _tapped_sleep
_append("loaded")
"""


def _run_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cycles: int
) -> tuple[str, float, list[float]]:
    """Run the real CLI to completion; return output, wall time and polls.

    The queue is empty, so no item is ever claimed. `max_open_prs = 0`
    switches off the flow-control bound, which would otherwise shell out
    to `gh` from a directory that is not a checkout.

    The fuse is a failure path, not a bound on the expected runtime: a
    mutation that makes the loop never return fails loudly instead of
    hanging the suite.
    """
    root = tmp_path / "root"
    root.mkdir()
    tap = tmp_path / "tap"
    tap.mkdir()
    (tap / "sitecustomize.py").write_text(TAP_SOURCE, encoding="utf-8")
    log = tmp_path / "sleeps.log"
    inherited = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(tap), inherited) if p))
    monkeypatch.setenv(SLEEP_LOG_ENV, str(log))
    (root / "kstrl.toml").write_text(
        f"[serve]\npoll_interval_seconds = {POLL_SECONDS}\nmax_open_prs = 0\n",
        encoding="utf-8",
    )
    fuse = (cycles - 1) * POLL_SECONDS + 30.0
    started = time.monotonic()
    done = run_serve_subprocess(root, "--max-cycles", str(cycles), fuse=fuse, merge_stderr=True)
    elapsed = time.monotonic() - started
    out = done.stdout
    assert done.returncode == 0, out
    assert re.search(rf"^\s*cycles:\s+{cycles}\s*$", out, re.MULTILINE), out
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert "loaded" in lines, f"the sleep tap never loaded in the child\n{out}"
    sleeps = [float(line.split()[1]) for line in lines if line.startswith("sleep ")]
    return out, elapsed, [s for s in sleeps if s == POLL_SECONDS]


@pytest.mark.parametrize("cycles", [1, 3])
def test_n_cycles_cost_n_minus_one_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cycles: int
) -> None:
    """The poll is BETWEEN cycles: one cycle waits for nothing, three wait twice.

    N=3 rather than N=2 because N=2 cannot separate "one poll per gap" from
    "one poll ever", which is what a sleep hoisted out of the loop looks like.
    """
    out, elapsed, polls = _run_serve(tmp_path, monkeypatch, cycles)
    assert len(polls) == cycles - 1, f"{cycles} cycles took {len(polls)} polls\n{out}"
    assert elapsed >= (cycles - 1) * POLL_SECONDS, (
        f"{cycles} cycles took {elapsed:.2f}s, under {cycles - 1} polls\n{out}"
    )


def test_the_doc_quotes_the_poll_sleep_that_is_actually_there() -> None:
    """Section 4 quotes serve.py, so a rename would make the doc false."""
    source = (REPO_ROOT / "kstrl" / "serve.py").read_text(encoding="utf-8")
    doc = (REPO_ROOT / "docs" / "continuous-intake.md").read_text(encoding="utf-8")
    assert QUOTED_CALL in source, f"kstrl/serve.py no longer spells {QUOTED_CALL!r}"
    assert QUOTED_CALL in doc, f"docs/continuous-intake.md no longer quotes {QUOTED_CALL!r}"
