"""The global playbook store: an append-only op ledger (#509, slice 7 of #217).

Every test points ``XDG_STATE_HOME`` at its own ``tmp_path`` through the
``xdg`` fixture, so nothing here can reach the operator's real state
directory. The concurrent-append test runs six real processes against a
real file, because the loss it guards against (a whole document
rewritten per operation) only exists when writers actually overlap.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from kstrl.config import ConfigError
from kstrl.playbook import (
    LearningConfig,
    Lesson,
    LessonStatus,
    Op,
    OpKind,
    PlaybookError,
    append_ops,
    contribute,
    ledger_path,
    load_playbook,
    playbook_dir,
)
from kstrl.statedir import clear_xdg_state_home_cache
from tests.helpers.procs import kill_group

AT = "2026-09-25T00:00:00Z"
LATER = "2026-09-26T00:00:00Z"
RUN_ID = "factory-20260925-101010.000000-abcdef"


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "xdg"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    clear_xdg_state_home_cache()
    return home


def _lesson(lesson_id: str, **overrides: Any) -> Lesson:
    fields: dict[str, Any] = {
        "id": lesson_id,
        "section": "verification",
        "keywords": ("mypy",),
        "issue": "untyped helper returns Any",
        "insight": "annotate the helper's return type before calling it",
        "active": True,
        "used_count": 0,
        "helpful_count": 0,
        "harmful_count": 0,
        "neutral_count": 0,
        "created_at": AT,
        "updated_at": AT,
        "evidence": (RUN_ID, "mypy:no-any-return"),
        "scope": "python",
        "target_signature": "mypy:no-any-return",
        "status": LessonStatus.ACTIVE,
    }
    fields.update(overrides)
    return Lesson(**fields)


def _ledger_lines(*lines: str) -> bytes:
    return "".join(line + "\n" for line in lines).encode("utf-8")


def test_the_store_lives_under_xdg_state_home(xdg: Path) -> None:
    assert playbook_dir() == xdg / "kstrl" / "global" / "playbook"
    assert ledger_path() == playbook_dir() / "ops.jsonl"


def test_ops_fold_in_order(xdg: Path) -> None:
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    append_ops([Op(OpKind.ADD, "L2", AT, lesson=_lesson("L2"))])
    append_ops([Op(OpKind.UPDATE, "L1", LATER, changes={"insight": "second wording"})])
    append_ops([Op(OpKind.DEMOTE, "L2", LATER)])
    append_ops([Op(OpKind.UPDATE, "L1", LATER, changes={"insight": "third wording"})])
    append_ops([Op(OpKind.RETIRE, "L2", LATER)])

    playbook = load_playbook()

    by_id = {lesson.id: lesson for lesson in playbook.lessons}
    assert [lesson.id for lesson in playbook.lessons] == ["L1", "L2"]
    # The later UPDATE wins, which is what "in order" means.
    assert by_id["L1"].insight == "third wording"
    assert by_id["L1"].updated_at == LATER
    assert by_id["L1"].status is LessonStatus.ACTIVE
    assert by_id["L2"].status is LessonStatus.RETIRED
    assert by_id["L2"].active is False
    raw = ledger_path().read_bytes()
    assert playbook.line_count == 6
    assert playbook.sha256 == hashlib.sha256(raw).hexdigest()


def test_unparseable_ledger_line_is_refused_by_index(xdg: Path) -> None:
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    with ledger_path().open("ab") as handle:
        handle.write(b'{"op": "UPDATE", "id": "L1", "at"\n')
    append_ops([Op(OpKind.DEMOTE, "L1", LATER)])

    with pytest.raises(PlaybookError, match=r"line 2\b"):
        load_playbook()


def test_unknown_op_is_refused_by_index(xdg: Path) -> None:
    playbook_dir().mkdir(parents=True)
    ledger_path().write_bytes(_ledger_lines('{"op": "PROMOTE", "id": "L1", "at": "t"}'))

    with pytest.raises(PlaybookError, match=r"line 1\b.*PROMOTE"):
        load_playbook()


def test_unknown_lesson_id_is_refused(xdg: Path) -> None:
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    append_ops([Op(OpKind.DEMOTE, "L9", LATER)])

    with pytest.raises(PlaybookError, match=r"line 2\b.*'L9'"):
        load_playbook()


def test_a_second_add_of_one_id_is_refused(xdg: Path) -> None:
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    append_ops([Op(OpKind.ADD, "L1", LATER, lesson=_lesson("L1"))])

    with pytest.raises(PlaybookError, match=r"line 2\b.*'L1'"):
        load_playbook()


def test_a_missing_ledger_is_an_empty_playbook(xdg: Path) -> None:
    playbook = load_playbook()

    assert playbook.lessons == ()
    assert playbook.line_count == 0
    assert playbook.sha256 == hashlib.sha256(b"").hexdigest()
    assert not playbook_dir().exists()


@pytest.mark.parametrize(
    "evidence",
    [
        "kstrl/playbook.py:42",
        "src/app.ts:7-19",
        "mypy:no-any-return\nreturn cast(int, x)",
    ],
    ids=["path-line", "path-line-range", "newline"],
)
def test_evidence_with_source_line_is_refused(xdg: Path, evidence: str) -> None:
    bad = _lesson("L1", evidence=(RUN_ID, evidence))

    with pytest.raises(PlaybookError, match=r"evidence\[1\]"):
        append_ops([Op(OpKind.ADD, "L1", AT, lesson=bad)])
    with pytest.raises(PlaybookError, match=r"evidence\[0\]"):
        append_ops([Op(OpKind.UPDATE, "L1", AT, changes={"evidence": [evidence]})])

    assert not ledger_path().exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "retired"},
        {"active": False},
        {"id": "L2"},
        {"created_at": LATER},
        {"updated_at": LATER},
    ],
    ids=["status", "active", "id", "created_at", "updated_at"],
)
def test_an_update_may_not_change_status_identity_or_time(
    xdg: Path, changes: dict[str, Any]
) -> None:
    """Status moves only through DEMOTE and RETIRE, and identity and
    time only through the fold, so ``status`` and ``active`` cannot
    disagree about a lesson."""
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    before = ledger_path().read_bytes()

    with pytest.raises(PlaybookError, match="may not change"):
        append_ops([Op(OpKind.UPDATE, "L1", LATER, changes=changes)])

    assert ledger_path().read_bytes() == before


def test_signatures_and_run_ids_are_accepted_as_evidence(xdg: Path) -> None:
    """The control for the refusal above: the rule must not refuse the
    evidence it exists to let through."""
    evidence = (RUN_ID, "review:prd_criterion", "ruff:E501", "tests:assertion-error")
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1", evidence=evidence))])

    assert load_playbook().lessons[0].evidence == evidence


def test_a_batch_with_one_refused_op_writes_none_of_it(xdg: Path) -> None:
    """``append_ops`` validates every op before it writes any, so a
    refused op cannot leave the ops in front of it on disk."""
    good = Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))
    bad = Op(OpKind.ADD, "L2", AT, lesson=_lesson("L2", evidence=("kstrl/playbook.py:42",)))

    with pytest.raises(PlaybookError, match=r"op 1\b.*evidence\[0\]"):
        append_ops([good, bad])

    assert not ledger_path().exists()


def _write_toml(root: Path, body: bytes) -> None:
    (root / "kstrl.toml").write_bytes(body)


@pytest.mark.parametrize(
    "fault",
    ["syntax", "not-utf-8", "a-directory"],
)
def test_unreadable_toml_means_no_contribution(tmp_path: Path, fault: str) -> None:
    root = tmp_path / "project"
    root.mkdir()
    # The control first: the same key in a readable file is honoured.
    _write_toml(root, b"[learning]\ncontribute = true\nconsume = true\n")
    assert LearningConfig.load(root).contribute is True

    if fault == "syntax":
        _write_toml(root, b"[learning]\ncontribute = true\n[[[\n")
    elif fault == "not-utf-8":
        _write_toml(root, b"[learning]\ncontribute = true\n# \xff\xfe\n")
    else:
        (root / "kstrl.toml").unlink()
        (root / "kstrl.toml").mkdir()

    assert LearningConfig.load(root).contribute is False


def test_both_switches_default_to_true_and_the_file_can_turn_them_off(tmp_path: Path) -> None:
    assert LearningConfig.load(tmp_path) == LearningConfig(contribute=True, consume=True)
    _write_toml(tmp_path, b"[learning]\ncontribute = false\nconsume = false\n")
    assert LearningConfig.load(tmp_path) == LearningConfig(contribute=False, consume=False)


def test_the_environment_overrides_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_toml(tmp_path, b"[learning]\ncontribute = true\n")
    monkeypatch.setenv("KSTRL_LEARNING_CONTRIBUTE", "0")
    monkeypatch.setenv("KSTRL_LEARNING_CONSUME", "0")

    assert LearningConfig.load(tmp_path) == LearningConfig(contribute=False, consume=False)
    assert LearningConfig.from_env() == LearningConfig(contribute=False, consume=False)


def test_a_non_boolean_switch_is_rejected_not_read_as_true(tmp_path: Path) -> None:
    """``bool("false")`` is True, so a lenient cast would turn an
    operator's quoted opt-out into a contribution."""
    _write_toml(tmp_path, b'[learning]\ncontribute = "false"\n')

    with pytest.raises(ConfigError, match="contribute"):
        LearningConfig.load(tmp_path)


def test_the_environment_cannot_turn_contribution_back_on_over_an_unreadable_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-out may be in the file that could not be read, so the
    environment does not get to re-enable ``contribute``. ``consume``
    still follows the environment."""
    monkeypatch.setenv("KSTRL_LEARNING_CONTRIBUTE", "1")
    monkeypatch.setenv("KSTRL_LEARNING_CONSUME", "0")
    # The control first: over a readable file the environment wins.
    _write_toml(tmp_path, b"[learning]\ncontribute = false\n")
    assert LearningConfig.load(tmp_path) == LearningConfig(contribute=True, consume=False)

    _write_toml(tmp_path, b"[learning]\ncontribute = false\n[[[\n")

    assert LearningConfig.load(tmp_path) == LearningConfig(contribute=False, consume=False)


def test_contribute_false_writes_nothing(xdg: Path) -> None:
    warnings: list[str] = []

    written = contribute(
        [Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))],
        LearningConfig(contribute=False),
        warn=warnings.append,
    )

    assert written == 0
    assert not ledger_path().exists()
    assert warnings == []


def test_an_unreachable_store_warns_and_writes_nowhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker / "state"))
    clear_xdg_state_home_cache()
    monkeypatch.chdir(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    warnings: list[str] = []

    written = contribute(
        [Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))],
        LearningConfig(),
        warn=warnings.append,
    )

    assert written == 0
    assert len(warnings) == 1 and "skipped" in warnings[0]
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_contribute_true_appends(xdg: Path) -> None:
    written = contribute(
        [Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))],
        LearningConfig(),
        warn=lambda _message: None,
    )

    assert written == 1
    assert [lesson.id for lesson in load_playbook().lessons] == ["L1"]


# --- six writers at once ---------------------------------------------------

WRITERS = 6
PER_WRITER = 25

_WRITER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from kstrl.playbook import Lesson, LessonStatus, Op, OpKind, append_ops

    worker, count = int(sys.argv[1]), int(sys.argv[2])
    ready, go = Path(sys.argv[3]), Path(sys.argv[4])
    ready.touch()
    deadline = time.monotonic() + 60
    while not go.exists():
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.001)
    for i in range(count):
        lesson_id = f"w{worker}-{i}"
        lesson = Lesson(
            id=lesson_id, section="s", keywords=(), issue="i", insight="n",
            active=True, used_count=0, helpful_count=0, harmful_count=0,
            neutral_count=0, created_at="t", updated_at="t",
            evidence=("r",), scope="universal", target_signature="x:y",
            status=LessonStatus.ACTIVE,
        )
        append_ops([Op(OpKind.ADD, lesson_id, "t", lesson=lesson)])
    """
)


def _kill_groups(procs: list[subprocess.Popen[bytes]]) -> None:
    """Each writer leads its own group (``start_new_session=True``), so
    the pid is the pgid and nothing outside this test is touched."""
    for proc in procs:
        if proc.poll() is None:
            kill_group(proc.pid)
            proc.wait(timeout=10)


def test_concurrent_appends_lose_nothing(xdg: Path, tmp_path: Path) -> None:
    """Six processes, 150 appends, 150 recovered (design doc section 9,
    M11). A barrier releases every writer at once so they overlap; a
    rewritten document loses updates under exactly this overlap."""
    env = {**os.environ, "XDG_STATE_HOME": str(xdg)}
    go = tmp_path / "go"
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for worker in range(WRITERS):
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _WRITER,
                        str(worker),
                        str(PER_WRITER),
                        str(tmp_path / f"ready-{worker}"),
                        str(go),
                    ],
                    env=env,
                    cwd=tmp_path,
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        deadline = time.monotonic() + 60
        while not all((tmp_path / f"ready-{w}").exists() for w in range(WRITERS)):
            assert time.monotonic() < deadline, "a writer never became ready"
            assert all(p.poll() is None for p in procs), "a writer exited before the barrier"
            time.sleep(0.01)
        go.touch()
        for proc in procs:
            _out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err.decode("utf-8", "replace")
    finally:
        _kill_groups(procs)

    playbook = load_playbook()

    expected = {f"w{w}-{i}" for w in range(WRITERS) for i in range(PER_WRITER)}
    assert playbook.line_count == WRITERS * PER_WRITER
    assert {lesson.id for lesson in playbook.lessons} == expected


# --- the command -----------------------------------------------------------


def _ks(tmp_path: Path, xdg: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.update(XDG_STATE_HOME=str(xdg), KSTRL_NO_TUI="1", KSTRL_AGENT_PROBE="0")
    env.pop("NO_COLOR", None)
    return subprocess.run(
        [sys.executable, "-m", "kstrl", *argv],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_learn_playbook_prints_the_fold_and_the_ledger_digest(xdg: Path, tmp_path: Path) -> None:
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    append_ops([Op(OpKind.UPDATE, "L1", LATER, changes={"insight": "second wording"})])
    digest = hashlib.sha256(ledger_path().read_bytes()).hexdigest()

    proc = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "L1" in output and "second wording" in output
    assert digest in output
    assert re.search(r"^\s*lines:\s+2$", output, re.MULTILINE), output


def test_learn_playbook_refuses_an_unparseable_ledger_with_exit_2(
    xdg: Path, tmp_path: Path
) -> None:
    append_ops([Op(OpKind.ADD, "L1", AT, lesson=_lesson("L1"))])
    with ledger_path().open("ab") as handle:
        handle.write(b"not json\n")

    proc = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")

    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "line 2" in output


def test_learn_playbook_refuses_an_unreadable_ledger_with_exit_2(xdg: Path, tmp_path: Path) -> None:
    """A ledger that exists and cannot be read is a refusal, not an
    empty playbook: only a MISSING ledger reads as empty."""
    ledger_path().mkdir(parents=True)

    proc = _ks(tmp_path, xdg, "learn", "playbook", "--ui", "plain")

    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "could not be read" in output
