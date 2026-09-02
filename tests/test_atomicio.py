"""#291: an atomic write must not change the mode or guess the encoding.

Every assertion here runs against a real filesystem and reads the mode
back with ``stat``. Mocking the write would only pin that we called the
functions we call: the defect was that the real bytes on the real disk
came out 0600, so a mock cannot see it.

Why the defects existed and why one helper owns them now is argued once,
in ``kstrl/atomicio.py``'s module docstring, and deliberately not
restated here.

Related coverage elsewhere, so neither gets deleted as a duplicate:
``tests/test_prompt_upgrade.py::test_upgrade_preserves_the_mode_it_found``
is the end-to-end twin of the mode tests below, through
``init_cmd._atomic_replace``; ``tests/test_knowledge.py::
test_write_atomic_no_partial_files`` covers ``write_facts``, which is
not in ``WRITERS``.
"""

from __future__ import annotations

import ast
import errno
import inspect
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import kstrl.atomicio
from kstrl.atomicio import atomic_write_json, atomic_write_text
from kstrl.init_cmd import _atomic_replace
from kstrl.workqueue import atomic_write
from tests.helpers import astwalk

#: A string whose utf-8 and latin-1 encodings differ, so a test that
#: round-trips it through an unpinned encoding fails rather than passing
#: by accident on an ASCII payload.
NON_ASCII = "naive cafe: éèü £€ 你好 \U0001f600"


def run_under_c_locale(tmp_path: Path, body: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a child interpreter under a POSIX/ASCII locale.

    ``locale.getpreferredencoding`` is read at interpreter start, so
    setting LC_ALL inside this process would change nothing: the only way
    to make a claim about locale independence is to be a different
    process. PYTHONUTF8 and PYTHONCOERCECLOCALE are switched off because
    modern CPython would otherwise quietly rescue the very thing under
    test, and the result is what a minimal container gives you.

    The script is written as PURE ASCII and the payload is embedded in it
    with ``ascii()`` rather than passed in argv. An earlier version used
    ``python -c`` with the text inline and died on Linux CI with "Unable
    to decode the command from the command line", failing on exactly the
    condition it exists to exercise; macOS never caught it because its
    argv is UTF-8 whatever the locale says. ``encoding="ascii"`` on the
    write is what keeps that true rather than hoped for.

    The child's stdout is ASCII too, so a caller that wants to see a
    non-ASCII value back must print ``ascii(value)``.
    """
    repo_root = Path(kstrl.atomicio.__file__).parent.parent
    script = tmp_path / "under_c_locale.py"
    script.write_text(
        "import sys\n" + f"sys.path.insert(0, {ascii(str(repo_root))})\n" + body,
        encoding="ascii",
    )
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
        },
    )


def _mode(path: Path) -> int:
    """Permission bits of ``path`` itself, never a symlink target.

    ``lstat`` to match ``tests/test_prompt_upgrade.py``: two helpers of
    the same name in one suite disagreeing about symlinks is how a later
    reader gets misled.
    """
    return stat.S_IMODE(path.lstat().st_mode)


#: Every writer that goes through the shared helper, as
#: (name, callable taking (path, text)). Parametrised rather than written
#: out per module because the whole point of #291 is that these must not
#: be allowed to drift apart again.
WRITERS: list[tuple[str, Callable[[Path, str], None]]] = [
    ("atomicio.atomic_write_text", atomic_write_text),
    ("atomicio.atomic_write_json", lambda p, t: atomic_write_json(p, {"t": t})),
    ("workqueue.atomic_write", atomic_write),
]

#: The subset that writes the caller's text through unchanged. The JSON
#: writer escapes non-ASCII on its way past, so a "these bytes are on
#: disk" assertion is not a statement about it.
TEXT_WRITERS = [w for w in WRITERS if "json" not in w[0]]


@pytest.mark.parametrize("name,write", WRITERS, ids=[w[0] for w in WRITERS])
class TestModeSurvivesTheWrite:
    @pytest.mark.parametrize("original", [0o600, 0o640, 0o644, 0o664, 0o666])
    def test_whatever_mode_the_operator_chose_is_what_survives(
        self, name: str, write: Callable[[Path, str], None], original: int, tmp_path: Path
    ) -> None:
        """The #291 defect itself, over the range that matters.

        0o644 is the case the issue was filed about: before the fix every
        one of these turned it into 0o600. 0o600 is in the list for the
        other direction, because preserving a mode means a caller that
        deliberately tightened a file does not get it loosened either.
        """
        target = tmp_path / "tracked.json"
        target.write_text("{}\n", encoding="utf-8")
        os.chmod(target, original)

        write(target, "hello")

        assert _mode(target) == original, f"{name} did not preserve {oct(original)}"

    def test_a_new_file_lands_where_a_plain_write_would_have(
        self, name: str, write: Callable[[Path, str], None], tmp_path: Path
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
        write(target, "hello")

        assert _mode(target) == _mode(reference), (
            f"{name} created a new file at {oct(_mode(target))}, but a plain "
            f"write in the same directory produced {oct(_mode(reference))}"
        )

    def test_the_temp_file_leaves_nothing_behind(
        self, name: str, write: Callable[[Path, str], None], tmp_path: Path
    ) -> None:
        target = tmp_path / "tracked.json"
        write(target, "hello")
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
    @pytest.mark.parametrize("name,write", TEXT_WRITERS, ids=[w[0] for w in TEXT_WRITERS])
    def test_the_bytes_on_disk_are_utf8(
        self, name: str, write: Callable[[Path, str], None], tmp_path: Path
    ) -> None:
        """The text writers put the characters on disk as themselves.

        ``atomic_write_json`` is not in this table because it escapes
        non-ASCII before the text writer ever sees it, so there are no
        raw utf-8 bytes to find. Its encoding is pinned by
        ``test_json_is_written_utf8_with_one_trailing_newline`` instead,
        which asserts the stronger property that its output is pure
        ASCII.
        """
        target = tmp_path / "text.md"
        write(target, NON_ASCII)
        assert NON_ASCII.encode("utf-8") in target.read_bytes(), f"{name} did not write utf-8"

    def test_json_is_written_utf8_with_one_trailing_newline(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.json"
        atomic_write_json(target, {"k": NON_ASCII})

        raw = target.read_bytes()
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")
        # ASCII on disk, because ensure_ascii is left at its default: the
        # document is then readable under any locale rather than only
        # under one that can decode utf-8. See atomic_write_json.
        assert all(b < 128 for b in raw), "JSON output should be pure ASCII"
        assert json.loads(raw.decode("ascii"))["k"] == NON_ASCII
        assert json.loads(raw.decode("utf-8"))["k"] == NON_ASCII

    def test_the_bytes_do_not_depend_on_the_locale(self, tmp_path: Path) -> None:
        """The claim an in-process test cannot make.

        ``locale.getpreferredencoding`` is read at interpreter start, so
        setting LC_ALL here would change nothing. This runs a second
        interpreter under a POSIX/ASCII locale with PYTHONCOERCECLOCALE
        and PYTHONUTF8 disabled, which is what a minimal container gives
        you, and requires byte-for-byte identical output.

        How the payload reaches the child, and why, is in
        ``run_under_c_locale``.
        """
        target = tmp_path / "from_c_locale.txt"
        result = run_under_c_locale(
            tmp_path,
            "from pathlib import Path\n"
            "from kstrl.atomicio import atomic_write_text\n"
            f"atomic_write_text(Path(sys.argv[1]), {ascii(NON_ASCII)})\n",
            str(target),
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
        # The first two draws both land on a name already taken; the
        # third is fresh. A helper that opened without O_EXCL would
        # truncate somebody else's file instead of drawing again.
        names = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
        monkeypatch.setattr(kstrl.atomicio.secrets, "token_hex", lambda _n: next(names))

        target = tmp_path / "doc.txt"
        squatter = tmp_path / ".doc.txt-aaaaaaaa.tmp"
        squatter.write_text("not mine to touch", encoding="utf-8")

        atomic_write_text(target, "hello")

        assert target.read_text(encoding="utf-8") == "hello"
        assert squatter.read_text(encoding="utf-8") == "not mine to touch"

    def test_exhausting_every_name_raises_instead_of_looping_forever(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kstrl.atomicio.secrets, "token_hex", lambda _n: "collide")
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
        for func in (atomic_write_text, atomic_write_json):
            params = inspect.signature(func).parameters
            assert "mode" not in params, (
                f"{func.__name__} grew a mode parameter; #291's argument "
                f"that there is one correct behaviour, not two, needs "
                f"revisiting before a caller can reach the other one"
            )


# --- nobody hand-rolls the pattern again, in two layers -------------------
#
# LAYER 1, the census, counts every node in ``kstrl/`` that writes
# ``mkstemp`` anywhere the AST can hold a string. A module cannot call a
# function it never names, so a copy in any shape appears here first, and
# it enumerates neither node types nor fields. Round 1 had NO positive
# control: measured, changing its match string to ``mkstemp_xyz`` left
# the file green.
#
# LAYER 2, :func:`_mkstemp_calls`, resolves the callee and names the
# offending site, which "atomicio.py names mkstemp once more than it did"
# cannot. It decides ``from tempfile import mkstemp as _mk``, which round
# 1 matched on the last segment and so missed entirely.

#: The call #291 removed ten copies of, and the predicate that nets it.
MKSTEMP = "tempfile.mkstemp"
SPELLS_MKSTEMP = astwalk.spells("mkstemp")

#: Every module in ``kstrl/`` that spells ``mkstemp``, and how many
#: times. Empty, and meant to stay that way: a row here is a hand-rolled
#: temp file, and the diff that adds one is where somebody says why
#: ``kstrl.atomicio`` will not do.
EXPECTED_MKSTEMP_SPELLINGS: dict[str, int] = {}

#: Calls this walk cannot name at all, because the AST holds no
#: identifier to read: a callee looked up in a table, or returned by a
#: function. Pinned rather than dropped, because "could not decide" and
#: "decided it is fine" are the two answers #324 exists to keep apart.
#: Layer 1 is what covers them: a table of writers still spells the name.
#: Keyed by module and expression, not by line: none of the four is in a
#: file this guard is about, so a line here fails on a stranger's edit.
EXPECTED_UNDECIDED_CALLS: tuple[str, ...] = (
    "gateparse.py TOOL_PARSERS[chosen]",
    "gateparse.py TOOL_PARSERS[name]",
    "tui/app.py initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py initial_screens_for_kind(kind, observe_only=True)",
)


def _mkstemp_calls(sources: list[Path]) -> astwalk.Sites:
    """Every call to ``tempfile.mkstemp``, and every call not decidable.

    ``astwalk.calls_to`` resolves the callee: ``tempfile.mkstemp``,
    ``import tempfile as _t``, ``from tempfile import mkstemp``, the same
    renamed, and a rebind at any chain length. Round 1 compared the last
    segment to ``"mkstemp"``, so every alias walked past it.

    :func:`_named_mkstemp` is unioned in rather than dropped, and that is
    not belt-and-braces: measured, ``calls_to`` reports a DOTTED callee
    whose head it never saw bound as neither seen nor undecided, so
    planting ``tempfile.mkstemp(...)`` in a module with no ``import
    tempfile`` left this layer green. Round 1 caught that shape, and a
    migration that narrows a guard is the defect #324 records.

    Sites are keyed on ``label:lineno`` INTERNALLY, so one call both
    passes report is one row; the caller drops the line before pinning.
    ``astwalk.label``, not ``Path.name``: ten basenames occur twice in
    ``kstrl/`` and a message naming a file the reader cannot find is
    worse than none.
    """
    seen: dict[str, str] = {}
    undecided: list[str] = []
    for source_file in sources:
        tree = astwalk.parsed(source_file)
        where = astwalk.label(source_file)
        found = astwalk.calls_to(
            tree, {MKSTEMP}, where=where, module=astwalk.module_name(source_file)
        )
        undecided.extend(found.undecided)
        for site in (*found.seen, *_named_mkstemp(tree, where)):
            seen.setdefault(site.split(" ", 1)[0], site)
    return astwalk.Sites(tuple(sorted(seen.values())), tuple(sorted(undecided)))


def _named_mkstemp(tree: ast.Module, where: str) -> list[str]:
    """Calls whose last identifier is ``mkstemp``, whatever precedes it.

    Round 1's whole net, kept as half of layer 2 so the migration cannot
    lose a site. ``astwalk.leaf_name`` answers what a callee is called.
    """
    return [
        f"{where}:{node.lineno} {ast.unparse(node.func)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and astwalk.leaf_name(node.func) == "mkstemp"
    ]


class TestEveryCopyOfThePatternWasMigrated:
    """The mechanised half of #291's answer to "helper or four call sites".

    A helper only removes a class of defect if the copies actually go
    away, and stay away. Ten hand-rolled ``mkstemp`` + ``os.replace``
    blocks existed before this change and nine carried the mode bug,
    which is the evidence that a careful call site is not a durable fix.
    """

    def test_no_module_in_the_package_even_names_mkstemp(self) -> None:
        """Layer 1, the net: pin every spelling of the name itself.

        A module cannot call a function it never names, so a hand-rolled
        copy has to change this dict whatever shape the call takes: an
        attribute, a bare name, an import alias, a dispatch table keyed
        by the string, a ``getattr`` whose name folds. An exact count of
        spellings has no aliasing to be wrong about and no shape list to
        be incomplete. ``control`` is the piece round 1 had no equivalent
        of: without it an empty inventory and a net that stopped looking
        are the same green.
        """
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=SPELLS_MKSTEMP,
            expected=EXPECTED_MKSTEMP_SPELLINGS,
            control="import tempfile\nfd, path = tempfile.mkstemp(dir=str(target.parent))\n",
            message=(
                "A module in kstrl/ names mkstemp. Route the write through "
                "kstrl.atomicio instead: that is where the mode and encoding rules "
                "live, and a hand-rolled copy is how #291 came to have ten of them, "
                "nine downgrading the mode."
            ),
        )

    def test_no_module_still_calls_mkstemp(self) -> None:
        """Layer 2, the message: name the offending line and the fix."""
        astwalk.assert_sites(
            _mkstemp_calls(astwalk.package_sources()).without_line_numbers(),
            seen=(),
            undecided=EXPECTED_UNDECIDED_CALLS,
            message=(
                "These still call mkstemp directly. Route the write through "
                "kstrl.atomicio instead: that is where the mode and encoding rules "
                "live, and a hand-rolled copy is how #291 came to have ten of them, "
                "nine downgrading the mode."
            ),
        )


class TestTheMigrationNetCatchesWhatItClaims:
    """The net's own reach, measured rather than asserted in a docstring.

    Round 1 had no positive control at all: its matcher was never fed
    source it was supposed to flag, so switching it off was invisible.
    """

    @staticmethod
    def _spelled(source: str) -> int:
        return sum(1 for node in ast.walk(astwalk.parse(source)) if SPELLS_MKSTEMP(node))

    @staticmethod
    def _called(source: str) -> tuple[str, ...]:
        return astwalk.calls_to(astwalk.parse(source), {MKSTEMP}).seen

    @pytest.mark.parametrize(
        "body",
        [
            "import tempfile\ntempfile.mkstemp(dir=d)\n",
            "import tempfile as _t\n_t.mkstemp(dir=d)\n",
            "from tempfile import mkstemp\nmkstemp(dir=d)\n",
            "from tempfile import mkstemp as _mk\n_mk(dir=d)\n",
            "import tempfile\n_mk = tempfile.mkstemp\n_mk(dir=d)\n",
        ],
        ids=["attribute", "module-alias", "from-import", "import-alias", "rebind"],
    )
    def test_every_way_of_reaching_mkstemp_is_caught(self, body: str) -> None:
        """Both layers, on the five spellings of one call. The import
        alias is the one round 1 provably missed: it compared the
        callee's last segment, and ``_mk`` is not ``mkstemp``."""
        assert self._spelled(body), "layer 1 missed it"
        assert self._called(body), "layer 2 missed it"

    def test_an_attribute_call_is_caught_by_the_net(self) -> None:
        """``self.mkstemp()`` resolves to nothing this walk can name.
        ``Attribute.attr`` is a string field, so layer 1 counts it."""
        assert self._spelled("self.mkstemp(dir=d)\n")

    def test_an_assembled_name_is_caught_by_the_net(self) -> None:
        """What somebody writes to get past a string search.
        ``astwalk.folded_str`` decides it, so the census counts it."""
        assert self._spelled('import tempfile\ngetattr(tempfile, "mk" + "stemp")(dir=d)\n')

    def test_prose_naming_mkstemp_is_not_a_hit(self) -> None:
        """Why the census tests EQUALITY: ``kstrl/atomicio.py``'s own
        docstring argues about mkstemp, and a substring search would need
        a suppression list that rots."""
        assert not self._spelled('"""Never hand-roll mkstemp plus os.replace."""\n')

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_name_the_interpreter_builds_is_a_known_miss(self) -> None:
        """Layer 1's residual, stated rather than implied.

        A name only the interpreter can produce folds to ``None``, so the
        census cannot count it. Layer 2 does not miss it silently:
        measured, the callee is a ``Call`` with no identifier to read, so
        it lands in ``undecided`` and the pinned tuple moves. Foldable
        assembly is not here either: the test above measures that half.
        """
        astwalk.blind_spot(
            self._spelled,
            'import tempfile\ngetattr(tempfile, "".join(["mk", "stemp"]))(dir=d)\n',
        )


class TestTheReadSideNamesTheSameEncoding:
    """#291 round two: pinning only the WRITE side made files unreadable.

    ``ensure_ascii=False`` turns a JSON document that used to be pure
    ASCII into raw utf-8 bytes. Every reader that opened it with the
    LOCALE codec then failed on the first non-ASCII character, on a file
    the previous release read fine. Measured before the fix, under
    ``LC_ALL=C`` on a manifest holding one curly quote:
    ``UnicodeDecodeError: 'ascii' codec can't decode byte 0xe2``.

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so it
    was not caught by the ``(json.JSONDecodeError, OSError)`` handlers
    that exist to fail closed; it propagated out of
    ``check_snapshot_regression`` instead.

    A round trip is the only honest test of this: pinning the write side
    and then asserting on the write side proves nothing about a reader.
    """

    #: One curly quote is all it takes, and it is exactly what an LLM
    #: writes into a component description or an acceptance criterion.
    CURLY = "the operator\u2019s \u00e9\u00e8\u00fc \u2713"

    #: These write the file as RAW utf-8 rather than through
    #: ``atomic_write_json``, deliberately. kstrl's own writer leaves
    #: ``ensure_ascii`` at its default and so emits pure ASCII, which
    #: every locale can read whether or not the reader named an
    #: encoding: a round trip through it would therefore pass with the
    #: pins reverted and prove nothing. The case these defend is a file
    #: somebody hand-edited, or an agent rewrote, into real utf-8, which
    #: for a git-tracked PRD or manifest is an ordinary thing to happen.

    def _probe(self, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
        """``run_under_c_locale`` with the directory as the child's argv."""
        return run_under_c_locale(tmp_path, body, str(tmp_path))

    def test_a_manifest_written_here_loads_under_the_c_locale(self, tmp_path: Path) -> None:
        target = tmp_path / "manifest.json"
        target.write_bytes(
            json.dumps(
                {
                    "version": "1",
                    "specFile": "spec.md",
                    "projectName": "p",
                    "baseBranch": "main",
                    "singlePr": False,
                    "components": [
                        {
                            "id": "comp-a",
                            "title": "T",
                            "description": self.CURLY,
                            "dependencies": [],
                            "prdPath": "p.json",
                            "branchName": "b",
                        }
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
        )
        assert not all(b < 128 for b in target.read_bytes()), (
            "the payload must actually be non-ASCII on disk, or this test "
            "cannot detect a locale-dependent reader"
        )

        result = self._probe(
            tmp_path,
            "from pathlib import Path\n"
            "from kstrl.manifest import Manifest\n"
            "m = Manifest.load(Path(sys.argv[1]) / 'manifest.json')\n"
            "print(ascii(m.components[0].description))\n",
        )
        assert result.returncode == 0, result.stderr
        assert ascii(self.CURLY) in result.stdout

    def test_a_prd_written_here_loads_under_the_c_locale(self, tmp_path: Path) -> None:
        (tmp_path / "prd.json").write_bytes(
            json.dumps(
                {
                    "branchName": "b",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": self.CURLY,
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": False,
                            "notes": "",
                        }
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
        )
        result = self._probe(
            tmp_path,
            "from pathlib import Path\n"
            "from kstrl.prd import PRD\n"
            "p = PRD.load(Path(sys.argv[1]) / 'prd.json')\n"
            "print(ascii(p.user_stories[0].title))\n",
        )
        assert result.returncode == 0, result.stderr
        assert ascii(self.CURLY) in result.stdout

    def test_a_fixture_snapshot_is_readable_under_the_c_locale(self, tmp_path: Path) -> None:
        """``check_snapshot_regression`` fails CLOSED on an unreadable
        snapshot, so a decode error there did not even surface as one: it
        escaped the handler entirely, because ``UnicodeDecodeError`` is a
        ``ValueError`` and the handler names ``OSError``."""
        (tmp_path / "comp-a.json").write_bytes(
            json.dumps(
                {
                    "component_id": "comp-a",
                    "fixture_count": 0,
                    "entries": [],
                    "note": self.CURLY,
                },
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
        )
        result = self._probe(
            tmp_path,
            "from pathlib import Path\n"
            "from kstrl.fixtures import check_snapshot_regression\n"
            "print('REGRESSIONS:', check_snapshot_regression('comp-a', [], Path(sys.argv[1])))\n",
        )
        assert result.returncode == 0, result.stderr
        assert "REGRESSIONS: []" in result.stdout


class TestTheDescriptorIsOwnedBeforeAnythingCanFail:
    """#291 round two: a failing ``fchmod`` leaked the raw descriptor.

    Why the descriptor is handed to a context manager first is argued in
    ``kstrl/atomicio.py``. These pin the count it was measured at, and
    the empty-temp property the reordering could have broken.
    """

    def _open_fd_count(self) -> int:
        return len(os.listdir("/dev/fd"))

    def test_a_failing_fchmod_leaks_no_descriptors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "tracked.json"
        target.write_text("original\n", encoding="utf-8")
        os.chmod(target, 0o644)

        def refuse(fd: int, mode: int) -> None:
            raise PermissionError("fchmod not supported on this filesystem")

        monkeypatch.setattr(kstrl.atomicio.os, "fchmod", refuse)
        before = self._open_fd_count()
        for _ in range(20):
            with pytest.raises(PermissionError):
                atomic_write_text(target, "new")
        leaked = self._open_fd_count() - before
        monkeypatch.undo()

        assert leaked == 0, f"{leaked} descriptors leaked across 20 failed writes"
        assert target.read_text(encoding="utf-8") == "original\n"
        assert _mode(target) == 0o644
        assert [p.name for p in tmp_path.iterdir()] == ["tracked.json"]

    def test_the_mode_is_still_pinned_before_any_byte_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moving ``fchmod`` inside the ``with`` must not open a window in
        which a private file's contents sit at a looser mode."""
        target = tmp_path / "secret.txt"
        target.write_text("old", encoding="utf-8")
        os.chmod(target, 0o600)

        sizes: list[int] = []
        real_fchmod = kstrl.atomicio.os.fchmod

        def record(fd: int, mode: int) -> None:
            sizes.append(os.fstat(fd).st_size)
            real_fchmod(fd, mode)

        monkeypatch.setattr(kstrl.atomicio.os, "fchmod", record)
        atomic_write_text(target, "brand new secret")
        monkeypatch.undo()

        assert sizes == [0], (
            f"the temp file held {sizes} bytes when its mode was pinned; it "
            f"must be empty, or a 0600 file's contents are briefly readable"
        )
        assert _mode(target) == 0o600


class TestReplaceRefusesAMissingTargetWithoutRenamingTheError:
    """#291 round two, and the one finding this pushes back on.

    ``init_cmd._atomic_replace`` guards with ``target.is_file()``. Review
    read that as swallowing every ``OSError``, so an ``EACCES`` on the
    parent directory would be reported as "the caller is wrong".
    Measured: it does not. ``pathlib`` ignores only ENOENT, ENOTDIR,
    EBADF and ELOOP, so ``is_file()`` PROPAGATES ``PermissionError`` and
    the real errno survives, on every Python this project supports
    (>=3.11). Pinned here so it stays true rather than staying argued.
    """

    def test_a_missing_target_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="expects an existing file"):
            _atomic_replace(tmp_path / "never-created.md", "x")
        assert not (tmp_path / "never-created.md").exists(), (
            "a refusal must not leave behind the file it refused to write"
        )

    def test_an_unreadable_parent_keeps_its_own_errno(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        target = locked / "prompt.md"
        target.write_text("hi\n", encoding="utf-8")
        os.chmod(locked, 0o000)
        try:
            if os.access(locked, os.X_OK):
                pytest.skip("running as root: permission bits are not enforced")
            with pytest.raises(PermissionError) as caught:
                _atomic_replace(target, "x")
            assert caught.value.errno == errno.EACCES
        finally:
            os.chmod(locked, 0o700)
