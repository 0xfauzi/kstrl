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

Where a rung is used (slice 2). ``ks factory`` under a ``[stack]``
proves both zones once per run with :func:`prove_zones`, in the policy
its commands then run in, and refuses below a proven rung: the setup
runs in the setup zone and every check in the test zone, through
:meth:`kstrl.rung.ProvenRung.command`, and the records carry the test
zone's label. Without a ``[stack]`` nothing is proven and every command
runs on the host under :data:`~kstrl.rung.HOST_LABEL`. ``ks doctor
--measure`` proves the two zones with no stack paths and only reports.

The verdict rules. A canary is contained only when its operation failed
with an errno; the egress canary only with EPERM, because a timeout or
"no route" is the network not answering rather than the rung refusing.
A timeout is never contained. A canary whose control did not succeed is
uninformative, because a sandbox cannot be credited with stopping an
operation that fails anyway. Any canary that is not contained, and any
positive control that does not pass, refuses the zone and is named.

A write canary's own report is not trusted alone: a backend that
redirects a write instead of failing it, or that misreports its errno,
must not read as contained. For the three write canaries kstrl also
checks the host filesystem itself, right after the sandboxed run and
before the control run writes the same path: if the target already
exists on the host at that moment, the write reached the host, and the
verdict is forced to "escaped" no matter what the sandboxed process
claimed. A SIGTERM delivered to a canary process inside the rung is a
positive control of its own: the rung must still report the ordinary
signal-death exit, never a plain success.

Two measured limits, both recorded rather than probed:

- DNS resolves inside the test zone whatever the policy says (G1), so
  the ``dns`` canary is recorded, never gated: the owner decided
  (2026-10-04, #700) to accept the gap rather than refuse the zone on
  it. Its verdict is still reported, and the test-zone label says
  "DNS open" whenever it escaped.
- The test zone reaches this host's own non-loopback addresses (G5), a
  Seatbelt property, so the test-zone label says "this host only, not
  loopback only". A probe would need a listener on a non-loopback
  interface, which the operating system's firewall can stop to ask the
  operator about.

No prover exists off macOS: on Linux nono's Landlock rules filter TCP
by port and not by host, so it cannot express a localhost-only test zone
(G7, measurement M2). There :func:`prove_zones` proves nothing and hands
back the host fallback (:func:`kstrl.rung.host_fallback`) for both zones,
whose one label every record carries (owner decision 2026-10-05, #700).
:func:`prove_rung` itself still refuses off macOS, read off the real
platform, so nono never runs unmeasured there.
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
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kstrl import git
from kstrl.atomicio import atomic_write_json, atomic_write_text
from kstrl.events import RunPaths
from kstrl.jsonread import read_json
from kstrl.rung import (
    ENV_PROGRAM,
    HOST_LABEL,
    PROVER_PLATFORMS,
    ProvenRung,
    Rung,
    _nono_argv,
    host_fallback,
    zone_dir,
)
from kstrl.statedir import control_dir, xdg_state_home
from kstrl.verify import ChildOutputDecodeError, run_scrubbed
from kstrl.version import kstrl_version

#: The environment variable naming the nono binary; PATH is searched
#: when it is unset.
NONO_ENV = "KSTRL_NONO"

SETUP_ZONE = "setup"
TEST_ZONE = "test"

#: A run's record of the rungs its commands ran in, beside
#: ``base-gates.json`` in the run directory (#700 slice 2).
ISOLATION_FILE = "isolation.json"

#: kstrl's own interpreter runs the canary. This is kstrl's runtime, not
#: an assumption about the target project.
_INTERPRETER = sys.executable

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
#: Argv for the SIGTERM positive control: a process that kills itself
#: must report an ordinary signal death (143, or -15 from kstrl's own
#: subprocess wrapper), never a plain success. Run through a shell
#: because sending a signal to the current process needs one.
SIGTERM_PROBE = ("/bin/sh", "-c", "kill -TERM $$")
#: The exit statuses a SIGTERM death may report.
SIGTERM_CODES = (143, -15)

#: nono's raw Seatbelt rules that headless Chromium needs (evaluation
#: 4.5, gap G3). The owner decided (2026-10-04, #700) they go into the
#: TEST-zone policy only, and only for a ``[stack]`` that declares
#: ``browser = true``: both widen the boundary.
BROWSER_SEATBELT_RULES = ("(allow mach-register)", "(allow iokit-open)")

#: What the rung must still allow: a check that cannot do these cannot run.
POSITIVE_CONTROLS = ("scratch_write", "loopback", "env_reaches", "exit_127", "sigterm")

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
    browser: bool = False,
) -> dict[str, Any]:
    """The nono profile for ``zone``: the setup zone leaves egress open,
    the test zone blocks it and allows localhost on any port. No DNS deny
    is ever written: denying the resolver socket blocks DNS in the setup
    zone and does not block it in the test zone (evaluation 4.3).
    ``browser`` adds :data:`BROWSER_SEATBELT_RULES` to the test zone and
    to nothing else."""
    network: dict[str, Any] = {"block": False}
    if zone == TEST_ZONE:
        network = {"block": True, "open_port": [0]}
    rules = list(BROWSER_SEATBELT_RULES) if browser and zone == TEST_ZONE else []
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
        **({"unsafe_macos_seatbelt_rules": rules} if rules else {}),
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
            zone_dir(scratch),
            scratch / "outside",
            zone_dir(scratch) / "planted",
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


#: Write canaries whose target the host filesystem is checked for,
#: between the sandboxed run and the control run. ``scratch_write`` is
#: excluded: it targets the granted zone, where a write is expected to
#: land.
_HOST_CHECKED_WRITES = ("write_outside", "write_tmp", "write_state_parent")


def _canary_verdicts(
    names: Sequence[str],
    sandboxed: Mapping[str, str],
    control: Mapping[str, str],
    host_escaped: Mapping[str, bool],
) -> dict[str, str]:
    """Each canary's verdict, a "contained" write downgraded to
    "escaped" when the host check caught it reaching the host anyway."""
    verdicts: dict[str, str] = {}
    for name in names:
        if name in POSITIVE_CONTROLS:
            verdicts[name] = sandboxed[name]
            continue
        verdict = _verdict(name, sandboxed[name], control[name])
        if host_escaped.get(name) and verdict.startswith("contained"):
            verdict = "escaped"
        verdicts[name] = verdict
    return verdicts


def _probe(nono: str, policy_path: Path, layout: _Layout, argv: Sequence[str]) -> _Ran:
    """One extra nono spawn outside the canary process: the missing-
    command and SIGTERM positive controls, each its own invocation."""
    return _run_bounded(_nono_argv(nono, policy_path, layout.zone, list(argv), []), layout.zone)


def _run_canaries(nono: str, policy_path: Path, layout: _Layout, zone: str) -> dict[str, str]:
    plan = _plan(zone, layout)
    names = [name for name, _kind, _target in plan]
    write_targets = {
        name: target
        for name, kind, target in plan
        if name in _HOST_CHECKED_WRITES and kind == "write"
    }
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
        # Checked now, before the control run writes the same paths: a
        # write that reached the host during the sandboxed run must not
        # be credited to the control run instead.
        host_escaped = {name: Path(target).exists() for name, target in write_targets.items()}
        for target in write_targets.values():
            Path(target).unlink(missing_ok=True)
        control = _outcomes(_run_bounded([ENV_PROGRAM, *assignments, *canary], layout.zone), names)
    finally:
        for _name, kind, target in plan:
            if kind == "write":
                Path(target).unlink(missing_ok=True)
    verdicts = _canary_verdicts(names, sandboxed, control, host_escaped)
    missing = _probe(nono, policy_path, layout, [f"kstrl-canary-missing-{layout.token}"])
    verdicts["exit_127"] = (
        "ok" if missing.code == MISSING_COMMAND_EXIT else (missing.why or f"exit {missing.code}")
    )
    killed = _probe(nono, policy_path, layout, SIGTERM_PROBE)
    verdicts["sigterm"] = (
        "ok" if killed.code in SIGTERM_CODES else (killed.why or f"exit {killed.code}")
    )
    return verdicts


#: Canaries that are recorded but never gate a zone. DNS is the one
#: entry (G1): nono 0.79 cannot deny it in the test zone, and the owner
#: decided (2026-10-04, #700) to accept the gap rather than refuse on
#: it. Its verdict is still reported and still drives the zone's label.
NON_GATING_CANARIES = ("dns",)


def _refusal(canaries: Mapping[str, str]) -> str:
    failed = [
        f"{name} ({verdict})"
        for name, verdict in canaries.items()
        if name not in NON_GATING_CANARIES
        and (verdict != "ok" if name in POSITIVE_CONTROLS else not verdict.startswith("contained"))
    ]
    return f"refused: {', '.join(failed)}" if failed else ""


def _locate_nono(scratch: Path) -> tuple[str, str, str]:
    """``(binary, version, refusal)``. The version is recorded, never
    consulted: the canaries decide. ``--version`` runs in ``scratch``
    with no update check, like every other nono spawn."""
    if sys.platform not in PROVER_PLATFORMS:
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


def _label(zone: str, version: str, canaries: Mapping[str, str]) -> str:
    if zone == TEST_ZONE:
        dns_suffix = ", DNS open" if canaries.get("dns", "") == "escaped" else ""
        return (
            f"{version}, test zone: writes confined, egress blocked, "
            f"this host only, not loopback only{dns_suffix}"
        )
    return f"{version}, setup zone: writes confined, egress open"


def prove_rung(
    root: Path,
    scratch: Path,
    deny_read: Sequence[Path],
    zone: str,
    *,
    writable: Sequence[Path] = (),
    readable: Sequence[Path] = (),
    browser: bool = False,
) -> ProvenRung:
    """Prove ``zone`` in ``scratch`` (a directory the caller owns and
    removes), denying reads of ``deny_read``. The only constructor of
    :class:`ProvenRung`.

    ``writable`` and ``readable`` are granted on top of the scratch zone
    and kstrl's own runtime, and ``browser`` adds the browser rules
    (#700 slice 2): the canaries run in the very policy every command of
    the rung then runs in, so a grant that lets a canary out refuses the
    rung."""
    started = time.monotonic()
    nono, version, refusal = _locate_nono(scratch)
    policy_path, digest, canaries = "", "", {}
    if not refusal:
        layout = _Layout.under(scratch)
        runtime = [Path(_INTERPRETER).parent, Path(sys.prefix), Path(sys.base_prefix)]
        policy = nono_policy(
            zone,
            [layout.zone, *writable],
            [*runtime, *readable],
            [*deny_read, layout.planted],
            browser,
        )
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
        label=HOST_LABEL if refusal else _label(zone, version, canaries),
        scratch=str(scratch),
    )


def prove_zones(
    root: Path, writable: Sequence[Path], readable: Sequence[Path], browser: bool
) -> dict[str, Rung]:
    """Both zones for one run, each proven in a scratch directory of its
    own that the caller removes through :func:`kstrl.rung.release`, and
    with the control directory denied to both (#700 slice 2). On a
    platform with no prover, the host fallback for both and nothing
    proven (owner decision 2026-10-05).

    Both zones can read the repository's git common directory and cannot
    write it (#700, the #625 trial): a kstrl worktree's ``.git`` file
    points into it, so without it ``git`` in a worktree fails with "not a
    git repository" in either zone. A check can already read the files of
    the repository."""
    fallback = host_fallback()
    if fallback is not None:
        return {SETUP_ZONE: fallback, TEST_ZONE: fallback}
    common = git.git_common_dir(root)
    readable = [*readable, *([common] if common is not None else [])]
    return {
        zone: prove_rung(
            root,
            Path(tempfile.mkdtemp(prefix=f"kstrl-rung-{zone}-")),
            [control_dir(root)],
            zone,
            writable=writable,
            readable=readable,
            browser=browser,
        )
        for zone in (SETUP_ZONE, TEST_ZONE)
    }


def write_record(
    root: Path, run_id: str, stack_digest: str, rungs: Mapping[str, Rung]
) -> list[str]:
    """Write the run's :data:`ISOLATION_FILE`, proven, refused or the host
    fallback; return why it could not be written, or []."""
    path = RunPaths.for_run(root, run_id).root / ISOLATION_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "runId": run_id,
                "kstrlVersion": kstrl_version(),
                "stackDigest": stack_digest,
                **{zone: asdict(rung) for zone, rung in rungs.items()},
            },
        )
    except OSError as exc:
        return [f"the isolation record cannot be written at {path}: {exc}"]
    return []
