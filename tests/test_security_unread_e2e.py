"""A security reply kstrl cannot parse is read once more, never reported passed.

A user run's security reviewer left out one closing brace in a reply that
held two low findings: the phase printed "Security review passed" and the
findings were shown nowhere. A reply that did not parse and states no
finding at or above the fail threshold now gets the reviewer once more, as
Phase 2 does; one that states a finding at or above it is not re-asked, so
a re-ask can never drop a blocking finding; and a reply still unread is
said to be unread.

End to end: the real ``ks factory`` as a subprocess on a real git
repository after the real ``ks init``, with a confirmed ``[stack]``, a stub
engineer that commits, and a stub security reviewer that reports the diff
it measured and counts its calls (the ``tests/test_stack_e2e.py`` harness).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.helpers.executables import write_executable
from tests.test_isolation_rung import runs_a_stack
from tests.test_stack_e2e import _factory, _repo, _stack

ENGINEER = "date +%s%N > made.txt && git add -A && git commit -q -m made >/dev/null 2>&1"


def _finding(severity: str) -> dict[str, str]:
    return {
        "category": "race_condition",
        "severity": severity,
        "location": "made.txt:1",
        "explanation": f"a {severity} finding the stub reports",
        "suggestion": "none",
    }


def _security(tmp_path: Path, severity: str, broken: list[bool]) -> Path:
    """A security reviewer that reports one finding of ``severity``; its Nth
    reply lacks its last closing brace when ``broken[N-1]`` (the last entry
    again after they run out). Logs one line per call."""
    calls = tmp_path / "security.calls"
    return write_executable(
        tmp_path / "security.py",
        f"""#!{sys.executable}
import json, subprocess, sys
sys.stdin.read()
with open({str(calls)!r}, "a") as log:
    log.write("call\\n")
n = len(open({str(calls)!r}).read().splitlines())
broken = {broken!r}
out = subprocess.run(["git", "diff", "--numstat", "main...HEAD"], capture_output=True, text=True)
rows = [[int(a), int(b), p] for a, b, p in (line.split("\\t") for line in out.stdout.splitlines())]
text = json.dumps({{
    "observedDiffstat": {{"rows": rows}},
    "findings": [{json.dumps(_finding(severity))}],
    "exhaustively_searched": True,
    "overallNotes": "",
}})
print(text[:-1] if broken[min(n, len(broken)) - 1] else text)
""",
    )


def _calls(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


@runs_a_stack
def test_an_unparsed_reply_with_no_blocking_finding_is_read_once_more(tmp_path: Path) -> None:
    root = _repo(tmp_path, _stack({"tests": "true"}))
    security = _security(tmp_path, "low", [True, False])

    run = _factory(
        tmp_path,
        root,
        *("--security-mode", "advisory", "--security-agent-cmd", str(security)),
        engineer=ENGINEER,
    )

    assert "Security reply did not parse; reading it once more" in run.out, run.out
    assert _calls(tmp_path / "security.calls") == 2, run.out
    assert "a low finding the stub reports" in run.out, run.out
    assert run.code == 0, run.out


@runs_a_stack
def test_a_reply_still_unparsed_is_said_unread_not_passed(tmp_path: Path) -> None:
    root = _repo(tmp_path, _stack({"tests": "true"}))
    security = _security(tmp_path, "low", [True])

    run = _factory(
        tmp_path,
        root,
        *("--security-mode", "advisory", "--security-agent-cmd", str(security)),
        engineer=ENGINEER,
    )

    assert _calls(tmp_path / "security.calls") == 2, run.out
    assert "Security review not read" in run.out, run.out
    assert "Security review passed" not in run.out, run.out


@runs_a_stack
def test_an_unparsed_reply_that_states_a_blocking_finding_is_not_asked_again(
    tmp_path: Path,
) -> None:
    """The re-ask replaces the reply, so it may only replace one that holds
    nothing it could drop: a high finding in hard mode blocks as unread."""
    root = _repo(tmp_path, _stack({"tests": "true"}))
    security = _security(tmp_path, "high", [True, False])

    run = _factory(
        tmp_path,
        root,
        *("--security-mode", "hard", "--security-agent-cmd", str(security)),
        engineer=ENGINEER,
    )

    assert "reading it once more" not in run.out, run.out
    assert _calls(tmp_path / "security.calls") == 1, run.out
    assert "Security review FAILED" in run.out, run.out
    assert run.code != 0, run.out
