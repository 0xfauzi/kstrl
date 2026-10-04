"""The isolation rung, proven by canaries through nono (#700 slice 1).

kstrl writes no sandbox profile of its own. It writes a small nono
policy (paths and one network switch) under the repository's control
directory, runs one canary process through ``nono wrap`` and the same
process beside it with no sandbox as its control, and decides the rung
from what the canaries could do. The rung is never chosen from the
binary being present or from its version: the #700 evaluation measured
nono's manifest interface silently ignoring two documented fields (gap
G2), so what nono says it enforces is a hint, and only a canary is
evidence.

Record only. Nothing runs inside a rung yet and nothing is refused
because of one: ``ks doctor --measure`` reports the reading, and the
verification records carry :data:`HOST_LABEL`, which stays the truth
until a later slice runs commands inside a proven rung.

The verdict rules. A canary is contained only when its operation failed
with an errno; the egress canary only with EPERM, because a timeout or
"no route" is the network not answering rather than the rung refusing.
A timeout is never contained. A canary whose control did not succeed is
uninformative, because a sandbox cannot be credited with stopping an
operation that fails anyway. Any canary that is not contained, and any
positive control that does not pass, refuses the zone and is named.

Two measured limits, both recorded rather than probed:

- DNS resolves inside the test zone whatever the policy says (G1), so
  the ``dns`` canary escapes and the test zone is refused naming it.
- The test zone reaches this host's own non-loopback addresses (G5), a
  Seatbelt property, so the test-zone label says "this host only, not
  loopback only". A probe would need a listener on a non-loopback
  interface, which the operating system's firewall can stop to ask the
  operator about.

Every system but macOS is refused: on Linux nono's Landlock rules filter
TCP by port and not by host, so it cannot express a localhost-only test
zone (G7, measurement M2).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_text
from kstrl.jsonread import read_json
from kstrl.statedir import control_dir, xdg_state_home
from kstrl.verify import ChildOutputDecodeError, run_scrubbed

#: What a result records when its command ran with no rung around it.
HOST_LABEL = "none: ran on the host"

#: The environment variable naming the nono binary; PATH is searched
#: when it is unset.
NONO_ENV = "KSTRL_NONO"

SETUP_ZONE = "setup"
TEST_ZONE = "test"

#: kstrl's own interpreter runs the canary. This is kstrl's runtime, not
#: an assumption about the target project.
_INTERPRETER = sys.executable

#: The program in front of every command inside the rung. nono turns a
#: missing command into exit 1 and strips the variables a shell or an
#: interpreter reads at startup; ``env`` in front restores exit 127 and
#: 126 and sets those variables after nono has run (evaluation 4.9).
ENV_PROGRAM = "/usr/bin/env"

#: nono's default groups minus the write grant to the system temporary
#: directories, the user tool directories, and the startup-command block
#: (the command is always ``env``, so it would only ever block ``rm``).
EXCLUDED_GROUPS = (
    "system_write_macos",
    "user_tools",
    "dangerous_commands",
    "dangerous_commands_macos",
)

#: Seconds one canary process may run before it is killed. Measured at
#: well under a second inside nono; the rest is margin for a loaded host.
CANARY_PROCESS_SECONDS = 10.0
#: Seconds a network canary waits. The egress control connects to an
#: address nothing answers on, so this is what the control costs.
CONNECT_SECONDS = 1.0
#: TEST-NET-1 (RFC 5737): no host answers it, so the unsandboxed control
#: times out instead of connecting, and only the rung can return EPERM.
EGRESS_TARGET = "192.0.2.1:80"
#: A wildcard DNS zone: every name under it resolves when the query
#: reaches a public resolver, so a random label is never in a cache.
DNS_ZONE = "10.9.8.7.nip.io"
#: A variable nono strips from the command's environment. The canary is
#: handed it through ``env`` and reports whether it arrived.
ENV_CANARY_NAME = "CDPATH"
#: The exit a missing command must give inside the rung.
MISSING_COMMAND_EXIT = 127

#: What the rung must still allow: a check that cannot do these cannot run.
POSITIVE_CONTROLS = ("scratch_write", "loopback", "env_reaches", "exit_127")

#: The canary program. It takes ``name kind target`` triples on argv,
#: attempts each operation once, and prints one JSON object mapping each
#: name to "ok", "timeout", "errno N" or "error <type>". It decides
#: nothing: the verdict is :func:`_verdict`'s, made against the control.
CANARY_SOURCE = """
import json, os, socket, sys

SECONDS = float(sys.argv[1])

def attempt(kind, target):
    if kind == "write":
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("canary")
    elif kind == "read":
        with open(target, encoding="utf-8") as handle:
            handle.read()
    elif kind == "list":
        os.listdir(target)
    elif kind == "connect":
        host, port = target.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=SECONDS):
            pass
    elif kind == "resolve":
        socket.getaddrinfo(target, 80)
    elif kind == "loopback":
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=SECONDS) as client:
                peer, _ = server.accept()
                with peer:
                    client.sendall(b"canary")
                    if peer.recv(6) != b"canary":
                        raise ValueError("echo")
    elif kind == "env":
        name, _, value = target.partition("=")
        if os.environ.get(name) != value:
            raise LookupError(name)
    else:
        raise ValueError(kind)

def outcome(kind, target):
    try:
        attempt(kind, target)
    except TimeoutError:
        return "timeout"
    except OSError as exc:
        return f"errno {exc.errno}" if exc.errno else f"error {type(exc).__name__}"
    except Exception as exc:
        return f"error {type(exc).__name__}"
    return "ok"

args = sys.argv[2:]
print(json.dumps({args[i]: outcome(args[i + 1], args[i + 2]) for i in range(0, len(args), 3)}))
"""


@dataclass(frozen=True)
class ProvenRung:
    """One zone's reading. ``refusal`` is empty only when every canary
    was contained and every positive control passed."""

    zone: str
    backend: str
    backend_version: str
    policy_path: str
    policy_sha256: str
    canaries: Mapping[str, str]
    seconds: float
    refusal: str
    label: str


@dataclass(frozen=True)
class _Ran:
    """One bounded child: its exit status (None when it gave none) and
    stdout, or why there is no status."""

    code: int | None
    stdout: str
    why: str


def _real(paths: Sequence[Path | str]) -> list[str]:
    return list(dict.fromkeys(os.path.realpath(path) for path in paths))


def nono_policy(
    zone: str,
    writable: Sequence[Path | str],
    readable: Sequence[Path | str],
    deny_read: Sequence[Path | str],
) -> dict[str, Any]:
    """The nono profile for ``zone``: the setup zone leaves egress open,
    the test zone blocks it and allows localhost on any port. No DNS deny
    is ever written: denying the resolver socket blocks DNS in the setup
    zone and does not block it in the test zone (evaluation 4.3)."""
    network: dict[str, Any] = {"block": False}
    if zone == TEST_ZONE:
        network = {"block": True, "open_port": [0]}
    return {
        "meta": {"name": f"kstrl-{zone}-zone"},
        "groups": {"exclude": list(EXCLUDED_GROUPS)},
        "filesystem": {
            "allow": _real(writable),
            "read": _real(readable),
            "deny": _real(deny_read),
            "allow_file": ["/dev/null"],
        },
        "network": network,
        "workdir": {"access": "none"},
    }


def write_policy(root: Path, policy: Mapping[str, Any]) -> tuple[Path, str]:
    """Write ``policy`` under the control directory, outside every tree an
    agent can write, named by the SHA-256 of its exact bytes."""
    text = json.dumps(policy, indent=2) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = control_dir(root) / "rung" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return path, digest


def _nono_argv(
    nono: str, policy_path: Path, scratch: Path, argv: Sequence[str], assignments: Sequence[str]
) -> list[str]:
    """The one place a nono command line is built. nono's own state goes
    to ``scratch`` (a missing ``XDG_CONFIG_HOME`` makes it fall back to
    the operator's ``~/.config``, measured), and no update check runs."""
    return [
        ENV_PROGRAM,
        "NONO_NO_UPDATE_CHECK=1",
        f"TMPDIR={scratch / 'nono-tmp'}/",
        f"XDG_CONFIG_HOME={scratch / 'nono-config'}",
        nono,
        "wrap",
        "-s",
        "-p",
        str(policy_path),
        "--",
        ENV_PROGRAM,
        *assignments,
        *argv,
    ]


def _run_bounded(argv: Sequence[str], cwd: Path) -> _Ran:
    try:
        done = run_scrubbed(list(argv), cwd=cwd, timeout=CANARY_PROCESS_SECONDS, term_grace=1.0)
    except subprocess.TimeoutExpired:
        return _Ran(None, "", "timeout")
    except ChildOutputDecodeError:
        return _Ran(None, "", "undecodable output")
    except OSError as exc:
        return _Ran(None, "", f"could not start: {exc}")
    return _Ran(done.returncode, done.stdout, "")


def _outcomes(ran: _Ran, names: Sequence[str]) -> dict[str, str]:
    """Each canary's raw outcome, or the process's failure for all of them."""
    failure = ran.why or f"no report (exit {ran.code})"
    lines = ran.stdout.strip().splitlines()
    try:
        report = read_json(lines[-1]) if lines else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        return dict.fromkeys(names, failure)
    return {name: str(report.get(name, failure)) for name in names}


def _contained(name: str, outcome: str) -> bool:
    if name == "egress":
        return outcome == f"errno {errno.EPERM}"
    return outcome.startswith("errno ")


def _control_informative(name: str, control: str) -> bool:
    if name == "egress":
        return control in ("ok", "timeout") or (
            control.startswith("errno ") and not _contained(name, control)
        )
    return control == "ok"


def _verdict(name: str, sandboxed: str, control: str) -> str:
    if not _control_informative(name, control):
        return f"uninformative: control {control}"
    if sandboxed == "timeout":
        return "timeout"
    if _contained(name, sandboxed):
        return f"contained: {sandboxed}"
    return "escaped" if sandboxed == "ok" else sandboxed


@dataclass(frozen=True)
class _Layout:
    """Where one zone's canaries point. ``zone`` is the only granted
    directory; ``planted`` sits inside it and is denied; ``outside`` is a
    sibling no grant covers."""

    zone: Path
    outside: Path
    planted: Path
    token: str

    @classmethod
    def under(cls, scratch: Path) -> _Layout:
        layout = cls(
            scratch / "zone",
            scratch / "outside",
            scratch / "zone" / "planted",
            secrets.token_hex(6),
        )
        for directory in (
            layout.planted,
            layout.outside,
            layout.zone / "nono-tmp",
            layout.zone / "nono-config",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (layout.planted / "canary").write_text("canary", encoding="utf-8")
        return layout


def _plan(zone: str, layout: _Layout) -> list[tuple[str, str, str]]:
    """``(name, kind, target)`` for every canary and positive control the
    canary process runs, in report order."""
    plan = [
        ("write_outside", "write", str(layout.outside / "canary")),
        ("write_tmp", "write", f"/private/tmp/kstrl-canary-{layout.token}"),
        ("write_state_parent", "write", str(xdg_state_home() / f"kstrl-canary-{layout.token}")),
        ("read_planted", "read", str(layout.planted / "canary")),
        ("list_ssh", "list", str(Path.home() / ".ssh")),
    ]
    if zone == TEST_ZONE:
        plan += [
            ("egress", "connect", EGRESS_TARGET),
            ("dns", "resolve", f"k{layout.token}.{DNS_ZONE}"),
        ]
    return plan + [
        ("scratch_write", "write", str(layout.zone / f"canary-{layout.token}")),
        ("loopback", "loopback", ""),
        ("env_reaches", "env", f"{ENV_CANARY_NAME}={layout.token}"),
    ]


def _run_canaries(nono: str, policy_path: Path, layout: _Layout, zone: str) -> dict[str, str]:
    plan = _plan(zone, layout)
    names = [name for name, _kind, _target in plan]
    canary = [_INTERPRETER, "-I", "-S", "-c", CANARY_SOURCE, str(CONNECT_SECONDS)]
    canary += [part for triple in plan for part in triple]
    assignments = [f"{ENV_CANARY_NAME}={layout.token}"]
    try:
        sandboxed = _outcomes(
            _run_bounded(
                _nono_argv(nono, policy_path, layout.zone, canary, assignments), layout.zone
            ),
            names,
        )
        control = _outcomes(_run_bounded([ENV_PROGRAM, *assignments, *canary], layout.zone), names)
    finally:
        for _name, kind, target in plan:
            if kind == "write":
                Path(target).unlink(missing_ok=True)
    verdicts = {
        name: sandboxed[name]
        if name in POSITIVE_CONTROLS
        else _verdict(name, sandboxed[name], control[name])
        for name in names
    }
    missing = _run_bounded(
        _nono_argv(nono, policy_path, layout.zone, [f"kstrl-canary-missing-{layout.token}"], []),
        layout.zone,
    )
    verdicts["exit_127"] = (
        "ok" if missing.code == MISSING_COMMAND_EXIT else (missing.why or f"exit {missing.code}")
    )
    return verdicts


def _refusal(canaries: Mapping[str, str]) -> str:
    failed = [
        f"{name} ({verdict})"
        for name, verdict in canaries.items()
        if (verdict != "ok" if name in POSITIVE_CONTROLS else not verdict.startswith("contained"))
    ]
    return f"refused: {', '.join(failed)}" if failed else ""


def _locate_nono(scratch: Path) -> tuple[str, str, str]:
    """``(binary, version, refusal)``. The version is recorded, never
    consulted: the canaries decide. ``--version`` runs in ``scratch``
    with no update check, like every other nono spawn."""
    if sys.platform != "darwin":
        return (
            "",
            "",
            (
                f"refused: no process rung implemented on {sys.platform} "
                "(nono cannot express a localhost-only test zone; M2)"
            ),
        )
    found = os.environ.get(NONO_ENV, "").strip() or shutil.which("nono") or ""
    if not found:
        return "", "", f"refused: nono not found (set {NONO_ENV} or put nono on PATH)"
    reported = _run_bounded(
        [ENV_PROGRAM, "NONO_NO_UPDATE_CHECK=1", f"TMPDIR={scratch}/", found, "--version"], scratch
    )
    lines = reported.stdout.strip().splitlines()
    return found, lines[0] if lines and reported.code == 0 else "unknown", ""


def _label(zone: str, version: str) -> str:
    if zone == TEST_ZONE:
        return (
            f"{version}, test zone: writes confined, egress blocked, "
            "this host only, not loopback only"
        )
    return f"{version}, setup zone: writes confined, egress open"


def prove_rung(root: Path, scratch: Path, deny_read: Sequence[Path], zone: str) -> ProvenRung:
    """Prove ``zone`` in ``scratch`` (a directory the caller owns and
    removes), denying reads of ``deny_read``. The only constructor of
    :class:`ProvenRung`."""
    started = time.monotonic()
    nono, version, refusal = _locate_nono(scratch)
    policy_path, digest, canaries = "", "", {}
    if not refusal:
        layout = _Layout.under(scratch)
        runtime = [Path(_INTERPRETER).parent, Path(sys.prefix), Path(sys.base_prefix)]
        policy = nono_policy(zone, [layout.zone], runtime, [*deny_read, layout.planted])
        path, digest = write_policy(root, policy)
        policy_path = str(path)
        canaries = _run_canaries(nono, path, layout, zone)
        refusal = _refusal(canaries)
    return ProvenRung(
        zone=zone,
        backend=nono,
        backend_version=version,
        policy_path=policy_path,
        policy_sha256=digest,
        canaries=canaries,
        seconds=round(time.monotonic() - started, 3),
        refusal=refusal,
        label=HOST_LABEL if refusal else _label(zone, version),
    )
