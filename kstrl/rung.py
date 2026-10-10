"""A proven isolation rung, and the one nono command line (#700).

The record of a rung lives here, apart from the prover in
:mod:`kstrl.isolation`, because the prover runs its canaries through
:func:`kstrl.scrubbed_run.run_scrubbed` while :mod:`kstrl.verify` must hold a
rung to run a command inside it: one module each way would be an import
cycle. Nothing here imports from kstrl.

Only :func:`kstrl.isolation.prove_rung` constructs a :class:`ProvenRung`
(``tests/test_isolation_census.py``), and :func:`_nono_argv` is the only
place a nono command line is built, so a command runs in exactly the
policy and the nono invocation the canaries ran in.

The host fallback (#700, owner decision 2026-10-05). Where no prover
exists for the platform (today every platform but macOS), a ``[stack]``
run's commands run on the host under a :class:`HostFallback` instead of
refusing. Only :func:`host_fallback` constructs one, the platform alone
decides it (never a failed proof: on macOS a rung whose canaries fail
still refuses), and its one label, :data:`HOST_FALLBACK_LABEL`, is what
every record of the run carries.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: What a result records when its command ran with no rung around it.
HOST_LABEL = "none: ran on the host"

#: The platforms a prover exists for. A ``[stack]`` run anywhere else runs
#: its commands on the host under :data:`HOST_FALLBACK_LABEL` (#700 M2
#: gives Linux a proven rung).
PROVER_PLATFORMS = ("darwin",)

#: The one label of a run on a platform with no prover: it names the
#: platform and says that nothing was isolated.
HOST_FALLBACK_LABEL = (
    "none: no isolation rung exists on {platform}, so every command ran on the host "
    "and nothing was isolated"
)

#: Test seam, read only by :func:`host_fallback`: the platform the prover
#: decision is made for, in place of ``sys.platform``. It only decides
#: whether a run falls back; whether nono may run at all is still read
#: off the real ``sys.platform`` (:func:`kstrl.isolation.prove_rung`).
PLATFORM_ENV = "KSTRL_ISOLATION_PLATFORM"

#: The program in front of every command inside the rung. nono turns a
#: missing command into exit 1 and strips the variables a shell or an
#: interpreter reads at startup; ``env`` in front restores exit 127 and
#: 126 and sets those variables after nono has run (evaluation 4.9).
ENV_PROGRAM = "/usr/bin/env"


def zone_dir(scratch: Path) -> Path:
    """The one granted directory inside a rung's scratch: the canaries'
    positive write lands here, and every command's TMPDIR and nono's own
    state live here."""
    return scratch / "zone"


@dataclass(frozen=True)
class ProvenRung:
    """One zone's reading. ``refusal`` is empty only when every canary
    was contained and every positive control passed. ``scratch`` is the
    directory the caller handed the prover and removes when the rung is
    no longer used (:func:`release`)."""

    zone: str
    backend: str
    backend_version: str
    policy_path: str
    policy_sha256: str
    canaries: Mapping[str, str]
    seconds: float
    refusal: str
    label: str
    scratch: str

    def command(self, argv: Sequence[str], assignments: Sequence[str]) -> list[str]:
        """``argv`` as a command line that runs inside this rung, with
        ``assignments`` (``NAME=value``) set after nono has started it.
        Raises when the rung was refused: a command must never run on
        the host while its result claims a rung."""
        if self.refusal or not self.policy_path:
            raise RuntimeError(f"no proven rung to run in: {self.refusal or 'no policy'}")
        return _nono_argv(
            self.backend, Path(self.policy_path), zone_dir(Path(self.scratch)), argv, assignments
        )


@dataclass(frozen=True)
class HostFallback:
    """A ``[stack]`` run on a platform with no prover: its commands run on
    the host. It claims nothing a rung proves (no zone, no canaries, no
    policy, no refusal): it carries the platform and the one label."""

    platform: str
    label: str

    def command(self, argv: Sequence[str], assignments: Sequence[str]) -> list[str]:
        """``argv`` on the host, with :data:`ENV_PROGRAM` in front as inside
        a rung, so a missing command exits 127 here too and ``assignments``
        are set the same way."""
        return [ENV_PROGRAM, *assignments, *argv]


#: What a ``[stack]`` run's commands run in: a proven rung, or the host
#: fallback on a platform with no prover. Never None: None is a run with
#: no ``[stack]``.
Rung = ProvenRung | HostFallback


#: What an operator reads when a command fails inside a proven rung (#700,
#: the #625 trial): a tool that is refused a path prints only its own error,
#: such as EPERM, so the message must say that the sandbox can be the cause.
SANDBOX_HINT = (
    "the {zone} zone of the isolation sandbox can be the cause: grant a path the "
    "command needs with `writable` or `readable` in the [stack] table of kstrl.toml"
)


def sandbox_hint(rung: Rung | None) -> str:
    """:data:`SANDBOX_HINT` for a command that failed in ``rung``, or "" when
    it ran in no sandbox (no ``[stack]``, or the host fallback)."""
    return SANDBOX_HINT.format(zone=rung.zone) if isinstance(rung, ProvenRung) else ""


def host_fallback() -> HostFallback | None:
    """The host fallback when no prover exists for this platform, else None.
    The only constructor of :class:`HostFallback`, and the only reader of
    :data:`PLATFORM_ENV`."""
    platform = os.environ.get(PLATFORM_ENV, "").strip() or sys.platform
    if platform in PROVER_PLATFORMS:
        return None
    return HostFallback(platform, HOST_FALLBACK_LABEL.format(platform=platform))


def refusal_of(rung: Rung) -> str:
    """Why ``rung`` must not be run in, or "". A host fallback is never
    refused: no proof ran, so none failed."""
    return rung.refusal if isinstance(rung, ProvenRung) else ""


def label_of(rung: Rung | None) -> str:
    """The isolation a result records: the rung's or the fallback's label,
    or :data:`HOST_LABEL` when the command ran with no ``[stack]``."""
    return HOST_LABEL if rung is None else rung.label


def release(rungs: Iterable[Rung | None]) -> None:
    """Remove each proven rung's scratch directory: its TMPDIR and nono's
    state files go with it. A scratch directory is kstrl's own temporary
    directory, so a failure to remove it changes no verdict. A host
    fallback has none."""
    for rung in rungs:
        if isinstance(rung, ProvenRung) and rung.scratch:
            shutil.rmtree(rung.scratch, ignore_errors=True)


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
