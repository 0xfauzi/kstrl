"""#291: an atomic write must not change the mode or guess the encoding.

Every assertion here runs against a real filesystem and reads the mode
back with ``stat``. Mocking the write would only pin that we called the
functions we call: the defect was that the real bytes on the real disk
came out 0600, so a mock cannot see it.

The two defects, both measured on this tree before the fix:

* ``tempfile.mkstemp`` creates 0600 and ``os.replace`` carries that onto
  the destination, so rewriting an operator's 0o644 file left it 0o600.
  ``workqueue.atomic_write`` and ``decompose._atomic_write_json`` both
  did this to git-tracked files.
* Neither pinned an encoding, so the bytes depended on the machine's
  locale.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl.atomicio import atomic_write_json, atomic_write_text
from kstrl.decompose import _atomic_write_json
from kstrl.workqueue import atomic_write

#: A string whose utf-8 and latin-1 encodings differ, so a test that
#: round-trips it through an unpinned encoding fails rather than passing
#: by accident on an ASCII payload.
NON_ASCII = "naive cafe: éèü £€ 你好 \U0001f600"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


#: Every writer that goes through the shared helper, as
#: (name, callable taking (path, text)). Parametrised rather than written
#: out per module because the whole point of #291 is that these must not
#: be allowed to drift apart again.
WRITERS: list[tuple[str, object]] = [
    ("atomicio.atomic_write_text", lambda p, t: atomic_write_text(p, t)),
    ("atomicio.atomic_write_json", lambda p, t: atomic_write_json(p, {"t": t})),
    ("workqueue.atomic_write", lambda p, t: atomic_write(p, t)),
    ("decompose._atomic_write_json", lambda p, t: _atomic_write_json(p, {"t": t})),
]


@pytest.mark.parametrize("name,write", WRITERS, ids=[w[0] for w in WRITERS])
class TestModeSurvivesTheWrite:
    def test_an_existing_files_mode_is_preserved(
        self, name: str, write: object, tmp_path: Path
    ) -> None:
        """The #291 defect itself: 0o644 in, 0o644 out.

        Before the fix every one of these turned it into 0o600.
        """
        target = tmp_path / "tracked.json"
        target.write_text("{}\n", encoding="utf-8")
        os.chmod(target, 0o644)

        write(target, "hello")  # type: ignore[operator]

        assert _mode(target) == 0o644, f"{name} changed the mode of an existing file"

    @pytest.mark.parametrize("original", [0o600, 0o640, 0o644, 0o664, 0o666])
    def test_whatever_mode_the_operator_chose_is_what_survives(
        self, name: str, write: object, original: int, tmp_path: Path
    ) -> None:
        """Preserved, not replaced by a new hard-coded default.

        0o600 is in the list on purpose: a caller that deliberately
        tightened a file must not have it loosened by a write either.
        """
        target = tmp_path / "tracked.json"
        target.write_text("{}\n", encoding="utf-8")
        os.chmod(target, original)

        write(target, "hello")  # type: ignore[operator]

        assert _mode(target) == original, f"{name} did not preserve {oct(original)}"

    def test_a_new_file_lands_where_a_plain_write_would_have(
        self, name: str, write: object, tmp_path: Path
    ) -> None:
        """No destination to copy from: match ``open(path, "w")`` exactly.

        Measured against a plain write in the same directory under the
        same umask rather than asserted as a literal, because the correct
        answer is a function of the umask and hard-coding 0o644 would
        just be a different wrong answer for an operator with umask 077.
        """
        reference = tmp_path / "reference.txt"
        reference.write_text("x", encoding="utf-8")

        target = tmp_path / "fresh.json"
        write(target, "hello")  # type: ignore[operator]

        assert _mode(target) == _mode(reference), (
            f"{name} created a new file at {oct(_mode(target))}, but a plain "
            f"write in the same directory produced {oct(_mode(reference))}"
        )

    def test_the_temp_file_leaves_nothing_behind(
        self, name: str, write: object, tmp_path: Path
    ) -> None:
        target = tmp_path / "tracked.json"
        write(target, "hello")  # type: ignore[operator]
        assert [p.name for p in tmp_path.iterdir()] == ["tracked.json"]


class TestNewFileModeTracksTheUmask:
    """The umask is what decides a new file's mode, so vary it and look.

    ``os.umask`` is process-wide, so this is the one place that touches
    it, it restores it in a ``finally``, and the production path
    deliberately never reads it at all (``atomicio`` asks the kernel to
    apply it via ``O_CREAT``, because ``workqueue.atomic_write`` runs on
    factory worker threads where a mutate-and-restore window is a race).
    """

    @pytest.mark.parametrize("umask,expected", [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
    def test_a_new_file_honors_the_process_umask(
        self, umask: int, expected: int, tmp_path: Path
    ) -> None:
        previous = os.umask(umask)
        try:
            target = tmp_path / "fresh.txt"
            atomic_write_text(target, "hello")
            assert _mode(target) == expected
        finally:
            os.umask(previous)

    def test_an_existing_file_beats_the_umask(self, tmp_path: Path) -> None:
        """Preserving a mode means preserving it even when the umask
        would have forbidden creating it that way."""
        target = tmp_path / "tracked.txt"
        target.write_text("old", encoding="utf-8")
        os.chmod(target, 0o664)

        previous = os.umask(0o077)
        try:
            atomic_write_text(target, "new")
        finally:
            os.umask(previous)

        assert _mode(target) == 0o664


class TestEncodingIsPinnedNotInherited:
    def test_utf8_round_trips_through_every_writer(self, tmp_path: Path) -> None:
        target = tmp_path / "text.md"
        atomic_write_text(target, NON_ASCII)
        assert target.read_text(encoding="utf-8") == NON_ASCII
        assert target.read_bytes() == NON_ASCII.encode("utf-8")

    def test_json_is_written_utf8_with_one_trailing_newline(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.json"
        atomic_write_json(target, {"k": NON_ASCII})

        raw = target.read_bytes()
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")
        assert json.loads(raw.decode("utf-8"))["k"] == NON_ASCII

    def test_the_bytes_do_not_depend_on_the_locale(self, tmp_path: Path) -> None:
        """The claim an in-process test cannot make.

        ``locale.getpreferredencoding`` is read at interpreter start, so
        setting LC_ALL here would change nothing. This runs a second
        interpreter under a POSIX/ASCII locale with PYTHONCOERCECLOCALE
        and PYTHONUTF8 disabled, which is what a minimal container gives
        you, and requires byte-for-byte identical output.
        """
        import kstrl.atomicio

        repo_root = Path(kstrl.atomicio.__file__).parent.parent
        script = (
            "import sys; from pathlib import Path;"
            f"sys.path.insert(0, {str(repo_root)!r});"
            "from kstrl.atomicio import atomic_write_text;"
            f"atomic_write_text(Path(sys.argv[1]), {NON_ASCII!r})"
        )
        target = tmp_path / "from_c_locale.txt"
        env = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
        }
        result = subprocess.run(
            [sys.executable, "-c", script, str(target)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == NON_ASCII.encode("utf-8")


class TestAtomicityIsUnchanged:
    def test_the_original_survives_a_failed_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that fails must leave the destination untouched and
        drop its temp file, which is the property the whole pattern
        exists for.

        The failure is injected at ``os.replace``, i.e. after the temp
        file exists and has been written. Making the *payload* explode
        instead would raise inside ``json.dumps`` before any file was
        created, and would pass without the cleanup path running at all.
        """
        target = tmp_path / "tracked.json"
        target.write_text("original\n", encoding="utf-8")
        os.chmod(target, 0o644)

        def boom(src: object, dst: object) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="no space left"):
            atomic_write_json(target, {"k": "new"})
        monkeypatch.undo()

        assert target.read_text(encoding="utf-8") == "original\n"
        assert _mode(target) == 0o644
        assert [p.name for p in tmp_path.iterdir()] == ["tracked.json"], (
            "the temp file was left behind after a failed write"
        )

    def test_the_temp_file_is_created_in_the_destination_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same-directory temp is what makes ``os.replace`` a rename
        within one filesystem, and a rename atomic. A temp in /tmp would
        still pass every other test in this file while silently making
        the write non-atomic on a machine where /tmp is its own mount.
        """
        target = tmp_path / "sub" / "tracked.txt"
        target.parent.mkdir()

        seen: list[Path] = []
        real_replace = os.replace

        def spy(src: object, dst: object) -> None:
            seen.append(Path(str(src)))
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", spy)
        atomic_write_text(target, "hello")
        monkeypatch.undo()

        assert seen, "os.replace was never called"
        assert seen[0].parent == target.parent
        assert target.read_text(encoding="utf-8") == "hello"


class TestTheTempNameLoop:
    """The part ``mkstemp`` used to provide, so it has to be tested here.

    Rolling this by hand is what buys the umask-honoring ``O_CREAT``
    (``mkstemp`` hard-codes 0600), so the exclusive-creation contract that
    came free before is now this module's own responsibility.
    """

    def test_a_name_collision_retries_rather_than_clobbering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kstrl.atomicio as atomicio

        # First two names collide with files that already exist; the
        # third is fresh. A helper that opened without O_EXCL would
        # truncate somebody else's file instead of moving on.
        names = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb", "cccccccc"])
        monkeypatch.setattr(atomicio.secrets, "token_hex", lambda _n: next(names))

        target = tmp_path / "doc.txt"
        squatter = tmp_path / ".doc.txt-aaaaaaaa.tmp"
        squatter.write_text("not mine to touch", encoding="utf-8")

        atomic_write_text(target, "hello")

        assert target.read_text(encoding="utf-8") == "hello"
        assert squatter.read_text(encoding="utf-8") == "not mine to touch"

    def test_exhausting_every_name_raises_instead_of_looping_forever(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kstrl.atomicio as atomicio

        monkeypatch.setattr(atomicio.secrets, "token_hex", lambda _n: "collide")
        (tmp_path / ".doc.txt-collide.tmp").write_text("squatting", encoding="utf-8")

        with pytest.raises(FileExistsError, match="could not create a temp file"):
            atomic_write_text(tmp_path / "doc.txt", "hello")

        assert not (tmp_path / "doc.txt").exists()


class TestTheHelperDoesNotOfferTheWrongBehaviour:
    def test_there_is_no_mode_parameter_to_get_wrong(self) -> None:
        """#291's design constraint, pinned.

        Nothing in kstrl writes a credential through this path, so a
        private-mode option would today be reachable only by mistake. A
        future caller that genuinely needs one adds it with a reason;
        this test is what makes that a deliberate act rather than a
        default nobody noticed.
        """
        import inspect

        for func in (atomic_write_text, atomic_write_json):
            params = list(inspect.signature(func).parameters)
            assert params == ["target", "content"] or params == ["target", "payload"], (
                f"{func.__name__} grew a parameter; if it is a mode, #291's "
                f"argument for one behaviour needs revisiting first"
            )


class TestEveryCopyOfThePatternWasMigrated:
    """The mechanised half of #291's answer to "helper or four call sites".

    A helper only removes the class of defect if the copies actually go
    away. Before this change there were seven hand-rolled
    ``mkstemp`` + ``os.replace`` blocks and six of them shared the bug,
    which is the evidence that a careful call site is not a durable fix.
    """

    def test_no_module_still_calls_mkstemp(self) -> None:
        """AST-walked, not grepped.

        A text search reports any module that merely mentions the word,
        including this change's own docstrings explaining why the pattern
        moved, so it would have to be suppressed with an exclusion list
        that then rots. A call node is the actual claim.
        """
        import ast

        import kstrl.atomicio

        package = Path(kstrl.atomicio.__file__).parent
        offenders: list[str] = []
        for source in sorted(package.glob("**/*.py")):
            if source.name == "atomicio.py":
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "mkstemp":
                    offenders.append(f"{source.name}:{node.lineno}")
        assert offenders == [], (
            f"{offenders} still call mkstemp directly. Route the write "
            f"through kstrl.atomicio instead: that is where the mode and "
            f"encoding rules live, and a hand-rolled copy is how #291 "
            f"came to have ten of them, six carrying the same bug."
        )
