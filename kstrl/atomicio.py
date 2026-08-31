"""The one atomic file write, so its mode and encoding rules live in one place.

#291. TEN copies of "``tempfile.mkstemp`` in the destination directory,
write, ``os.replace``" had grown across the package: ``manifest``,
``prd``, ``decompose``, ``knowledge``, ``workqueue``, ``autonomy``,
``inbox``, ``fixtures``, ``factory`` and ``init_cmd``. NINE carried the
mode defect below (all but ``init_cmd``, fixed in #290), and FIVE of
those also left the encoding to the locale.

Those counts are the argument, so they are stated once. The issue was
filed against four call sites, which is the point rather than a
complaint about the issue: ten copies of a fifteen-line pattern is a
thing nobody can see the whole of, and a careful call site does not stay
careful. This docstring is the only place the reasoning is written out;
call sites point here instead of restating it, so a correction lands in
one file.

MODE. ``mkstemp`` creates its file 0600 and ``os.replace`` carries that
onto the destination, so every one of those writers silently tightened
whatever the operator had. Measured on this tree before the change:
``workqueue.atomic_write`` and ``decompose._atomic_write_json`` both took
a 0o644 file to 0o600, on git-tracked operator files
(``scripts/kstrl/manifest.json``, per-component ``prd.json``). The
failure that costs a run, from #290: a container or CI job that runs one
kstrl command as one uid and the factory as another gets a
``PermissionError`` on a file that worked before, with nothing in the
error pointing at the mode.

That 0600 was never a decision. Measured in a real operator state
directory: every file kstrl wrote with a plain ``open()`` was 0644
(``evolution.jsonl``, ``progress.jsonl``, ``experiments.tsv``), and every
file it wrote through the mkstemp pattern was 0600 (29 knowledge facts).
Same tree, same owner, same day; the mode tracked nothing but which
writer happened to run. So there are not two correct behaviours here
needing two helpers, there is one behaviour and an implementation detail
that leaked into most copies of it.

Hence the rule, and the whole of it: an atomic write leaves the
destination's mode exactly as it found it, and a file that did not exist
is created the way ``open(path, "w")`` would have created it. There is
deliberately no ``mode`` parameter. Nothing in kstrl writes a credential
through this path, so a private-mode option would today be reachable
only by mistake; a caller that genuinely needs one can add it, with a
reason, at the point it is needed.

HOW THE NEW-FILE MODE IS OBTAINED. ``os.open`` with ``O_CREAT`` and 0o666
lets the kernel subtract the umask, which is what gives a new file the
same mode a plain write would have given it. The alternative,
``os.umask(0)`` followed by restoring it, is a process-wide mutation with
a window in it: ``workqueue.atomic_write`` is called from factory worker
threads and the serve daemon, and ``init_cmd`` runs inside the Textual
wizard, so that window is a real race in this codebase and not a
theoretical one. Asking the kernel to apply the umask costs nothing and
has no window, which is also why this rolls its own O_EXCL name loop
instead of calling ``mkstemp``: ``mkstemp`` hard-codes the 0600 this
module exists to stop.

The temp file is created empty and its mode is pinned before a single
byte is written, so replacing a 0600 file never exposes its contents
through the temp.

ATOMICITY is unchanged and comes from the same two properties as before:
the temp file is created in the destination's own directory, so the
``os.replace`` is a rename within one filesystem, and a rename is atomic.
A crash before the replace leaves the destination untouched and the temp
file removed.

IDENTITY is NOT preserved, and this is the one property a reader of
"keeps its mode" is likely to assume and not get. ``os.replace`` swaps
the directory entry, so a destination that was a symlink becomes a
regular file and a destination that shared an inode with a hard link
stops sharing it. #290 measured exactly that on
``scripts/kstrl/prompt.md``, and the caller that cares about it
(``init_cmd._rewrite_blockers``) refuses such a target BEFORE calling a
writer. Guarding it here instead would put a special case in shared
infrastructure for the one caller that has an opinion, and would silently
change what the other nine do; the property is documented so a future
caller decides deliberately.

Stdlib only and importing nothing: this module is a leaf so that
``init_cmd`` can use it without pulling ``kstrl.workqueue`` into
``kstrl.loop``'s static import closure, which #274's
``tests/test_state_dir_scope.py`` refuses.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

#: Retries for a colliding temp name. ``O_EXCL`` makes a collision safe
#: rather than silent, and 32 random bits inside one directory makes it
#: vanishingly rare, so this only has to be larger than one.
_NAME_ATTEMPTS = 100

#: Requested mode for the temp file. The kernel subtracts the umask from
#: it, so a new destination lands on the same mode ``open(path, "w")``
#: would have produced.
_NEW_FILE_MODE = 0o666


def _create_temp(directory: Path, prefix: str) -> tuple[int, Path]:
    """Create an empty temp file in ``directory``, umask applied.

    The ``mkstemp`` contract (exclusive creation, unpredictable name) with
    the umask honored instead of overridden to 0600.
    """
    for _ in range(_NAME_ATTEMPTS):
        candidate = directory / f"{prefix}{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _NEW_FILE_MODE,
            )
        except FileExistsError:
            continue
        return fd, candidate
    raise FileExistsError(
        f"could not create a temp file in {directory} after {_NAME_ATTEMPTS} tries"
    )


def atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically, in utf-8, keeping its mode.

    An existing destination keeps the mode it already had, so the
    operator's own choice survives a rewrite. A destination that does not
    exist is created at the process umask default.

    utf-8 is pinned rather than left to the locale so that what one
    machine writes another reads back byte-identical, which several
    callers depend on for digests and comparisons.

    The parent directory must exist; callers that create files in fresh
    directories make them first, and this raising is better than quietly
    building a path that a caller got wrong.
    """
    try:
        mode: int | None = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        # New file: keep what the kernel already applied the umask to.
        mode = None

    fd, tmp_path = _create_temp(target.parent, f".{target.name}-")
    try:
        # ``fdopen`` FIRST, so the descriptor belongs to a context
        # manager before anything that can fail touches it. With the
        # ``fchmod`` outside, a destination whose filesystem refuses it
        # (NFS with root-squash, exFAT, SMB all return EPERM or EROFS)
        # raised past the raw fd and leaked it: measured at 20 leaked
        # descriptors from 20 failed writes, which inside the serve
        # daemon or a retrying worker accumulates to EMFILE.
        #
        # The ``fchmod`` still runs BEFORE the first write, so the file
        # is empty for the whole window in which it carries the wrong
        # mode, and replacing a 0600 file never exposes its contents.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(content)
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(target: Path, payload: Any) -> None:
    """``atomic_write_text`` for the JSON documents kstrl persists.

    Two-space indent and exactly one trailing newline, which is the shape
    every hand-rolled copy of this wrote and what the committed fixtures
    and the pre-commit end-of-file hook both expect.

    ``ensure_ascii=False`` for the same reason the encoding is pinned:
    the file is utf-8, so a non-ASCII character belongs in it as itself
    rather than as a ``\\uXXXX`` escape. It also matches what kstrl's
    other JSON writers already pass (``workqueue``, ``serve``,
    ``statedir``, ``intake_github``), so there is one on-disk shape
    instead of one per writer.
    """
    atomic_write_text(target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
