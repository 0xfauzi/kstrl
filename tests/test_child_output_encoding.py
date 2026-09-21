"""#409: a child's bytes are decoded as utf-8, not as the locale says.

WHY A CHILD INTERPRETER. ``locale.getencoding()`` is fixed at interpreter
start, so ``monkeypatch.setenv("LC_ALL", "C")`` inside this process changes
nothing about how ``subprocess`` decodes. The only way to measure the rule
this file is about is to run the real entry points in a process that was
STARTED under that locale. Both variables plus ``PYTHONUTF8=0``, because a C
locale alone turns PEP 540 UTF-8 mode ON and the decode would be utf-8 for a
reason that has nothing to do with the fix. Measured: with all three,
``locale.getencoding()`` is ``US-ASCII``.

WHY A REAL REPOSITORY AND NOT A STUB. The pair this issue is about is
``git.get_diff_content`` and ``git.get_diff_name_status``, and what splits
them is a git behaviour: ``core.quotepath`` escapes a non-ASCII path in a
diff HEADER to backslash-octal (pure ASCII, decodes anywhere) while ``-z``
turns quoting off and emits the raw utf-8 bytes. A stub that printed bytes
would not reproduce the asymmetry that makes pinning half the pair worse
than pinning neither.

THE TWO WAYS THIS TEST COULD PASS WHILE MEASURING NOTHING, both closed
below rather than trusted. A green run here is evidence only if the child
was really on a non-utf-8 codec and really imported THIS worktree's
``kstrl``. If a future CPython or a stray ``PYTHONUTF8`` in the
environment put the child on utf-8, every assertion below would pass
against unfixed code; and the child resolves ``kstrl`` through the venv's
editable install rather than through ``sys.path[0]``, so a venv pointing
at another checkout would have it measuring somebody else's tree. So the
child reports its codec and the file it imported, and the test asserts
both. Measured by the critic at 6e0b4d1: ``US-ASCII`` and
``/Users/wumpinihussein/Documents/code/ralph-wt-409/kstrl/__init__.py``.
"""

from __future__ import annotations

import codecs
import json
import subprocess
import sys
from pathlib import Path

import kstrl
from tests.helpers.gitrepo import git_in, set_identity

#: The three variables that put the child on a non-utf-8 codec.
C_LOCALE_ENV = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}

#: A bound on the child, which itself only runs git. Generous because this
#: machine runs several suites at once; the test is not measuring latency.
CHILD_TIMEOUT_SECONDS = 120

#: The child. It writes ASCII-only JSON on stdout, because under this locale
#: printing the accented answer itself would raise UnicodeEncodeError in the
#: child and the failure would be the test's, not the code's.
DRIVER = """
import json, locale, sys
from pathlib import Path
import kstrl
from kstrl import git
from kstrl.timeout import run_with_timeout

repo = Path(sys.argv[1])
answer = {
    "codec": locale.getencoding(),
    "kstrl_file": kstrl.__file__,
    "content_has_path": "caf" in git.get_diff_content("main", repo),
    "name_status": git.get_diff_name_status("main", repo, strict=True),
    "printf": run_with_timeout(["printf", "caf\\\\303\\\\251"], timeout=30).stdout,
}
sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=True).encode("ascii"))
"""


def _repo(tmp_path: Path) -> Path:
    """A repository whose branch adds one file with a non-ASCII name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    set_identity(repo)
    (repo / "seed.py").write_text("x = 1\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "seed")
    git_in(repo, "checkout", "-q", "-b", "work")
    (repo / "café.py").write_text("y = 2\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "accented")
    return repo


def test_a_child_writing_an_accented_byte_decodes_under_a_c_locale(
    tmp_path: Path,
) -> None:
    """Three real entry points, one child, one C locale.

    ``get_diff_content`` is the half PR #405 already pinned and it passes
    before this change as well as after; it is asserted here so the pair is
    measured together rather than one of them being taken on trust.
    """
    repo = _repo(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(driver), str(repo)],
        capture_output=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_SECONDS,
        env={**C_LOCALE_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        start_new_session=True,
    )

    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    # The two anti-vacuity assertions, first because a failure in either
    # one means every assertion below it is measuring something else.
    assert codecs.lookup(answer["codec"]).name != "utf-8", (
        f"the child ran on codec {answer['codec']!r}, which IS utf-8, so this "
        "test would pass against unfixed code. The locale env no longer does "
        "what it did when this was written; report it, do not adjust it away."
    )
    assert answer["kstrl_file"] == kstrl.__file__, (
        f"the child imported kstrl from {answer['kstrl_file']!r} and this "
        f"process imported it from {kstrl.__file__!r}, so the test is not "
        "measuring the tree under edit."
    )
    assert answer["content_has_path"] is True
    assert answer["name_status"] == [["A", "café.py"]]
    assert answer["printf"] == "café"
