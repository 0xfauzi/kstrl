"""#700 slice 2: under a ``[stack]`` every verification command runs in a proven rung.

``ks factory`` proves both zones once per run, before the base gates, in
the policy its commands then run in: the setup in the setup zone (egress
open, writes confined), every check in the test zone (egress blocked,
localhost open). Below a proven rung it refuses with exit 2 before any
engineer runs; there is no opt-in to run a stack's commands on the host.

End to end: the real ``ks factory`` and ``ks doctor --measure`` as
subprocesses on a real git repository after the real ``ks init``, with a
stub engineer that logs each call (``tests/test_stack_e2e.py``). The
tests that need the real sandbox skip unless nono 0.79 or later runs on
macOS; the ones built on a fake ``nono`` need only macOS; one runs only
off macOS, where the run refuses with its reason (gap G7).

What a check does is observed on the host: a file it wrote or did not
write, and the row the base-gates record keeps for it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.executables import write_executable
from tests.test_isolation_rung import needs_nono, on_macos
from tests.test_stack_e2e import PY, _commit, _factory, _measure, _record, _repo, _stack

#: Exits 0 only when a connect to TEST-NET-1 by IP is refused with EPERM,
#: the rung's answer (DNS is open in the test zone, gap G1, so the probe
#: names no host), and a loopback bind and connect work.
NET_PROBE = """\
import errno, socket, sys
try:
    socket.create_connection(("192.0.2.1", 80), timeout=2).close()
    sys.exit(10)
except OSError as exc:
    if exc.errno != errno.EPERM:
        sys.exit(11)
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
socket.create_connection(server.getsockname(), timeout=2).close()
"""

#: Exits 4 when it can write the file its argument names in $HOME (the
#: setup zone confines writes, so only a setup on the host can), and 3
#: when a connect to TEST-NET-1 is refused with EPERM: egress is blocked,
#: which the setup zone must not do. A timeout or an unreachable network is
#: egress left open, so it exits 0.
SETUP_PROBE = """\
import errno, os, socket, sys
try:
    with open(os.path.join(os.environ["HOME"], sys.argv[1]), "w") as handle:
        handle.write("x")
    sys.exit(4)
except OSError:
    pass
try:
    socket.create_connection(("192.0.2.1", 80), timeout=1).close()
except OSError as exc:
    sys.exit(3 if exc.errno == errno.EPERM else 0)
"""

REFUSAL = "Refusing to run: the isolation rung is not proven"


def _isolation_record(root: Path) -> dict[str, Any]:
    (path,) = sorted((root / ".kstrl" / "runs").glob("*/isolation.json"))
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _policy(zone: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(Path(zone["policy_path"]).read_text(encoding="utf-8"))
    return policy


def _verification_isolation(root: Path) -> list[str]:
    (events,) = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    return [
        line["data"]["isolation"]
        for line in map(json.loads, events.read_text(encoding="utf-8").splitlines())
        if line["event"] == "verification_result"
    ]


def _ignoring_nono(tmp_path: Path) -> dict[str, str]:
    """A `nono` first on PATH that answers --version and runs every
    `wrap -s -p <policy> -- <argv>` with no sandbox, so each canary escapes."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "nono",
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "nono 0.0.0-fake"; exit 0; fi\n'
        'shift 5\nexec "$@"\n',
    )
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "KSTRL_NONO": ""}


@needs_nono
def test_a_check_runs_in_the_test_zone_with_only_the_declared_paths(tmp_path: Path) -> None:
    """Each check is one thing a command in the test zone may or may not do.
    Writing $HOME, writing the checkout outside kstrl's worktree trees,
    reaching a non-loopback address by IP, and writing or reading a path
    the stack did not declare all fail; loopback, a declared
    writable path, a declared readable path and a declared variable that
    nono strips all work. The base refuses on the failing rows only.

    The two read targets sit under the home directory, removed afterwards:
    nono's default groups grant reads of all of /private, the system
    temporary directories that hold ``tmp_path`` included (measured), so
    a path there is readable declared or not."""
    token = secrets.token_hex(6)
    escape = Path.home() / f"kstrl-escape-{token}"
    home_reads = Path.home() / f".kstrl-reads-{token}"
    declared, undeclared = tmp_path / "declared-write", tmp_path / "undeclared-write"
    readable, unreadable = home_reads / "declared", home_reads / "undeclared"
    for directory in (declared, undeclared, readable, unreadable):
        directory.mkdir(parents=True)
    for directory in (readable, unreadable):
        (directory / "data.txt").write_text("data\n", encoding="utf-8")
    checks = {
        "escape": f'echo x > "$HOME/{escape.name}"',
        "checkout_write": f'echo x > "{tmp_path / "proj" / "kstrl-checkout-write"}"',
        "network": f"{PY} -I -S net_probe.py",
        "declared_write": f'echo x > "{declared}/made"',
        "undeclared_write": f'echo x > "{undeclared}/made"',
        "declared_read": f'cat "{readable}/data.txt"',
        "undeclared_read": f'cat "{unreadable}/data.txt"',
        "stripped_env": 'test "$CDPATH" = /kstrl-declared',
    }
    stack = _stack(
        checks,
        env=["CDPATH"],
        rung={"writable": [str(declared)], "readable": [f"~/{home_reads.name}/declared"]},
    )
    root = _repo(tmp_path, stack)
    _commit(root, "net_probe.py", NET_PROBE)

    try:
        run = _factory(tmp_path, root, env={"CDPATH": "/kstrl-declared"})
        escaped = escape.exists()
    finally:
        escape.unlink(missing_ok=True)
        shutil.rmtree(home_reads)

    assert run.code == 2, run.out
    assert run.calls == 0, run.out
    assert not escaped, "a check wrote the operator's home directory"
    record = _record(root)
    passed = {row["name"]: row["passed"] for row in record["checks"]}
    assert passed == {
        "stack:escape": False,
        "stack:checkout_write": False,
        "stack:network": True,
        "stack:declared_write": True,
        "stack:undeclared_write": False,
        "stack:declared_read": True,
        "stack:undeclared_read": False,
        "stack:stripped_env": True,
    }, run.out
    assert (declared / "made").exists()
    assert not (undeclared / "made").exists()
    assert not (root / "kstrl-checkout-write").exists()
    test_zone = _isolation_record(root)["test"]
    assert record["isolation"] == test_zone["label"]
    assert "test zone: writes confined, egress blocked" in record["isolation"], record


@needs_nono
def test_the_setup_runs_in_the_setup_zone_and_every_result_names_the_rung(
    tmp_path: Path,
) -> None:
    """The setup refuses itself when it can write $HOME (exit 4, so it did
    not run in a rung) or when egress is blocked (exit 3, so it ran in the
    test zone), and the check passes only when egress is blocked, so a run
    that reaches its engineer and passes Phase 3 ran each in its own zone:
    the setup in the base worktree, the component's and the contract
    worktree, the check on the base, in Phase 1 and in Phase 3. Both
    records name the test zone's rung, and the rungs' scratch is gone once
    the run ends."""
    escape = Path.home() / f"kstrl-escape-setup-{secrets.token_hex(6)}"
    stack = _stack(
        {"network": f"{PY} -I -S net_probe.py"},
        setup=f"{PY} -I -S setup_probe.py {escape.name}",
    )
    root = _repo(tmp_path, stack)
    _commit(root, "net_probe.py", NET_PROBE)
    _commit(root, "setup_probe.py", SETUP_PROBE)

    try:
        run = _factory(tmp_path, root, contract="final")
        escaped = escape.exists()
    finally:
        escape.unlink(missing_ok=True)

    assert not escaped, "a setup wrote the operator's home directory"
    assert REFUSAL not in run.out, run.out
    assert run.calls == 1, run.out
    assert "contract tests passed" in run.out, run.out
    record = _record(root)
    assert (record["refused"], record["setupError"]) == (False, ""), record
    rungs = _isolation_record(root)
    setup, test = rungs["setup"], rungs["test"]
    assert (setup["refusal"], test["refusal"]) == ("", ""), rungs
    assert setup["label"].endswith("setup zone: writes confined, egress open"), setup
    assert record["isolation"] == test["label"]
    assert _verification_isolation(root) == [test["label"]], run.out
    for zone in (setup, test):
        assert not Path(zone["scratch"]).exists(), zone
        assert not Path(zone["policy_path"]).is_relative_to(root), zone
    assert _policy(setup)["network"] == {"block": False}
    assert _policy(test)["network"] == {"block": True, "open_port": [0]}


@on_macos
def test_a_rung_that_lets_a_canary_out_refuses_before_the_base_and_the_engineer(
    tmp_path: Path,
) -> None:
    """A nono that ignores its policy lets every canary out. The run exits 2
    naming the canary, before the base gates run and before any engineer,
    and the record it leaves says why."""
    root = _repo(tmp_path, _stack({"tests": "true"}))

    run = _factory(tmp_path, root, env=_ignoring_nono(tmp_path))

    assert run.code == 2, run.out
    assert REFUSAL in run.out, run.out
    assert "write_outside (escaped)" in run.out, run.out
    assert run.calls == 0, run.out
    assert sorted((root / ".kstrl" / "runs").glob("*/base-gates.json")) == [], run.out
    rungs = _isolation_record(root)
    assert "write_outside (escaped)" in rungs["test"]["refusal"], rungs
    assert not Path(rungs["test"]["scratch"]).exists(), rungs


@on_macos
def test_browser_rules_reach_the_test_zone_only_when_the_stack_declares_a_browser(
    tmp_path: Path,
) -> None:
    """Headless Chromium needs two raw Seatbelt allow rules (gap G3). They
    widen the boundary, so only a stack that says ``browser = true`` gets
    them, and only in the test zone. Read off the policy files the run
    recorded; the fake nono refuses the rung, which is after both are
    written."""
    plain = _repo(tmp_path / "plain", _stack({"tests": "true"}))
    browser = _repo(tmp_path / "browser", _stack({"tests": "true"}, rung={"browser": True}))

    _factory(tmp_path / "plain", plain, env=_ignoring_nono(tmp_path / "plain"))
    _factory(tmp_path / "browser", browser, env=_ignoring_nono(tmp_path / "browser"))

    without, with_browser = _isolation_record(plain), _isolation_record(browser)
    for zone in ("setup", "test"):
        assert "unsafe_macos_seatbelt_rules" not in _policy(without[zone]), without[zone]
    assert "unsafe_macos_seatbelt_rules" not in _policy(with_browser["setup"])
    assert _policy(with_browser["test"])["unsafe_macos_seatbelt_rules"] == [
        "(allow mach-register)",
        "(allow iokit-open)",
    ]


@pytest.mark.skipif(sys.platform == "darwin", reason="the refusal off macOS")
def test_off_macos_a_stack_run_refuses_with_its_reason(tmp_path: Path) -> None:
    """No rung is implemented off macOS until measurement M2 (gap G7), so a
    run under a stack refuses there: its commands never run on the host."""
    root = _repo(tmp_path, _stack({"tests": "true"}))

    run = _factory(tmp_path, root)

    assert run.code == 2, run.out
    assert REFUSAL in run.out, run.out
    assert f"no process rung implemented on {sys.platform}" in run.out, run.out
    assert run.calls == 0, run.out


def test_editing_writable_readable_or_browser_changes_the_stack_digest(tmp_path: Path) -> None:
    """The rung keys are part of what the stack is: each edit is a new
    digest, and an empty key is the same stack as an absent one."""
    tables = {
        "base": _stack({"tests": "true"}),
        "empty": _stack({"tests": "true"}, rung={"writable": [], "readable": []}),
        "writable": _stack({"tests": "true"}, rung={"writable": ["~/.cache/demo"]}),
        "readable": _stack({"tests": "true"}, rung={"readable": ["docs"]}),
        "browser": _stack({"tests": "true"}, rung={"browser": True}),
    }
    digests = {}
    for name, table in tables.items():
        root = _repo(tmp_path / name, table)
        _code, document = _measure(root)
        digests[name] = document["base_gates"]["stackDigest"]

    assert digests["empty"] == digests["base"], digests
    edited = [digests[name] for name in ("base", "writable", "readable", "browser")]
    assert len(set(edited)) == 4, digests
