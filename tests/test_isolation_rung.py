"""#700 slice 1: the isolation rung is proven by canaries through nono.

End to end: the real ``ks doctor --measure`` in its own process group
under a fuse, on a real git repository after the real ``ks init``, with
the base gates set to ``true`` so the reading is the isolation row's.
The row proves two zones, setup and test, by running one canary process
through ``nono wrap`` and the same process unsandboxed as its control.

Which nono runs. ``KSTRL_NONO`` when it is set, else ``nono`` on PATH;
the tests that need the real sandbox skip unless that binary reports
0.79 or later, because 0.16 (the Homebrew build on the machine this was
written on) grants the user temporary directory and the canaries
measure it escaping. The tests built on a fake ``nono`` need only
macOS, and the Linux test runs only off macOS: until measurement M2,
Linux CI runs that one test and nothing else here.

DNS. nono 0.79 cannot deny DNS in the test zone (gap G1), so the test
zone is refused naming ``dns``. ``test_the_test_zone_is_proven`` pins
that as a strict xfail: a nono release that fixes G1 XPASSes it and the
run fails until somebody reads it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.executables import write_executable
from tests.helpers.procs import wait_for_pid_to_die
from tests.test_red_base_preflight import _doctor_json, _repo

#: The base gates, all `true`: these tests read the isolation row.
GATES = {
    "KSTRL_VERIFY_TEST_CMD": "true",
    "KSTRL_VERIFY_TYPECHECK_CMD": "true",
    "KSTRL_VERIFY_LINT_CMD": "true",
}

#: Read at import, before the autouse fixtures run.
NONO = os.environ.get("KSTRL_NONO", "").strip() or shutil.which("nono") or ""


def _nono_version(path: str) -> tuple[int, ...]:
    if not path:
        return ()
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(part) for part in found.groups()) if found else ()


on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="the rung is macOS-only until M2")
needs_nono = pytest.mark.skipif(
    sys.platform != "darwin" or _nono_version(NONO) < (0, 79, 0),
    reason=f"needs macOS and nono 0.79 or later; found {NONO or 'none'} "
    f"{'.'.join(map(str, _nono_version(NONO))) or ''}; set KSTRL_NONO",
)

#: Every canary each zone must contain, and every positive control.
CONTAINED = ("write_outside", "write_tmp", "write_state_parent", "read_planted", "list_ssh")
POSITIVE = ("scratch_write", "loopback", "env_reaches", "exit_127")


def _isolation(tmp_path: Path, env: dict[str, str]) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Run `ks doctor --measure --json`; return the exit code, the
    isolation row and the isolation reading."""
    root = _repo(tmp_path, {})
    code, document = _doctor_json(root, {**GATES, **env})
    (row,) = [row for row in document["checks"] if row["name"] == "isolation"]
    return code, row, document["isolation"]


def _fake_nono(tmp_path: Path, body: str) -> dict[str, str]:
    """A `nono` first on PATH that logs each call's first argument and
    the three variables kstrl must set on it to ``nono-calls.log``,
    answers --version, and runs ``body`` for
    `nono wrap -s -p <policy> -- <argv>`; returns the env for it."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "nono-calls.log"
    write_executable(
        bin_dir / "nono",
        "#!/bin/sh\n"
        f'echo "$1 ${{NONO_NO_UPDATE_CHECK:-unset}} ${{TMPDIR:-unset}} '
        f'${{XDG_CONFIG_HOME:-unset}}" >> "{log}"\n'
        'if [ "$1" = "--version" ]; then echo "nono 0.0.0-fake"; exit 0; fi\n' + body,
    )
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


@needs_nono
def test_doctor_measure_proves_the_setup_zone_and_names_dns_in_the_test_zone(
    tmp_path: Path,
) -> None:
    code, row, reading = _isolation(tmp_path, {"KSTRL_NONO": NONO})

    assert code == 0, row
    assert row["status"] == "warn", row
    setup, test = reading["setup"], reading["test"]
    assert setup["refusal"] == "", setup
    assert setup["label"].endswith("setup zone: writes confined, egress open"), setup
    for name in CONTAINED:
        assert setup["canaries"][name].startswith("contained: errno "), (name, setup)
        assert test["canaries"][name].startswith("contained: errno "), (name, test)
    assert test["canaries"]["egress"] == "contained: errno 1", test
    for name in POSITIVE:
        assert (setup["canaries"][name], test["canaries"][name]) == ("ok", "ok"), name
    # G1: DNS is not contained, and it is the only thing that refuses the zone.
    assert not test["canaries"]["dns"].startswith("contained"), test
    assert test["refusal"].startswith("refused: dns ("), test
    assert test["label"] == "none: ran on the host"
    # The policy is outside the repository and is the file the digest names.
    for zone in (setup, test):
        policy = Path(zone["policy_path"])
        assert not policy.is_relative_to(tmp_path / "proj"), policy
        assert policy.is_relative_to(Path(os.environ["XDG_STATE_HOME"]).resolve()), policy
        assert hashlib.sha256(policy.read_bytes()).hexdigest() == zone["policy_sha256"]
        assert policy.name == f"{zone['policy_sha256']}.json"
        assert zone["backend_version"].startswith("nono "), zone
    assert json.loads(Path(test["policy_path"]).read_text(encoding="utf-8"))["network"] == {
        "block": True,
        "open_port": [0],
    }


@needs_nono
@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="G1: nono 0.79 cannot deny DNS in the test zone"
)
def test_the_test_zone_is_proven(tmp_path: Path) -> None:
    _code, _row, reading = _isolation(tmp_path, {"KSTRL_NONO": NONO})

    assert reading["test"]["refusal"] == "", reading["test"]


@on_macos
def test_a_nono_that_ignores_the_policy_is_refused(tmp_path: Path) -> None:
    """The fake drops `wrap -s -p <policy> --` and runs the command with
    no sandbox, so the binary is present and answers --version."""
    env = _fake_nono(tmp_path, 'shift 5\nexec "$@"\n')

    code, row, reading = _isolation(tmp_path, env)

    assert code == 0, row
    assert row["status"] == "warn", row
    for zone in ("setup", "test"):
        refusal = reading[zone]["refusal"]
        assert "write_outside (escaped)" in refusal, refusal
        assert "read_planted (escaped)" in refusal, refusal
        assert reading[zone]["label"] == "none: ran on the host"


@on_macos
def test_a_canary_that_hangs_is_a_timeout_and_never_contained(tmp_path: Path) -> None:
    """The fake hangs whenever it is handed the canary program and writes
    its pid first, so the test can name the process the bound must kill.
    Each zone waits out the 10 s bound once."""
    pids = tmp_path / "hung.pids"
    env = _fake_nono(
        tmp_path,
        f'for arg; do [ "$arg" = "-c" ] && echo $$ >> "{pids}" && exec sleep 60; done\n'
        'shift 5\nexec "$@"\n',
    )

    _code, row, reading = _isolation(tmp_path, env)

    assert row["status"] == "warn", row
    for zone in ("setup", "test"):
        canaries = reading[zone]["canaries"]
        assert canaries["write_outside"] == "timeout", canaries
        assert "write_outside (timeout)" in reading[zone]["refusal"]
    hung = [int(line) for line in pids.read_text(encoding="utf-8").split()]
    assert len(hung) == 2, hung
    assert all(wait_for_pid_to_die(pid) for pid in hung), hung


@on_macos
def test_a_nono_that_rejects_the_policy_is_refused_and_never_phones_home(tmp_path: Path) -> None:
    """The fake answers --version and exits 1 for everything else, as nono
    does when it rejects a policy. A canary that printed no report never
    ran, so it was not contained. Every nono spawn, --version included,
    carries NONO_NO_UPDATE_CHECK and a TMPDIR inside the scratch
    directory, and every `wrap` an XDG_CONFIG_HOME there too, so nono
    neither checks for updates nor writes the operator's directories."""
    env = _fake_nono(tmp_path, 'echo "nono: policy rejected" >&2\nexit 1\n')

    code, row, reading = _isolation(tmp_path, env)

    assert code == 0, row
    assert row["status"] == "warn", row
    for zone in ("setup", "test"):
        canaries = reading[zone]["canaries"]
        assert canaries["write_outside"] == "no report (exit 1)", canaries
        assert canaries["exit_127"] == "exit 1", canaries
        assert "write_outside (no report (exit 1))" in reading[zone]["refusal"]
        assert reading[zone]["label"] == "none: ran on the host"
    calls = [
        line.split(" ")
        for line in (tmp_path / "nono-calls.log").read_text(encoding="utf-8").splitlines()
    ]
    # Per zone: --version, the canary, the missing command.
    assert [call[0] for call in calls] == ["--version", "wrap", "wrap"] * 2, calls
    for first, no_update_check, tmpdir, config_home in calls:
        assert no_update_check == "1", calls
        assert "/kstrl-rung-" in tmpdir, calls
        assert first != "wrap" or "/kstrl-rung-" in config_home, calls


@on_macos
def test_without_nono_the_row_refuses_and_runs_nothing(tmp_path: Path) -> None:
    path = os.pathsep.join(
        entry
        for entry in os.environ["PATH"].split(os.pathsep)
        if not (Path(entry) / "nono").exists()
    )

    code, row, reading = _isolation(tmp_path, {"PATH": path})

    assert code == 0, row
    assert row["status"] == "warn", row
    for zone in ("setup", "test"):
        assert reading[zone]["refusal"] == (
            "refused: nono not found (set KSTRL_NONO or put nono on PATH)"
        ), reading[zone]
        assert (reading[zone]["canaries"], reading[zone]["policy_path"]) == ({}, "")


@pytest.mark.skipif(sys.platform == "darwin", reason="the refusal off macOS")
def test_off_macos_the_row_refuses_with_its_reason(tmp_path: Path) -> None:
    code, row, reading = _isolation(tmp_path, {})

    assert code == 0, row
    assert row["status"] == "warn", row
    for zone in ("setup", "test"):
        assert reading[zone]["refusal"] == (
            f"refused: no process rung implemented on {sys.platform} "
            "(nono cannot express a localhost-only test zone; M2)"
        ), reading[zone]
        assert reading[zone]["canaries"] == {}
