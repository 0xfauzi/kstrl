"""A proven isolation rung, and the one nono command line (#700).

The record of a rung lives here, apart from the prover in
:mod:`kstrl.isolation`, because the prover runs its canaries through
:func:`kstrl.verify.run_scrubbed` while :mod:`kstrl.verify` must hold a
rung to run a command inside it: one module each way would be an import
cycle. Nothing here imports from kstrl.

Only :func:`kstrl.isolation.prove_rung` constructs a :class:`ProvenRung`
(``tests/test_isolation_census.py``), and :func:`_nono_argv` is the only
place a nono command line is built, so a command runs in exactly the
policy and the nono invocation the canaries ran in.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: What a result records when its command ran with no rung around it.
HOST_LABEL = "none: ran on the host"

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


def label_of(rung: ProvenRung | None) -> str:
    """The isolation a result records: the rung's label, or
    :data:`HOST_LABEL` when the command ran with no rung."""
    return HOST_LABEL if rung is None else rung.label


def release(rungs: Iterable[ProvenRung | None]) -> None:
    """Remove each rung's scratch directory: its TMPDIR and nono's state
    files go with it. A scratch directory is kstrl's own temporary
    directory, so a failure to remove it changes no verdict."""
    for rung in rungs:
        if rung is not None and rung.scratch:
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
