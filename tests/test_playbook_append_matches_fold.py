"""#529: no append can leave the playbook ledger in a state its own fold
refuses, and a ledger that is already refused has a way back.

Four ways an append used to land a line the fold refuses, each driven
through the real writer and read back through the real fold: an op the
fold's ``_apply`` refuses, two writers racing on one id, the tail a
writer killed mid-write leaves, and the pad the next append puts after
that tail. ``ks learn repair`` runs as a real process against a real
ledger. Every test points ``XDG_STATE_HOME`` at its own ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kstrl.appendio import appending
from kstrl.jsonread import read_json
from kstrl.playbook import (
    LessonStatus,
    Op,
    OpKind,
    PlaybookError,
    append_ops,
    ledger_path,
    load_playbook,
    playbook_dir,
    repair_ledger,
)
from kstrl.statedir import clear_xdg_state_home_cache
from tests.test_playbook_store import (
    AT,
    LATER,
    WRITERS,
    _kill_groups,
    _ks,
    _ledger_lines,
    _lesson,
    _line,
)


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "xdg"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    clear_xdg_state_home_cache()
    return home


# --- an op the fold refuses ------------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        Op(OpKind.ADD, "L1", LATER, lesson=_lesson("L1")),
        Op(OpKind.UPDATE, "L9", LATER, changes={"insight": "x"}),
        Op(OpKind.DEMOTE, "L9", LATER),
        Op(OpKind.RETIRE, "L9", LATER),
    ],
    ids=["second-add", "update-unknown", "demote-unknown", "retire-unknown"],
)
def test_an_op_the_fold_would_refuse_is_refused_before_it_is_written(xdg: Path, op: Op) -> None:
    """#529: the writer applies each op to the fold of the ledger as it
    stands, with the fold's own ``_apply``, so the line never lands and
    every later read still works."""
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    before = ledger_path().read_bytes()

    with pytest.raises(PlaybookError, match=rf"op 0\b.*'{op.lesson_id}'"):
        append_ops([op])

    assert ledger_path().read_bytes() == before
    assert [lesson.id for lesson in load_playbook().lessons] == ["L1"]


def test_a_batch_is_checked_in_order_and_lands_whole_or_not_at_all(xdg: Path) -> None:
    append_ops([Op(OpKind.ADD, "L2", AT, lesson=_lesson("L2")), Op(OpKind.DEMOTE, "L2", LATER)])
    before = ledger_path().read_bytes()

    with pytest.raises(PlaybookError, match=r"op 1\b.*'L3'"):
        append_ops(
            [
                Op(OpKind.ADD, "L3", AT, lesson=_lesson("L3")),
                Op(OpKind.ADD, "L3", LATER, lesson=_lesson("L3")),
            ]
        )

    assert ledger_path().read_bytes() == before
    assert load_playbook().lessons[0].status is LessonStatus.DEMOTED


_TORN = [
    pytest.param(0.5, id="half-a-record"),
    pytest.param(1.0, id="a-whole-record-without-its-newline"),
]


@pytest.mark.parametrize("fraction", _TORN)
def test_a_writer_killed_mid_write_leaves_a_ledger_the_fold_accepts(
    xdg: Path, tmp_path: Path, fraction: float
) -> None:
    """#529: a line is committed by its newline. The tail a killed writer
    leaves is not folded, and the next append voids it in the same write
    that appendio's pad turns it into a line, so no read is ever refused.
    The fragment is planted rather than produced by a real kill, because
    a kill cannot be timed to land mid-write; it is byte for byte what a
    short write leaves."""
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    record = _line(Op(OpKind.ADD, "L2", AT, lesson=_lesson("L2"))).encode("utf-8")
    fragment = record[: int(len(record) * fraction)]
    with ledger_path().open("ab") as handle:
        handle.write(fragment)

    torn = load_playbook()

    assert [lesson.id for lesson in torn.lessons] == ["L1"]
    assert (torn.line_count, torn.tail_bytes, torn.voided) == (1, len(fragment), ())

    append_ops([Op(OpKind.ADD, "L3", AT, lesson=_lesson("L3"))])
    healed = load_playbook()

    assert [lesson.id for lesson in healed.lessons] == ["L1", "L3"]
    assert (healed.line_count, healed.tail_bytes, healed.voided) == (4, 0, (2,))
    void = read_json(ledger_path().read_bytes().split(b"\n")[2].decode("utf-8"))
    assert void["op"] == "VOID" and void["line"] == 2
    assert void["sha256"] == hashlib.sha256(fragment).hexdigest()
    proc = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"^\s*voided:\s+1$", proc.stdout + proc.stderr, re.MULTILINE)


def _void_ledger(void: dict[str, Any]) -> bytes:
    add = _line(Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1")))
    return _ledger_lines(add, "not json", json.dumps(void))


def _void(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "op": "VOID",
        "line": 2,
        "sha256": hashlib.sha256(b"not json").hexdigest(),
        "at": AT,
        "reason": "r",
    }
    record.update(overrides)
    return record


def test_a_void_passes_over_the_line_it_names(xdg: Path) -> None:
    """The control for the refusals below: the well-formed VOID is honoured."""
    playbook_dir().mkdir(parents=True)
    ledger_path().write_bytes(_void_ledger(_void()))

    playbook = load_playbook()

    assert [lesson.id for lesson in playbook.lessons] == ["L1"]
    assert playbook.voided == (2,)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sha256": hashlib.sha256(b"other").hexdigest()}, "digest"),
        ({"line": 2}, "not before it"),
        ({"line": 0}, r"\.line"),
        ({"line": True}, r"\.line"),
        ({"sha256": "ABC"}, r"\.sha256"),
        ({"reason": ""}, "reason"),
    ],
    ids=["wrong-digest", "names-itself", "line-zero", "line-bool", "not-hex", "no-reason"],
)
def test_a_void_that_does_not_match_its_line_is_refused(
    xdg: Path, overrides: dict[str, Any], message: str
) -> None:
    """Each VOID here names line 1 unless it overrides that, so the only
    line that can be refused is the VOID itself, at line 2."""
    add = _line(Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1")))
    void = _void(line=1, sha256=hashlib.sha256(add.encode("utf-8")).hexdigest())
    void.update(overrides)
    playbook_dir().mkdir(parents=True)
    ledger_path().write_bytes(_ledger_lines(add, json.dumps(void)))

    with pytest.raises(PlaybookError, match=rf"line 2\b.*{message}"):
        load_playbook()


def test_a_void_that_names_a_void_takes_nothing_back(xdg: Path) -> None:
    """Voiding only removes. A VOID of a VOID line is accepted and the
    VOID it names still passes over its own line, so a line voided once
    is never folded again."""
    playbook_dir().mkdir(parents=True)
    first = json.dumps(_void())
    ledger_path().write_bytes(
        _void_ledger(_void())
        + _ledger_lines(
            json.dumps(_void(line=3, sha256=hashlib.sha256(first.encode("utf-8")).hexdigest()))
        )
    )

    playbook = load_playbook()

    assert [lesson.id for lesson in playbook.lessons] == ["L1"]
    assert playbook.voided == (2, 3)


def _torn_repair() -> bytes:
    """A repair that wrote VOIDs for lines 2 and 3 and was killed before
    the second one's newline: the second VOID is a whole record in the
    tail, and line 3 is committed with nothing voiding it."""
    add = _line(Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1")))
    dup = _line(Op(OpKind.ADD, "L1", LATER, lesson=_lesson("L1")))
    return _ledger_lines(
        add,
        dup,
        "not json",
        json.dumps(_void(sha256=hashlib.sha256(dup.encode("utf-8")).hexdigest())),
    ) + json.dumps(_void(line=3)).encode("utf-8")


def _torn_tail_void() -> bytes:
    """An append that padded a torn fragment into line 2 and was killed
    half way through the VOID naming it: line 2 is committed, its VOID
    is not."""
    add = _line(Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1")))
    fragment = add.encode("utf-8")[:40]
    void = json.dumps(_void(sha256=hashlib.sha256(fragment).hexdigest()))
    return _ledger_lines(add) + fragment + b"\n" + void[: len(void) // 2].encode("utf-8")


@pytest.mark.parametrize(
    "ledger",
    [
        pytest.param(_torn_repair, id="repair-torn-at-its-last-byte"),
        pytest.param(_torn_tail_void, id="append-torn-inside-its-tail-void"),
    ],
)
def test_learn_repair_heals_a_ledger_whose_own_void_was_torn(
    xdg: Path, tmp_path: Path, ledger: Callable[[], bytes]
) -> None:
    """#529: a write torn inside its own VOID payload leaves a ledger the
    fold refuses (the stated residual of a short write inside the one
    write). ``ks learn repair`` must bring it back, and running it must
    never leave a ledger that a second repair cannot read."""
    playbook_dir().mkdir(parents=True)
    ledger_path().write_bytes(ledger())

    assert _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain").returncode == 2
    repaired = _ks(tmp_path, xdg, "learn", "repair", "--ui", "plain")
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr

    shown = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert [lesson.id for lesson in load_playbook().lessons] == ["L1"]
    again = _ks(tmp_path, xdg, "learn", "repair", "--ui", "plain")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Nothing to repair" in again.stdout + again.stderr


# --- two writers on one id -------------------------------------------------

_SAME_ID_WRITER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from kstrl.playbook import Lesson, LessonStatus, Op, OpKind, append_ops

    ready, go = Path(sys.argv[1]), Path(sys.argv[2])
    lesson = Lesson(
        id="L1", section="s", keywords=(), issue="i", insight="n",
        active=True, used_count=0, helpful_count=0, harmful_count=0,
        neutral_count=0, created_at="t", updated_at="t",
        evidence=("r",), scope="universal", target_signature="x:y",
        status=LessonStatus.ACTIVE,
    )
    ready.touch()
    deadline = time.monotonic() + 60
    while not go.exists():
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.001)
    append_ops([Op(OpKind.ADD, "L1", "t", lesson=lesson)])
    """
)


def _same_id_writer(tmp_path: Path, xdg: Path, name: str, go: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", _SAME_ID_WRITER, str(tmp_path / f"ready-{name}"), str(go)],
        env={**os.environ, "XDG_STATE_HOME": str(xdg)},
        cwd=tmp_path,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_ready(tmp_path: Path, names: list[str], procs: list[subprocess.Popen[bytes]]) -> None:
    deadline = time.monotonic() + 60
    while not all((tmp_path / f"ready-{name}").exists() for name in names):
        assert time.monotonic() < deadline, "a writer never became ready"
        assert all(p.poll() is None for p in procs), "a writer exited before the barrier"
        time.sleep(0.01)


def test_an_add_that_waited_for_the_lock_sees_the_line_written_under_it(
    xdg: Path, tmp_path: Path
) -> None:
    """#529, deterministic: this test holds the ledger's lock, releases a
    writer that ADDs L1, writes its own ADD of L1 while the writer waits,
    then lets go. A writer that checked the ledger before it took the
    lock would append a second ADD; one that checks under the lock sees
    this line and is refused. The pause only has to cover the writer
    reaching its read, and a slow writer can only make this pass."""
    playbook_dir().mkdir(parents=True)
    go = tmp_path / "go"
    procs: list[subprocess.Popen[bytes]] = []
    try:
        with appending(ledger_path(), lock=True) as handle:
            procs.append(_same_id_writer(tmp_path, xdg, "a", go))
            _wait_ready(tmp_path, ["a"], procs)
            go.touch()
            time.sleep(1.0)
            handle.write(
                (_line(Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))) + "\n").encode("utf-8")
            )
        _out, err = procs[0].communicate(timeout=60)
    finally:
        _kill_groups(procs)

    assert procs[0].returncode != 0, "the waiting writer appended a second ADD of L1"
    assert b"already exists" in err, err.decode("utf-8", "replace")
    playbook = load_playbook()
    assert (playbook.line_count, [lesson.id for lesson in playbook.lessons]) == (1, ["L1"])


def test_writers_racing_on_one_id_land_it_once(xdg: Path, tmp_path: Path) -> None:
    """#529, the race as it happens: six writers released at once, each
    adding L1. Exactly one lands; the fold reads the ledger afterwards."""
    go = tmp_path / "go"
    names = [str(n) for n in range(WRITERS)]
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.extend(_same_id_writer(tmp_path, xdg, name, go) for name in names)
        _wait_ready(tmp_path, names, procs)
        go.touch()
        results = [proc.communicate(timeout=120) for proc in procs]
    finally:
        _kill_groups(procs)

    assert sorted(proc.returncode for proc in procs) == [0] + [1] * (WRITERS - 1)
    assert sum(b"already exists" in err for _out, err in results) == WRITERS - 1
    playbook = load_playbook()
    assert (playbook.line_count, [lesson.id for lesson in playbook.lessons]) == (1, ["L1"])


# --- the way back ----------------------------------------------------------


def test_learn_repair_voids_every_refused_line_and_the_playbook_reads_again(
    xdg: Path, tmp_path: Path
) -> None:
    """#529: a ledger an earlier writer (or a hand edit) left refused.
    ``ks learn repair`` appends one VOID per refused line, each naming
    the line by number and digest and carrying the fold's reason, and
    the bytes it voids stay where they were."""
    playbook_dir().mkdir(parents=True)
    lines = [
        _line(Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))),
        _line(Op(OpKind.ADD, "L1", LATER, lesson=_lesson("L1"))),
        "not json",
        _line(Op(OpKind.DEMOTE, "L9", LATER)),
        _line(Op(OpKind.UPDATE, "L1", LATER, changes={"insight": "second wording"})),
    ]
    before = _ledger_lines(*lines) + b'{"op": "ADD"'
    ledger_path().write_bytes(before)

    refused = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "ks learn repair" in refused.stdout + refused.stderr

    repaired = _ks(tmp_path, xdg, "learn", "repair", "--ui", "plain")

    output = repaired.stdout + repaired.stderr
    assert repaired.returncode == 0, output
    for expected in ("line 2", "already exists", "line 3", "not a JSON record", "line 4", "'L9'"):
        assert expected in output, output
    after = ledger_path().read_bytes()
    assert after.startswith(before)
    raw_lines = after.split(b"\n")
    voids = [read_json(line.decode("utf-8")) for line in raw_lines[6:] if line]
    assert [(v["op"], v["line"]) for v in voids] == [
        ("VOID", 6),
        ("VOID", 2),
        ("VOID", 3),
        ("VOID", 4),
    ]
    for void in voids:
        assert void["sha256"] == hashlib.sha256(raw_lines[void["line"] - 1]).hexdigest()

    shown = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert "second wording" in shown.stdout + shown.stderr
    assert re.search(r"^\s*voided:\s+4$", shown.stdout + shown.stderr, re.MULTILINE)

    again = _ks(tmp_path, xdg, "learn", "repair", "--ui", "plain")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Nothing to repair" in again.stdout + again.stderr
    assert ledger_path().read_bytes() == after


def test_repair_leaves_a_missing_ledger_missing(xdg: Path) -> None:
    assert repair_ledger() == ()
    assert not playbook_dir().exists()
