"""#640: nothing codex prints is read as the model's own words.

Every test but the last is a real ``ks run 3`` subprocess in a scratch
repository scaffolded by a real ``ks init``, with ``[agent] type = "codex"``
and stand-in ``codex`` and ``claude`` executables first on PATH, so no real
CLI is reached. The stand-in ``codex`` behaves as ``codex-cli 0.156.1`` was
measured to in #640: it prints a header and then the whole prompt to
stderr, which the adapter's streamer merges into the stream it yields. The
engineer prompt ``ks init`` scaffolds holds ``<promise>COMPLETE</promise>``
alone on a line, so an adapter that yields the echo unmarked completes the
loop on iteration 1 whatever the model did.

The stand-in writes its reply to the ``--output-last-message`` file and
also prints it to stdout. What a real codex prints for a tool call was not
measured (it needs a billed call), so the adapter treats every printed line
as transcript and only the last-message file as the model's words; the
``transcript_marker`` and ``empty_last`` modes stand in for tool output
carrying a bare marker line.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import kstrl.agents
from kstrl.agents.base import TOOL_RESULT_PREFIX
from kstrl.loop import COMPLETION_MARKER
from tests.helpers import gitrepo
from tests.spine_utils import git

MAX_ITERATIONS = 3
ENGINEER_REPLY = "I looked around and changed nothing."
DISTILLER_REPLY = "DISTILLER-REPLY nothing durable"
#: Only the stand-in's header prints this, so it can reach a reader only
#: through the echoed transcript.
CODEX_HEADER = "OpenAI Codex v0.156.1"

_STAND_IN_CODEX = """\
#!@PYTHON@
import os
import sys

argv = sys.argv[1:]
if argv[:2] == ["exec", "--help"]:
    print("  -o, --output-last-message <FILE>")
    sys.exit(0)
prompt = sys.stdin.read()
marker = "<promise>COMPLETE</promise>"
role = "engineer" if any(l.strip() == marker for l in prompt.splitlines()) else "other"
with open(os.environ["CODEX_STUB_CALLS"], "a", encoding="utf-8") as f:
    f.write(role + "\\n")
last = None
for i, arg in enumerate(argv):
    if arg in ("--output-last-message", "-o"):
        last = argv[i + 1]
mode = os.environ["CODEX_STUB_MODE"]
err = sys.stderr
err.write("Reading prompt from stdin...\\n@HEADER@\\n--------\\nworkdir: x\\n--------\\nuser\\n")
err.write(prompt if prompt.endswith("\\n") else prompt + "\\n")
err.write("\\n")
reply = "@ENGINEER_REPLY@"
if mode in ("transcript_marker", "empty_last"):
    err.write("exec\\n/bin/zsh -lc 'cat notes.txt' in x\\n succeeded in 5ms:\\n" + marker + "\\n")
if mode == "complete":
    reply = marker
elif mode == "empty_last":
    reply = ""
if role == "other":
    reply = "@DISTILLER_REPLY@"
err.flush()
if reply:
    sys.stdout.write("codex\\n" + reply + "\\n")
if mode != "empty_last":
    sys.stdout.write("tokens used\\n1,234\\n")
sys.stdout.flush()
if last:
    with open(last, "w", encoding="utf-8") as f:
        f.write(reply)
"""

_STAND_IN_CLAUDE = """\
#!@PYTHON@
import sys

sys.stdin.read()
sys.stderr.write("stand-in claude: no real claude is reached from this test\\n")
sys.exit(1)
"""


@dataclass(frozen=True)
class _Run:
    root: Path
    returncode: int
    stdout: str
    calls: list[str]

    @property
    def engineer_calls(self) -> int:
        return self.calls.count("engineer")

    def engineer_log(self) -> list[str]:
        logs = sorted(self.root.glob(".kstrl/runs/*/components/main/engineer.log"))
        assert len(logs) == 1, logs
        return logs[0].read_text(encoding="utf-8").splitlines()


def _stand_in(path: Path, source: str) -> None:
    text = (
        source.replace("@PYTHON@", sys.executable)
        .replace("@HEADER@", CODEX_HEADER)
        .replace("@ENGINEER_REPLY@", ENGINEER_REPLY)
        .replace("@DISTILLER_REPLY@", DISTILLER_REPLY)
    )
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _set_key(text: str, commented: str, value: str) -> str:
    """Uncomment one scaffolded ``kstrl.toml`` key; fail if it moved."""
    pattern = re.compile(rf"^# {re.escape(commented)} = .*$", re.MULTILINE)
    new, count = pattern.subn(f"{commented} = {value}", text)
    assert count == 1, f"ks init no longer scaffolds '# {commented} = ...'"
    return new


def _run_codex_loop(tmp_path: Path, mode: str) -> _Run:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stand_in(bindir / "codex", _STAND_IN_CODEX)
    _stand_in(bindir / "claude", _STAND_IN_CLAUDE)
    calls = tmp_path / "codex-calls.txt"
    calls.write_text("", encoding="utf-8")

    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KSTRL_", "FACTORY_"))}
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        KSTRL_AGENT_PROBE="0",
        CODEX_STUB_MODE=mode,
        CODEX_STUB_CALLS=str(calls),
    )
    init = subprocess.run(
        [sys.executable, "-m", "kstrl", "init", str(root), "--ui", "plain", "--no-color"],
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert init.returncode == 0, init.stdout + init.stderr

    toml = root / "kstrl.toml"
    text = _set_key(toml.read_text(encoding="utf-8"), "type", '"codex"')
    # No reviewer: the review phase would run the stand-in claude, and
    # what a refusing reviewer does is not what these tests measure.
    text = _set_key(text, "review_mode", '"skip"')
    toml.write_text(text, encoding="utf-8")
    (root / "scripts" / "kstrl" / "prd.json").write_text(
        '{"branchName": "kstrl/feature", "userStories": [{"id": "US-001", '
        '"title": "Add a greeting", "acceptanceCriteria": ["hello.txt exists"], '
        '"priority": 1, "passes": false, "notes": ""}]}\n',
        encoding="utf-8",
    )
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "scaffold", cwd=root)

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl",
            "run",
            str(MAX_ITERATIONS),
            "--no-verify",
            "--ui",
            "plain",
            "--no-color",
            "--branch",
            "",
            "-s",
            "0",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=180,
    )
    return _Run(
        root=root,
        returncode=done.returncode,
        stdout=done.stdout + done.stderr,
        calls=calls.read_text(encoding="utf-8").split(),
    )


def test_a_codex_that_changes_nothing_runs_every_iteration(tmp_path: Path) -> None:
    run = _run_codex_loop(tmp_path, "nothing")

    assert run.engineer_calls == MAX_ITERATIONS, run.stdout
    assert "COMPLETED: main" not in run.stdout


def test_a_last_message_of_exactly_the_marker_completes_in_one_iteration(
    tmp_path: Path,
) -> None:
    run = _run_codex_loop(tmp_path, "complete")

    assert run.returncode == 0, run.stdout
    assert run.engineer_calls == 1, run.stdout
    assert "COMPLETED: main (1 iterations" in run.stdout


def test_a_marker_in_the_transcript_does_not_complete_the_loop(tmp_path: Path) -> None:
    run = _run_codex_loop(tmp_path, "transcript_marker")

    assert run.engineer_calls == MAX_ITERATIONS, run.stdout
    assert "COMPLETED: main" not in run.stdout


def test_an_empty_last_message_leaves_no_transcript_line_as_the_reply(
    tmp_path: Path,
) -> None:
    run = _run_codex_loop(tmp_path, "empty_last")

    assert run.engineer_calls == MAX_ITERATIONS, run.stdout
    assert "COMPLETED: main" not in run.stdout
    lines = run.engineer_log()
    # With no reply, nothing the adapter yields is the model's words.
    assert [line for line in lines if not line.startswith(TOOL_RESULT_PREFIX)] == []
    # The echoed prompt and the tool block hold one marker each per
    # iteration. A third means a transcript line was also yielded as the
    # reply, marked or not.
    assert lines.count(TOOL_RESULT_PREFIX + COMPLETION_MARKER) == 2 * MAX_ITERATIONS


def test_engineer_log_holds_the_echoed_prompt_only_as_tool_output(tmp_path: Path) -> None:
    run = _run_codex_loop(tmp_path, "nothing")
    lines = run.engineer_log()

    assert [line for line in lines if line.strip() == COMPLETION_MARKER] == []
    assert lines.count(TOOL_RESULT_PREFIX + COMPLETION_MARKER) == MAX_ITERATIONS
    assert lines.count(TOOL_RESULT_PREFIX + CODEX_HEADER) == MAX_ITERATIONS
    assert CODEX_HEADER not in lines
    # The last-message reply is the one line the model said, unmarked.
    assert lines.count(ENGINEER_REPLY) == MAX_ITERATIONS


def test_the_distiller_reads_the_reply_not_the_echoed_prompt(tmp_path: Path) -> None:
    run = _run_codex_loop(tmp_path, "complete")
    knowledge = [line for line in run.stdout.splitlines() if line.startswith("  Knowledge:")]

    assert len(knowledge) == 1, run.stdout
    assert CODEX_HEADER not in knowledge[0]
    assert "Reading prompt from stdin" not in knowledge[0]


#: Adapters whose lines are NOT marked, each with the reason. A CustomAgent
#: runs the operator's own command, and kstrl cannot tell that command's
#: tool output from its words.
_DISCLOSED_UNMARKED = {"CustomAgent": "the operator's own command's stdout"}
#: Wrappers that report the wrapped agent's ``marks_tool_output``.
_DELEGATING = {"LoggingAgent"}


def _collect_agent_marks() -> dict[str, object]:
    """Every ``marks_tool_output`` value on a class shaped like an Agent.

    Extracted out of the census test below it so that test measures under
    the complexipy cognitive-complexity gate; this changes only where the
    loop lives, not what it collects.
    """
    found: dict[str, object] = {}
    for info in pkgutil.iter_modules(kstrl.agents.__path__):
        module = importlib.import_module(f"kstrl.agents.{info.name}")
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            if getattr(obj, "_is_protocol", False):
                continue
            if callable(getattr(obj, "run", None)) and hasattr(obj, "final_message"):
                found[name] = inspect.getattr_static(obj, "marks_tool_output", None)
    return found


def test_every_agent_adapter_marks_its_transcript_or_is_a_disclosed_exception() -> None:
    """Closed census of every class in kstrl/agents/ shaped like an Agent.

    A new adapter fails here until it either sets ``marks_tool_output =
    True`` or is added to a disclosed list above with its reason.
    """
    found = _collect_agent_marks()

    assert set(found) == {
        "ClaudeCodeAgent",
        "ClaudeSdkAgent",
        "CodexAgent",
        "CustomAgent",
        "LoggingAgent",
    }
    for name, attr in found.items():
        if name in _DISCLOSED_UNMARKED:
            assert attr is None, f"{name} now sets marks_tool_output; drop the disclosure"
        elif name in _DELEGATING:
            assert isinstance(attr, property), name
        else:
            assert attr is True, f"{name} yields output without marks_tool_output = True"
