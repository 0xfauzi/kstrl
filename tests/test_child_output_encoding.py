"""#409: a child's bytes are decoded as utf-8, not as the locale says.

WHY A CHILD INTERPRETER. ``locale.getencoding()`` is fixed at interpreter
start, so ``monkeypatch.setenv("LC_ALL", "C")`` inside this process changes
nothing about how ``subprocess`` decodes. The only way to measure the rule
this file is about is to run the real entry points in a process that was
STARTED under that locale.

WHY ``sys.flags.utf8_mode`` AND NOT ``locale.getencoding()``.
``tests/helpers/localeenv.py`` records the measurement this file used to
repeat: a bare ``LC_ALL=C`` turns PEP 540 UTF-8 mode ON, so
``locale.getencoding()`` reports ``US-ASCII`` while the interpreter still
encodes and decodes utf-8 underneath it. ``sys.flags.utf8_mode`` is the flag
that actually governs the decode this file is about, so the child reports
that instead.

WHY A REAL REPOSITORY AND NOT A STUB. The pair this issue is about is
``git.get_diff_content`` and ``git.get_diff_name_status``, and what splits
them is a git behaviour: ``core.quotepath`` escapes a non-ASCII path in a
diff HEADER to backslash-octal (pure ASCII, decodes anywhere) while ``-z``
turns quoting off and emits the raw utf-8 bytes. A stub that printed bytes
would not reproduce the asymmetry that makes pinning half the pair worse
than pinning neither.

THE TWO WAYS THIS TEST COULD PASS WHILE MEASURING NOTHING, both closed
below rather than trusted. A green run here is evidence only if the child
was really on ``utf8_mode == 0`` and really imported THIS checkout's
``kstrl``. If a future CPython or a stray ``PYTHONUTF8`` in the environment
put the child on utf-8 mode, every assertion below would pass against
unfixed code; and the child resolves ``kstrl`` through the venv's editable
install rather than through ``sys.path[0]``, so a venv pointing at another
checkout would have it measuring somebody else's tree. So the child reports
its own ``utf8_mode`` and the file it imported, and the test asserts both
before anything else.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import kstrl
from tests.conftest import make_review_repo
from tests.helpers.localeenv import NO_UTF8_DEFAULT

#: A bound on the child, which itself only runs git. Generous rather than
#: tight: the test is not measuring latency, and several other suites may
#: be sharing this machine's CPU while it runs.
CHILD_TIMEOUT_SECONDS = 120

#: The child. It writes ASCII-only JSON on stdout, because under this locale
#: printing the accented answer itself would raise UnicodeEncodeError in the
#: child and the failure would be the test's, not the code's.
DRIVER = """
import json, sys
from pathlib import Path
import kstrl
from kstrl import git
from kstrl.timeout import run_with_timeout

repo = Path(sys.argv[1])
quoted_path = sys.argv[2]
answer = {
    "utf8_mode": sys.flags.utf8_mode,
    "kstrl_file": kstrl.__file__,
    "content_has_path": quoted_path in git.get_diff_content("main", repo),
    "name_status": git.get_diff_name_status("main", repo, strict=True),
    # codespell:ignore-next-line
    "printf": run_with_timeout(["printf", "caf\\\\303\\\\251"], timeout=30).stdout,
}
sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=True).encode("ascii"))
"""


def _git_octal_quote(name: str) -> str:
    """The C-quoted spelling git writes for a ``+++``/``--- `` header path
    when it contains a non-ASCII byte: each such byte as ``\\NNN`` octal,
    the rest verbatim, wrapped in double quotes. Built here from a real
    accented character rather than typed as a literal escape sequence, so
    the source text never spells the ASCII prefix next to the escape - see
    ``[tool.codespell]`` in ``pyproject.toml`` for why that split reads as
    a typo (#399, and the same reason ``tests/test_policy_envelope.py``'s
    sibling of this function is built the same way).
    """
    out = ['"b/']
    for byte in name.encode("utf-8"):
        out.append(chr(byte) if byte < 0x80 else f"\\{byte:03o}")
    out.append('"')
    return "".join(out)


def test_a_child_writing_an_accented_byte_decodes_under_a_c_locale(
    tmp_path: Path,
) -> None:
    """Three real entry points, one child, one C locale.

    ``get_diff_content`` is the half PR #405 already pinned and it passes
    before this change as well as after; it is asserted here so the pair is
    measured together rather than one of them being taken on trust. The
    repository is built through ``tests.conftest.make_review_repo``, the
    same builder every other reviewer-facing test in this suite uses,
    rather than a bespoke one here.
    """
    repo = make_review_repo(
        tmp_path / "repo",
        files={"café.py": "y = 2\n"},
        base_files={"seed.py": "x = 1\n"},
    ).path
    quoted_path = _git_octal_quote("café.py")
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(driver), str(repo), quoted_path],
        capture_output=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_SECONDS,
        env={**NO_UTF8_DEFAULT, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        start_new_session=True,
    )

    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    # The two anti-vacuity assertions, first because a failure in either
    # one means every assertion below it is measuring something else.
    assert answer["utf8_mode"] == 0, (
        f"the child ran with sys.flags.utf8_mode == {answer['utf8_mode']!r}, "
        "not 0, so this test would pass against unfixed code. The locale env "
        "no longer does what it did when this was written; report it, do "
        "not adjust it away."
    )
    assert answer["kstrl_file"] == kstrl.__file__, (
        f"the child imported kstrl from {answer['kstrl_file']!r} and this "
        f"process imported it from {kstrl.__file__!r}, so the test is not "
        "measuring the tree under edit."
    )
    assert answer["content_has_path"] is True
    assert answer["name_status"] == [["A", "café.py"]]
    assert answer["printf"] == "café"
