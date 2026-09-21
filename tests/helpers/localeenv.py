"""The environment that actually turns CPython's utf-8 default off.

Shared by every test that has to run a REAL child interpreter on a
non-utf-8 codec, because the naive env does not reproduce that: ``LC_ALL=C``
alone turns PEP 540 UTF-8 mode ON, so ``locale.getencoding()`` says
``US-ASCII`` while ``sys.flags.utf8_mode`` is 1 and every read and write
still decodes and encodes utf-8. Measured on CPython 3.12.8, #344's review
caught the PR body and a production comment both naming the env that does
not reproduce:

    env                             getencoding  utf8_mode  a write of chr(233)
    (inherited)                     UTF-8        0          WROTE
    LC_ALL=C                        US-ASCII     1          WROTE
    LC_ALL=C LANG=C                 US-ASCII     1          WROTE
    LC_ALL=C PYTHONUTF8=0           US-ASCII     0          RAISED
    LC_ALL=C LANG=C PYTHONUTF8=0    US-ASCII     0          RAISED

Moved here from ``tests/test_encoding_sites.py`` (#409's simplify pass, C1):
a test module may not be imported by a helper (``tests/test_helper_import_
direction.py``), and ``tests/test_child_output_encoding.py`` needed this
same env, so a fourth copy inside that file was the alternative this module
exists to remove. ``tests/test_encoding_sites.py`` imports both names back.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: The env, named so every caller answers the same measurement rather than
#: re-deriving its own.
NO_UTF8_DEFAULT = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}


def ascii_child_env() -> dict[str, str] | None:
    """The child env, or None where this platform ignores it.

    The check is ``sys.flags.utf8_mode`` and NOT ``locale.getencoding()``,
    because the table above is exactly a row where the encoding name says
    ASCII and the interpreter still encodes utf-8. A skip guard that
    cannot fail for the reason it names is the defect this suite is about.
    """
    env = {**os.environ, **NO_UTF8_DEFAULT}
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import locale, sys; print(locale.getencoding(), sys.flags.utf8_mode)",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    encoding, _, mode = probe.stdout.strip().partition(" ")
    if mode != "0" or "utf" in encoding.lower():
        return None
    return env
