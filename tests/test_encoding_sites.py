"""#320: what each fixed reader DOES when the file is not valid UTF-8.

The AST guard in ``tests/test_encoding_readers.py`` proves the handler is
written. It cannot prove the handler is right, and for the fail-closed
sites "right" is the whole point: refusing to spend against a total it
could not read, and reading a pause marker it could not decode as PAUSED
rather than as RUNNING.

So every test here writes a REAL invalid byte to a REAL file and drives
the real function. No mocks, no monkeypatched ``read_text``: #319's
measurement is that the defect lives in the interaction between the
stdlib decoder and the handler ladder, which a mock replaces rather than
exercises.

``BAD_BYTES`` is a latin-1 byte inside otherwise valid JSON, so every
site under test gets a file that opens, has the right permissions and the
right shape, and still cannot be decoded. That isolates the decode: an
unreadable-file test would pass against the OLD code too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from kstrl import fixtures as fixtures_mod
from kstrl import knowledge, verify
from kstrl.autonomy import AutonomyState
from kstrl.calibration import load_baseline
from kstrl.intake_github import ProcessedLedger
from kstrl.serve import ServeStateError, SpendLedger
from kstrl.workqueue import Queue

#: A byte no UTF-8 decoder accepts, inside a document that is otherwise
#: exactly what each reader expects.
BAD_BYTES = b'{"na\xefve": 1}\n'


def _write_bad(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BAD_BYTES)
    return path


#: The env that actually removes the utf-8 default, measured on CPython
#: 3.12.8. ``LC_ALL=C`` alone is NOT it: a C locale turns PEP 540 UTF-8
#: mode ON, so ``locale.getencoding()`` says ``US-ASCII`` while
#: ``sys.flags.utf8_mode`` is 1 and every read and write still decodes and
#: encodes utf-8. #344's review caught the PR body and a production
#: comment both naming the env that does not reproduce.
#:
#:     env                             getencoding  utf8_mode  a write of chr(233)
#:     (inherited)                     UTF-8        0          WROTE
#:     LC_ALL=C                        US-ASCII     1          WROTE
#:     LC_ALL=C LANG=C                 US-ASCII     1          WROTE
#:     LC_ALL=C PYTHONUTF8=0           US-ASCII     0          RAISED
#:     LC_ALL=C LANG=C PYTHONUTF8=0    US-ASCII     0          RAISED
NO_UTF8_DEFAULT = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}


def _ascii_child_env() -> dict[str, str] | None:
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


def _file_fixture() -> fixtures_mod.Fixture:
    """The one file fixture both halves of the gate test use, so the
    readable and the undecodable run cannot drift apart."""
    return fixtures_mod.Fixture(
        description="the artifact is readable",
        fixture_type="file",
        input_data={"path": "out.json"},
        expected={"exists": True, "contains": ["cafe"]},
    )


class TestTheMoneyPathFailsClosedOnADecodeToo:
    """``serve.SpendLedger`` refuses to spend against a total it could not
    read. Before #320 that refusal covered ``OSError`` and not the decode,
    so one non-utf-8 byte raised past the handler and out of the daemon.
    """

    def test_a_ledger_that_will_not_decode_raises_serve_state_error(
        self, isolate_kstrl_state: Path
    ) -> None:
        tmp_path = isolate_kstrl_state
        ledger = SpendLedger(tmp_path)
        ledger.read_state()  # first run, creates the control dir
        _write_bad(ledger.path)
        with pytest.raises(ServeStateError) as caught:
            ledger.read_state()
        message = str(caught.value)
        assert "not valid UTF-8" in message, message
        assert str(ledger.path) in message, message
        assert "Refusing to spend" in message, message

    def test_the_decode_message_is_not_the_permission_message(
        self, isolate_kstrl_state: Path
    ) -> None:
        """The two remedies differ: one is chmod, the other is re-save.
        A single widened clause would have told the operator to fix
        permissions on a file that opened perfectly well."""
        tmp_path = isolate_kstrl_state
        ledger = SpendLedger(tmp_path)
        ledger.read_state()
        _write_bad(ledger.path)
        with pytest.raises(ServeStateError) as caught:
            ledger.read_state()
        assert "permissions" not in str(caught.value)

    def test_the_cause_survives_so_the_byte_offset_is_not_lost(
        self, isolate_kstrl_state: Path
    ) -> None:
        tmp_path = isolate_kstrl_state
        ledger = SpendLedger(tmp_path)
        ledger.read_state()
        _write_bad(ledger.path)
        with pytest.raises(ServeStateError) as caught:
            ledger.read_state()
        assert isinstance(caught.value.__cause__, UnicodeDecodeError)


class TestThePauseMarkerFailsClosedOnADecodeToo:
    """``workqueue.pause_state`` had the same shape and the same argument
    written above it: #185 F7 fixed a broad ``except OSError`` that made a
    PermissionError read as RUNNING. The decode still escaped the handler
    that fix installed - not fail-open, but no answer at all, which is
    worse for a caller that has to choose.
    """

    def test_a_marker_that_will_not_decode_reads_as_paused(self, isolate_kstrl_state: Path) -> None:
        tmp_path = isolate_kstrl_state
        queue = Queue(tmp_path)
        assert queue.pause_state().paused is False
        _write_bad(queue.pause_path)
        state = queue.pause_state()
        assert state.paused is True
        assert "not valid UTF-8" in (state.reason or "")

    def test_is_paused_agrees_with_pause_state(self, isolate_kstrl_state: Path) -> None:
        """The property the daemon actually consults."""
        tmp_path = isolate_kstrl_state
        queue = Queue(tmp_path)
        queue.pause_state()
        _write_bad(queue.pause_path)
        assert queue.is_paused() is True

    def test_the_reason_is_not_the_unreadable_marker_message(
        self, isolate_kstrl_state: Path
    ) -> None:
        tmp_path = isolate_kstrl_state
        queue = Queue(tmp_path)
        queue.pause_state()
        _write_bad(queue.pause_path)
        assert "unreadable pause marker" not in (queue.pause_state().reason or "")


class TestTheGatesReportRatherThanCrash:
    """The mechanical gates return a failing result instead of raising,
    which is what makes a bad artifact a red check rather than a dead
    pipeline."""

    def test_the_self_critique_gate_fails_with_its_own_message(self, tmp_path: Path) -> None:
        progress = _write_bad(tmp_path / "progress.txt")
        result = verify.check_self_critique(progress)
        assert result.passed is False
        assert "not valid UTF-8" in result.message
        assert "Could not read progress file" not in result.message

    def test_a_file_fixture_fails_with_its_own_message(self, tmp_path: Path) -> None:
        _write_bad(tmp_path / "out.json")
        result = fixtures_mod.run_file_fixture(_file_fixture(), tmp_path)
        assert result.passed is False
        assert "not valid UTF-8" in result.message
        assert "Failed to read file" not in result.message

    def test_a_readable_file_fixture_still_passes(self, tmp_path: Path) -> None:
        """The other direction, so the clause above is not just refusing
        everything."""
        (tmp_path / "out.json").write_text('{"cafe": 1}\n', encoding="utf-8")
        assert fixtures_mod.run_file_fixture(_file_fixture(), tmp_path).passed is True


class TestTheDegradingReadersDegrade:
    """Sites whose contract is "never crash". Each promised that before
    #320 and none of them kept it."""

    def test_knowledge_read_facts_rejects_the_file_with_a_warning(self, tmp_path: Path) -> None:
        """Its docstring promises a corrupted file is "rejected with a
        RuntimeWarning, never a crash". The decode was a crash."""
        run_dir = tmp_path / "comp" / "run-1"
        run_dir.mkdir(parents=True)
        _write_bad(run_dir / "fact.md")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert knowledge.read_facts(tmp_path, "comp") == []
        assert any("not valid UTF-8" in str(w.message) for w in caught), [
            str(w.message) for w in caught
        ]

    def test_knowledge_prd_text_says_which_fault_it_hit(self, tmp_path: Path) -> None:
        prd = _write_bad(tmp_path / "prd.json")
        assert knowledge._read_prd_text(prd) == "(PRD is not valid UTF-8)"

    def test_knowledge_telemetry_degrades_to_empty(self, tmp_path: Path) -> None:
        _write_bad(tmp_path / knowledge._E8_TELEMETRY_RELATIVE_PATH)
        assert knowledge.read_dependency_scope_telemetry(tmp_path) == []

    def test_autonomy_state_degrades_to_level_one_and_says_why(
        self, isolate_kstrl_state: Path
    ) -> None:
        tmp_path = isolate_kstrl_state
        AutonomyState.load(tmp_path)
        _write_bad(AutonomyState.path_for(tmp_path))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            state = AutonomyState.load(tmp_path)
        assert state.level == 1
        assert "not valid UTF-8" in (state.degraded_reason or "")
        assert "unreadable" not in (state.degraded_reason or "")

    def test_the_github_processed_ledger_degrades_to_empty(self, isolate_kstrl_state: Path) -> None:
        tmp_path = isolate_kstrl_state
        ledger = ProcessedLedger(tmp_path)
        ledger.load()
        _write_bad(ledger.path)
        assert ProcessedLedger(tmp_path).load().contains("anything") is False

    def test_the_queue_journal_degrades_to_empty(self, tmp_path: Path) -> None:
        queue = Queue(tmp_path)
        queue.path.mkdir(parents=True, exist_ok=True)
        _write_bad(queue.journal_path)
        assert queue.journal_entries() == []

    def test_a_queue_item_with_an_undecodable_sidecar_is_rejected(self, tmp_path: Path) -> None:
        queue = Queue(tmp_path)
        queue.ensure_dirs()
        item_dir = queue.path / "queued" / "item-1"
        item_dir.mkdir(parents=True)
        _write_bad(item_dir / "meta.json")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert queue.items() == []
        assert any("not valid UTF-8" in str(w.message) for w in caught), [
            str(w.message) for w in caught
        ]


class TestTheRaisingReaders:
    """Sites that raise a domain error rather than degrading. The type is
    unchanged; only the message and the fact that it arrives at all."""

    def test_load_baseline_raises_value_error_naming_the_file(self, tmp_path: Path) -> None:
        path = _write_bad(tmp_path / "baseline.json")
        with pytest.raises(ValueError) as caught:
            load_baseline(path)
        assert "not valid UTF-8" in str(caught.value)
        assert str(path) in str(caught.value)
        assert "cannot read baseline" not in str(caught.value)

    def test_load_baseline_still_calls_a_syntax_error_a_syntax_error(self, tmp_path: Path) -> None:
        """Pins the handler ORDER. ``json.JSONDecodeError`` is a
        ``ValueError`` and ``UnicodeDecodeError`` is a ``ValueError``, and
        reversing the ladder would relabel every malformed baseline an
        encoding fault."""
        path = tmp_path / "baseline.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError) as caught:
            load_baseline(path)
        assert "cannot read baseline" in str(caught.value)
        assert "not valid UTF-8" not in str(caught.value)


class TestTheLocalePinnedReads:
    """The other half of the rule: a read that names no encoding decodes
    as the locale says, so the same bytes give two answers on two
    machines. These six named none."""

    def test_the_spec_reader_names_utf8(self, tmp_path: Path) -> None:
        from kstrl.decompose import load_spec_input

        spec = tmp_path / "spec.md"
        spec.write_text("a café spec\n", encoding="utf-8")
        assert "café" in load_spec_input(spec)

    def test_the_speckit_directory_reader_names_utf8(self, tmp_path: Path) -> None:
        from kstrl.decompose import load_spec_input

        (tmp_path / "spec.md").write_text("café spec\n", encoding="utf-8")
        (tmp_path / "plan.md").write_text("café plan\n", encoding="utf-8")
        got = load_spec_input(tmp_path)
        assert "café spec" in got and "café plan" in got

    def test_the_bad_patterns_scan_reads_source_as_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PEP 3120 makes utf-8 the source encoding, so this is the file's
        real encoding rather than a preference, and which
        ``SECRET_PATTERNS`` match depends on getting it right.

        The scan is DRIVEN, not simulated: an earlier draft of this test
        read the file with ``pathlib`` and asserted about the answer,
        which is a test of the standard library that passes whatever
        ``kstrl/verify.py`` does.
        """
        source = tmp_path / "m.py"
        source.write_text('# na\u00efve\nKEY = "AKIA' + "A" * 16 + '"\n', encoding="utf-8")
        monkeypatch.setattr(verify.git, "get_diff_names", lambda base, cwd: ["m.py"])
        found = verify.check_bad_patterns(tmp_path, "main")
        assert not found.passed
        assert found.details == ["m.py: possible secret/credential detected"]

    def test_the_failure_context_reader_names_utf8(self, tmp_path: Path) -> None:
        from kstrl.parsers import ParsedFailure, add_source_context

        source = tmp_path / "m.py"
        source.write_text("a = 1\nb = 'é'\nc = 3\n", encoding="utf-8")
        failure = ParsedFailure(file="m.py", line=2, rule_or_test="t", message="boom")
        add_source_context(failure, tmp_path, context_lines=1)
        assert "é" in failure.source_context

    def test_the_same_four_pass_where_the_default_encoding_is_not_utf8(self) -> None:
        """The discriminating half, and the reason the four above are not
        enough on their own.

        This test file runs in a UTF-8 locale, where a read that names no
        encoding still decodes utf-8 and every assertion above passes
        against the UNFIXED code. Re-running the four in a child whose
        default encoding is ASCII is what makes them measure something:
        measured, they pass as shipped and the child fails with any one
        of the four ``encoding="utf-8"`` arguments removed.

        The child is given this class and told to deselect this wrapper,
        so it cannot re-collect itself.
        """
        env = _ascii_child_env()
        if env is None:
            pytest.skip("this platform keeps a utf-8 default under LC_ALL=C PYTHONUTF8=0")
        node = f"{Path(__file__).name}::TestTheLocalePinnedReads"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                node,
                "-q",
                "-p",
                "no:cacheprovider",
                "-k",
                "not where_the_default_encoding_is_not_utf8",
            ],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        assert result.returncode == 0, (
            "the locale-pinned reads failed with no utf-8 default:\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )


class TestTheChildEnvIsTheOneThatReproduces:
    """A skip is not a control, and every test that needs an ASCII default
    skips itself when the platform will not give one.

    So dropping ``PYTHONUTF8`` from :data:`NO_UTF8_DEFAULT` turned five
    tests green-by-skipping rather than red. Measured as mutation M37 of
    #344's battery, and this is what closes it: the constant is checked
    against the MEASUREMENT that chose it, which no platform can skip.
    """

    def test_the_env_carries_the_variable_the_measurement_says_is_load_bearing(
        self,
    ) -> None:
        """``LC_ALL``/``LANG`` set the locale; ``PYTHONUTF8=0`` is what
        actually turns PEP 540 UTF-8 mode off, and the table on
        NO_UTF8_DEFAULT records both C-locale rows still encoding utf-8
        without it."""
        assert NO_UTF8_DEFAULT.get("PYTHONUTF8") == "0", (
            "the child env no longer disables PEP 540 UTF-8 mode, so every "
            "test that asks for an ASCII default will SKIP rather than fail. "
            f"Got {NO_UTF8_DEFAULT}."
        )
        assert NO_UTF8_DEFAULT.get("LC_ALL") == "C"


class TestTheWriteSideIsPinnedToo:
    """#291's rule is two-sided, and the read-only half of #320 left the
    other one open: a write whose encoding the locale picks makes kstrl
    the SOURCE of bytes its own readers cannot decode.

    ``kstrl/agents/logging.py`` was the live one. It tees raw agent
    output to a log through ``path.open("a")``, so one accented character
    in a model's reply raised ``UnicodeEncodeError`` and killed the run
    mid-stream. Driven here rather than asserted: the AST guard proves
    the argument is written, and only this proves it is the argument that
    matters.
    """

    #: One accented character, the whole defect. ``chr(233)`` rather than
    #: the literal so this file stays pure ASCII and cannot itself become
    #: a test of the reader's encoding.
    ACCENTED = chr(233)

    def test_the_agent_log_takes_a_non_ascii_line_where_the_default_is_ascii(
        self, tmp_path: Path
    ) -> None:
        """Measured: green as shipped, and the child raises
        ``UnicodeEncodeError`` with the ``encoding="utf-8"`` removed."""
        env = _ascii_child_env()
        if env is None:
            pytest.skip("this platform keeps a utf-8 default under LC_ALL=C PYTHONUTF8=0")
        driver = tmp_path / "drive.py"
        driver.write_text(
            "import pathlib, sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from kstrl.agents.logging import LoggingAgent\n"
            "class Fake:\n"
            "    name = 'fake'\n"
            "    final_message = None\n"
            "    usage_records = []\n"
            "    def run(self, prompt, cwd=None, timeout=None):\n"
            "        yield chr(233) + ' line'\n"
            f"log = pathlib.Path({str(tmp_path / 'agent.log')!r})\n"
            "list(LoggingAgent(Fake(), log).run('p'))\n"
            "assert chr(233) in log.read_text(encoding='utf-8')\n"
            "print('OK')\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK" in result.stdout

    def test_the_same_write_raises_where_the_encoding_is_left_to_the_locale(
        self, tmp_path: Path
    ) -> None:
        """The control, and the reason the test above measures anything:
        without it, a green child is also what a machine that ignores the
        env returns."""
        env = _ascii_child_env()
        if env is None:
            pytest.skip("this platform keeps a utf-8 default under LC_ALL=C PYTHONUTF8=0")
        driver = tmp_path / "control.py"
        driver.write_text(
            "import pathlib\n"
            f"log = pathlib.Path({str(tmp_path / 'bare.log')!r})\n"
            "try:\n"
            "    with log.open('a') as handle:\n"
            "        handle.write(chr(233))\n"
            "except UnicodeEncodeError:\n"
            "    print('RAISED')\n"
            "else:\n"
            "    print('WROTE')\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.stdout.strip() == "RAISED", (
            "the child env did not remove the utf-8 default, so the test above "
            f"is an assertion about nothing: {result.stdout}{result.stderr}"
        )


class TestTheFactoryRunLockReadsItsHolderBack:
    """The 21st site, and the one no census of read CALLS can find: the
    decode is at ``fp.read(64)`` on an ``"a+"`` handle, not at the
    ``open`` that produced it.
    """

    def test_a_lock_file_that_will_not_decode_still_refuses_the_second_run(
        self, isolate_kstrl_state: Path
    ) -> None:
        """The refusal is the point. Before #320 the ``UnicodeDecodeError``
        from reading the holder pid escaped ``except OSError`` and turned
        a clean "another run holds this" refusal into a traceback."""
        pytest.importorskip("fcntl")
        tmp_path = isolate_kstrl_state
        from kstrl.factory import FactoryLockHeldError, _acquire_run_lock
        from kstrl.statedir import state_dir
        from kstrl.ui.plain import PlainUI

        ui = PlainUI()
        first = _acquire_run_lock(tmp_path, ui=ui, force=False)
        try:
            (state_dir(tmp_path) / "factory.lock").write_bytes(b"\xe9\xe9\xe9\n")
            with pytest.raises(FactoryLockHeldError) as caught:
                _acquire_run_lock(tmp_path, ui=ui, force=False)
            assert "refusing to start a second factory run" in str(caught.value)
        finally:
            first.release()


def test_every_site_in_this_file_reads_bytes_it_wrote() -> None:
    """The corpus check for THIS file: ``BAD_BYTES`` must actually be
    undecodable, or every assertion above is about nothing.

    An earlier draft of a sibling test built its bytes with ``str.encode``,
    which produces valid utf-8, and would have passed against the unfixed
    code at every one of these sites.
    """
    with pytest.raises(UnicodeDecodeError):
        BAD_BYTES.decode("utf-8")
    assert json.loads(BAD_BYTES.decode("latin-1")) == {"naïve": 1}
