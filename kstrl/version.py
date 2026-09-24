"""The kstrl version that wrote a record, from one source (#451).

Every record kstrl writes about a run carries this stamp: the event
envelope, the launch record, each evolution journal row, the manifest's
run stamp and each experiments.tsv row. Without it, a field missing
from an old record and a field missing because of a defect look the
same, and the only way to tell them apart was to compare commit dates
with run dates by hand.

The stamp is ``kstrl.__version__``, plus ``+g<commit>`` when kstrl runs
from a git checkout of its own source. The commit is read from the
directory above the ``kstrl`` package, never from the project kstrl is
working on: an installed kstrl's package sits in ``site-packages``,
which has no ``.git``, so it stamps the package version alone. A
checkout whose commit git cannot report stamps ``+commit-unknown``
rather than passing for an installed kstrl.
"""

from __future__ import annotations

import functools
from pathlib import Path

from kstrl import __version__
from kstrl.git import get_head_sha

#: What a reader shows for a record that carries no stamp. A record
#: written before #451 has none, and that is its age, not a defect.
UNSTAMPED = "written before stamping"

#: Bound on the one ``git rev-parse`` this makes per process.
_GIT_TIMEOUT = 5.0


def version_of_source(source_root: Path) -> str:
    """The stamp for a kstrl whose package sits in ``source_root``."""
    if not (source_root / ".git").exists():
        return __version__
    commit = get_head_sha(cwd=source_root, timeout=_GIT_TIMEOUT)
    return f"{__version__}+g{commit[:12]}" if commit else f"{__version__}+commit-unknown"


@functools.cache
def kstrl_version() -> str:
    """The stamp this process writes on every run record. Read once."""
    return version_of_source(Path(__file__).resolve().parent.parent)


def stamp_label(stamp: str) -> str:
    """How a reader shows a record's stamp: the stamp, or :data:`UNSTAMPED`."""
    return stamp or UNSTAMPED
