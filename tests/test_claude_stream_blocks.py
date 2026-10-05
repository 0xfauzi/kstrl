"""Every block a Claude Code stream carries has a decided disposition (#598).

``tests/fixtures/claude_stream/blocks.jsonl`` is HAND-BUILT, not a
recording: its shapes come from one ``stream-json`` probe of Claude Code
2.1.283 and a census of the ``message.content`` blocks in local session
files (#596 item 9). No recorded transcript was kept. Recording one is a
paid call and an owner action::

    claude -p "Run the shell command: echo kstrl-probe-42. Then reply with one word: done." \\
        --output-format stream-json --verbose --max-turns 3 --allowedTools Bash > probe.jsonl

The guard below is only as current as the fixture: a block type Claude
Code starts emitting later reaches it only when someone records a new
transcript and adds it here.

The adapters are driven end to end: ``ClaudeCodeAgent.run`` spawns a fake
``claude`` executable on ``PATH`` through the real ``DeadlineStreamer``,
and ``ClaudeSdkAgent.run`` spawns the real ``sdk_runner`` subprocess, which
drives the real SDK against a fake CLI that speaks the SDK's control
protocol. No LLM is called.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl.agents.base import TOOL_RESULT_PREFIX, UsageRecord, model_output_text
from kstrl.agents.claude_code import ClaudeCodeAgent, _parse_stream_event
from kstrl.agents.claude_sdk import ClaudeSdkAgent
from kstrl.agents.logging import LoggingAgent
from kstrl.config import KstrlConfig
from kstrl.decompose import _extract_agent_json, _extract_json, _select_agent_output
from kstrl.loop import COMPLETION_MARKER, run_loop
from kstrl.timeout import TimeoutConfig
from kstrl.ui.plain import PlainUI
from tests.helpers.executables import put_on_path, write_executable

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream" / "blocks.jsonl"

#: ``(event type, block type)`` -> what the adapter does with it. ``"<str>"``
#: means ``message.content`` is a string; ``None`` means the event has no
#: content list.
DISPOSITION: dict[tuple[str, str | None], str] = {
    ("assistant", "text"): "render",
    ("assistant", "tool_use"): "render",
    # A block type this parser does not specifically handle (#598) still
    # renders: a fixed placeholder, not the skip a truly non-model event
    # (system, rate_limit_event, result) gets, so an unhandled type is
    # visible instead of vanishing with no trace.
    ("assistant", "thinking"): "render",
    ("user", "tool_result"): "render",
    ("user", "text"): "skip",
    ("user", "<str>"): "skip",
    ("system", None): "skip",
    ("rate_limit_event", None): "skip",
    ("result", None): "skip",
}

#: What ``ClaudeCodeAgent.run`` yields over the fixture. Literals on
#: purpose: computing them with the code under test would test nothing.
EXPECTED = [
    "  | [unrendered thinking block]",
    "[Bash] echo kstrl-probe-42",
    "  | kstrl-probe-42",
    "[Bash] uv run pytest -q",
    "  | FAILED tests/test_api.py::test_create_rejects_empty_key - assert 500 == 400",
    "  | FAILED tests/test_api.py::test_update_rejects_empty_key - assert 500 == 400",
    "  | FAILED tests/test_server.py::test_post_empty_key...",
    "[Read] missing.py",
    "  | File does not exist.",
    "done",
]

PYTEST_OUTPUT = (
    "FAILED tests/test_api.py::test_create_rejects_empty_key - assert 500 == 400\n"
    "FAILED tests/test_api.py::test_update_rejects_empty_key - assert 500 == 400\n"
    "FAILED tests/test_server.py::test_post_empty_key - assert 500 == 400\n"
    "3 failed, 1084 passed in 12.31s"
)

#: Every agent call in these tests is bounded, so a fake that stops
#: answering fails the test instead of hanging it.
BOUNDED = TimeoutConfig(agent_iteration=60.0)

GREP_COMMAND = "grep -o '<promise>COMPLETE</promise>' scripts/kstrl/prompt.md"
VERDICT = {"verdict": "request_changes", "issues": [{"severity": "high"}]}


def _fixture_lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _pairs(event: dict[str, Any]) -> set[tuple[str, str | None]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return {(event["type"], block["type"]) for block in content}
    if isinstance(content, str):
        return {(event["type"], "<str>")}
    return {(event["type"], None)}


def _has_text_item(event: dict[str, Any]) -> bool:
    """An assistant text or tool_use block, any block type the parser does
    not specifically handle (its placeholder always renders - #598), or a
    tool_result holding text."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    for block in content:
        if block["type"] in ("text", "tool_use"):
            return True
        if block["type"] == "tool_result":
            inner = block["content"]
            if isinstance(inner, str) or any(item["type"] == "text" for item in inner):
                return True
        else:
            return True
    return False


def _tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "fake",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
        "parent_tool_use_id": None,
        "session_id": "fixture",
    }


def _tool_result(tool_id: str, content: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": tool_id, "type": "tool_result", "content": content},
            ],
        },
        "parent_tool_use_id": None,
        "session_id": "fixture",
    }


def _user(content: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "fixture",
    }


def _text(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "fake",
            "content": [{"type": "text", "text": text}],
        },
        "parent_tool_use_id": None,
        "session_id": "fixture",
    }


def _result(text: str) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "duration_ms": 4200,
        "duration_api_ms": 4000,
        "num_turns": 2,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "session_id": "fixture",
    }


def _install_fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_lines: list[str]
) -> None:
    """A ``claude`` on PATH that drains its stdin and prints ``stream_lines``."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text("".join(line + "\n" for line in stream_lines), encoding="utf-8")
    put_on_path(tmp_path, monkeypatch, "claude", f"#!/bin/sh\ncat > /dev/null\ncat '{stream}'\n")


def _run_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]
) -> tuple[ClaudeCodeAgent, list[str]]:
    _install_fake_claude(tmp_path, monkeypatch, [json.dumps(event) for event in events])
    agent = ClaudeCodeAgent()
    lines = list(agent.run("prompt", cwd=tmp_path, timeout=60))
    return agent, lines


#: A fake claude CLI speaking the Agent SDK's stream-json control protocol:
#: it answers every control request with success, and on the first user
#: message prints the stream in STREAM_FILE and exits.
_SDK_CLI = """\
import json, sys
if any(arg in ("-v", "--version") for arg in sys.argv[1:]):
    print("2.1.283 (Claude Code)")
    sys.exit(0)
with open(STREAM_FILE, encoding="utf-8") as handle:
    stream = handle.read()
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("type") == "control_request":
        reply = {"subtype": "success", "request_id": message["request_id"], "response": {}}
        print(json.dumps({"type": "control_response", "response": reply}), flush=True)
    elif message.get("type") == "user":
        sys.stdout.write(stream)
        sys.stdout.flush()
        sys.exit(0)
"""


def _sdk_agent(tmp_path: Path, events: list[dict[str, Any]]) -> ClaudeSdkAgent:
    pytest.importorskip("claude_agent_sdk")
    stream = tmp_path / "sdk-stream.jsonl"
    stream.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    cli = write_executable(
        tmp_path / "fake-sdk-claude",
        f"#!{sys.executable}\nSTREAM_FILE = {str(stream)!r}\n" + _SDK_CLI,
    )
    agent = ClaudeSdkAgent()
    agent._cli_path = str(cli)
    return agent


def _loop_config(tmp_path: Path) -> KstrlConfig:
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}', encoding="utf-8"
    )
    return KstrlConfig(
        max_iterations=1,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        kstrl_branch="",
        kstrl_branch_explicit=True,
    )


def _marker_stream() -> list[dict[str, Any]]:
    """The engineer greps the prompt file and the tool prints the marker."""
    return [
        _tool_use("toolu_1", "Bash", {"command": GREP_COMMAND}),
        _tool_result("toolu_1", "Reply with:\n<promise>COMPLETE</promise>"),
        _text("Still working."),
        _result("Still working."),
    ]


#: A Bash command whose own text embeds the marker on one of its lines
#: (a heredoc writing the marker to a file) - the tool never runs, so
#: this is only ever the announcement line, never a tool result.
HEREDOC_COMMAND = "cat > expected.txt <<'EOF'\n<promise>COMPLETE</promise>\nEOF"


def _heredoc_stream() -> list[dict[str, Any]]:
    """A multi-line tool call, no result event and no assistant text
    after it: the only candidate for the final_message fallback is the
    tool call's own announcement line (#598)."""
    return [_tool_use("toolu_1", "Bash", {"command": HEREDOC_COMMAND})]


def _reviewer_stream() -> list[dict[str, Any]]:
    return [
        _tool_use("toolu_1", "Bash", {"command": "cat .kstrl/state.json"}),
        _tool_result("toolu_1", '{"status": "pass", "issues": []}'),
        _text(json.dumps(VERDICT)),
        _text("Review complete."),
        _result("Review complete."),
    ]


# --- the census: every block in the fixture has a decided disposition ------


def test_every_fixture_block_has_a_disposition() -> None:
    seen: set[tuple[str, str | None]] = set()
    for line in _fixture_lines():
        seen |= _pairs(json.loads(line))

    undecided = seen - set(DISPOSITION)
    unseen = set(DISPOSITION) - seen
    assert not undecided, f"fixture blocks with no disposition: {sorted(undecided, key=str)}"
    assert not unseen, f"dispositions no fixture block exercises: {sorted(unseen, key=str)}"


def test_parse_stream_event_follows_disposition() -> None:
    for line in _fixture_lines():
        event = json.loads(line)
        renders = all(DISPOSITION[pair] == "render" for pair in _pairs(event))
        expect_output = renders and _has_text_item(event)
        got = list(_parse_stream_event(line))
        assert bool(got) == expect_output, f"{sorted(_pairs(event), key=str)} yielded {got!r}"


# --- the Claude Code CLI adapter, end to end through a fake claude ---------


def test_run_yields_exact_lines_over_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_claude(tmp_path, monkeypatch, _fixture_lines())

    lines = list(ClaudeCodeAgent().run("prompt", cwd=tmp_path, timeout=60))

    assert lines == EXPECTED


def test_tool_result_text_reaches_engineer_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_claude(tmp_path, monkeypatch, _fixture_lines())
    log = tmp_path / "engineer.log"

    list(LoggingAgent(ClaudeCodeAgent(), log).run("prompt", cwd=tmp_path, timeout=60))

    assert "  | kstrl-probe-42" in log.read_text(encoding="utf-8").splitlines()


def test_tool_result_capped_at_200_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_claude(tmp_path, monkeypatch, _fixture_lines())
    log = tmp_path / "engineer.log"

    list(LoggingAgent(ClaudeCodeAgent(), log).run("prompt", cwd=tmp_path, timeout=60))

    pytest_lines = [
        line.removeprefix(TOOL_RESULT_PREFIX)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(TOOL_RESULT_PREFIX + "FAILED")
    ]
    assert "\n".join(pytest_lines) == PYTEST_OUTPUT[:200] + "..."


def test_user_text_never_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_claude(tmp_path, monkeypatch, _fixture_lines())

    lines = list(ClaudeCodeAgent().run("prompt", cwd=tmp_path, timeout=60))

    for secret in ("OPERATOR-TEXT-BLOCK-MUST-NOT-LOG", "OPERATOR-STRING-CONTENT-MUST-NOT-LOG"):
        assert not any(secret in line for line in lines), secret


def test_model_text_that_starts_with_a_pipe_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exact prefix marks a tool line: the agent's own markdown
    table, whose lines start with "|", is still the agent's final message."""
    table = "| story | status |\n|---|---|\n| s1 | pass |"
    agent, lines = _run_cli(
        tmp_path,
        monkeypatch,
        [
            _tool_use("toolu_1", "Bash", {"command": "ls"}),
            _tool_result("toolu_1", "a.py"),
            _text(table),
        ],
    )

    assert lines == ["[Bash] ls", "  | a.py", table]
    assert agent.final_message == table


def test_every_tool_result_in_one_user_event_is_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user event can carry several tool_result blocks; each is rendered."""
    event = _user(
        [
            {"tool_use_id": "toolu_1", "type": "tool_result", "content": "first"},
            {"tool_use_id": "toolu_2", "type": "tool_result", "content": "second"},
        ]
    )
    _, lines = _run_cli(tmp_path, monkeypatch, [event, _text("done"), _result("done")])

    assert lines == ["  | first", "  | second", "done"]


def test_tool_result_of_exactly_200_characters_is_not_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap marks a cut with "..." only when text was actually dropped."""
    _, lines = _run_cli(
        tmp_path,
        monkeypatch,
        [_tool_result("toolu_1", "x" * 200), _text("done"), _result("done")],
    )

    assert lines == ["  | " + "x" * 200, "done"]


def test_top_level_tool_result_event_is_not_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code does not emit a top-level ``tool_result`` event (#596 item
    9), so the adapter keeps no branch for one: such a branch would put a
    tool's text in the stream with no prefix."""
    _, lines = _run_cli(
        tmp_path,
        monkeypatch,
        [{"type": "tool_result", "content": COMPLETION_MARKER}, _text("done"), _result("done")],
    )

    assert lines == ["done"]


def test_result_event_parse_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_claude(tmp_path, monkeypatch, _fixture_lines())
    agent = ClaudeCodeAgent()

    list(agent.run("prompt", cwd=tmp_path, timeout=60))

    assert agent.final_message == "done"
    assert agent.usage_records[-1] == UsageRecord(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=300,
        cache_creation_tokens=40,
        total_tokens=370,
        cost_usd=0.0123,
        duration_seconds=4.2,
        source="claude-stream-json",
    )


def test_tool_result_cannot_signal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real engineer loop over a real adapter: a tool that prints the
    marker must not end the loop as complete."""
    config = _loop_config(tmp_path)
    _install_fake_claude(tmp_path, monkeypatch, [json.dumps(e) for e in _marker_stream()])

    result = run_loop(config, PlainUI(no_color=True), ClaudeCodeAgent(), tmp_path, timeouts=BOUNDED)

    assert result.completed is False


def test_heredoc_tool_call_cannot_signal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real engineer loop, no result event: a Bash command whose own
    text embeds the marker (a heredoc) must not end the loop as complete
    through the final_message fallback (#598)."""
    config = _loop_config(tmp_path)
    _install_fake_claude(tmp_path, monkeypatch, [json.dumps(e) for e in _heredoc_stream()])

    result = run_loop(config, PlainUI(no_color=True), ClaudeCodeAgent(), tmp_path, timeouts=BOUNDED)

    assert result.completed is False


def test_final_message_never_tool_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No result event and no assistant text: the final_message fallback
    is None, never a tool call's announcement or its result (#598) - the
    fallback is collected only from the assistant's own text blocks."""
    agent, lines = _run_cli(tmp_path, monkeypatch, _marker_stream()[:2])

    assert lines[-1] == TOOL_RESULT_PREFIX + COMPLETION_MARKER
    assert agent.final_message is None


def test_select_agent_output_ignores_tool_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, lines = _run_cli(tmp_path, monkeypatch, _reviewer_stream())

    assert _extract_json(_select_agent_output(agent, lines)) == VERDICT


def test_extract_agent_json_ignores_tool_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, lines = _run_cli(tmp_path, monkeypatch, _reviewer_stream())

    assert _extract_agent_json(agent, lines) == VERDICT


def test_logging_agent_delegates_marks_tool_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LoggingAgent.marks_tool_output must delegate to the wrapped agent
    (#598 addendum A1): model_output_text's tool-output filtering keys on
    this property, and LoggingAgent itself never writes TOOL_RESULT_PREFIX."""
    agent, lines = _run_cli(tmp_path, monkeypatch, _reviewer_stream())
    wrapped = LoggingAgent(agent, tmp_path / "review.log")

    wrapped_text = model_output_text(wrapped, lines)

    assert not any(line.startswith(TOOL_RESULT_PREFIX) for line in wrapped_text.splitlines())
    assert wrapped_text == model_output_text(agent, lines)


# --- the SDK adapter, end to end through the real runner and SDK ----------


def test_sdk_tool_result_uses_shared_prefix_and_cap(tmp_path: Path) -> None:
    long_result = "a" * 120 + "\n" + "b" * 129
    assert len(long_result) == 250
    agent = _sdk_agent(
        tmp_path,
        [
            _tool_use("toolu_1", "Bash", {"command": "make report"}),
            _tool_result("toolu_1", long_result),
            _text("done"),
            _result("done"),
        ],
    )

    lines = list(agent.run("prompt", cwd=tmp_path, timeout=60))

    assert lines == [
        "[Bash] make report",
        "  | " + "a" * 120,
        "  | " + "b" * 79 + "...",
        "done",
    ]


def test_sdk_user_text_never_emitted(tmp_path: Path) -> None:
    agent = _sdk_agent(
        tmp_path,
        [
            _user([{"type": "text", "text": "OPERATOR-TEXT-BLOCK-MUST-NOT-LOG"}]),
            _user("OPERATOR-STRING-CONTENT-MUST-NOT-LOG"),
            _text("done"),
            _result("done"),
        ],
    )

    lines = list(agent.run("prompt", cwd=tmp_path, timeout=60))

    assert lines == ["done"]


def test_sdk_tool_result_cannot_signal_completion(tmp_path: Path) -> None:
    """The real engineer loop over the real SDK runner: a tool that prints
    the marker must not end the loop as complete."""
    config = _loop_config(tmp_path)
    agent = _sdk_agent(tmp_path, _marker_stream())

    result = run_loop(config, PlainUI(no_color=True), agent, tmp_path, timeouts=BOUNDED)

    assert result.completed is False


def _sdk_heredoc_stream() -> list[dict[str, Any]]:
    """A multi-line Bash announcement, then a normal reply and result:
    isolates the runner's own pipe-splitting defect (#598) from the
    unrelated no-result-event fallback path the CLI test exercises."""
    return [
        _tool_use("toolu_1", "Bash", {"command": HEREDOC_COMMAND}),
        _text("Still working."),
        _result("Still working."),
    ]


def test_sdk_multiline_tool_call_stays_one_element(tmp_path: Path) -> None:
    """A multi-line Bash announcement crosses the runner's pipe as ONE
    element (#598): printed raw, the pipe's own physical-line-splitting
    would turn it into several, one of which is the bare completion
    marker with no prefix."""
    agent = _sdk_agent(tmp_path, _sdk_heredoc_stream())

    lines = list(agent.run("prompt", cwd=tmp_path, timeout=60))

    assert lines == [f"[Bash] {HEREDOC_COMMAND}", "Still working."]


def test_sdk_multiline_tool_call_cannot_signal_completion(tmp_path: Path) -> None:
    """The real engineer loop over the real SDK runner: a Bash command
    whose own text embeds the marker must not end the loop as complete
    (#598). Before the fix, the runner printed the announcement raw, the
    pipe split it into separate physical lines, and one of them was the
    bare marker - matched by loop.py's per-line completion check."""
    config = _loop_config(tmp_path)
    agent = _sdk_agent(tmp_path, _sdk_heredoc_stream())

    result = run_loop(config, PlainUI(no_color=True), agent, tmp_path, timeouts=BOUNDED)

    assert result.completed is False


def _sdk_multiline_text_stream() -> list[dict[str, Any]]:
    """A multi-line assistant TEXT block embeds the marker on its own
    inner line, followed by a plain reply and result (#598): isolates the
    runner's TextBlock rendering from the ToolUseBlock path the heredoc
    tests above exercise."""
    return [
        _text("Checked the prompt file.\n<promise>COMPLETE</promise>\nwas not written by me."),
        _text("Still working."),
        _result("Still working."),
    ]


def test_sdk_multiline_text_block_stays_one_element(tmp_path: Path) -> None:
    """A multi-line assistant text block crosses the runner's pipe as ONE
    element (#598): printed raw, the pipe's own physical-line-splitting
    would turn it into three, one of which is the bare completion marker
    with no prefix."""
    agent = _sdk_agent(tmp_path, _sdk_multiline_text_stream())

    lines = list(agent.run("prompt", cwd=tmp_path, timeout=60))

    assert lines == [
        "Checked the prompt file.\n<promise>COMPLETE</promise>\nwas not written by me.",
        "Still working.",
    ]


def test_sdk_multiline_text_block_cannot_signal_completion(tmp_path: Path) -> None:
    """The real engineer loop over the real SDK runner: an assistant text
    block whose own body embeds the marker on an inner line must not end
    the loop as complete (#598)."""
    config = _loop_config(tmp_path)
    agent = _sdk_agent(tmp_path, _sdk_multiline_text_stream())

    result = run_loop(config, PlainUI(no_color=True), agent, tmp_path, timeouts=BOUNDED)

    assert result.completed is False


# --- the SDK runner's diagnostics are not agent lines (#727) ----------------

#: The warning asyncio logs when its child watcher's ``waitpid`` finds a
#: pid something else already reaped (#727). The runner below logs it, and
#: the agent below says it, word for word.
UNKNOWN_CHILD = "Unknown child process pid 4242, will report returncode 255"

#: Imported by every Python process the test starts with ``PYTHONPATH``
#: pointing at it (the leash runs ``-I -S`` and imports no site). The
#: runner logs the asyncio warning through ``logging``, prints an unframed
#: line on its stdout and writes to its stderr at exit. Any other process,
#: which here is the fake CLI, writes to the stderr it inherits from the
#: runner, with NO newline: on a pipe shared with stdout those bytes join
#: the runner's next agent line, the way a diagnostic written between the
#: two writes of one ``print`` does.
_DIAGNOSTIC_SITECUSTOMIZE = f"""\
import atexit, logging, sys
if "kstrl.agents.sdk_runner" in sys.orig_argv:
    logging.getLogger("asyncio").warning({UNKNOWN_CHILD!r})
    print("RUNNER-UNFRAMED-STDOUT", flush=True)
    atexit.register(lambda: sys.stderr.write("RUNNER-STDERR-AT-EXIT\\n"))
else:
    sys.stderr.write("CLI-STDERR-DIAGNOSTIC")
"""


def test_sdk_runner_diagnostics_never_become_agent_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Everything the runner's process tree writes outside the framed
    contract is a diagnostic: it reaches the kstrl log and never the
    lines ``run`` yields (#727). The agent's own line reads exactly like
    the asyncio warning, so a fix that filters by text drops it."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_DIAGNOSTIC_SITECUSTOMIZE, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(site))
    agent = _sdk_agent(tmp_path, [_text(UNKNOWN_CHILD), _text("done"), _result("done")])

    with caplog.at_level(logging.WARNING, logger="kstrl.agents.claude_sdk"):
        lines = list(agent.run("prompt", cwd=tmp_path, timeout=60))

    assert lines == [UNKNOWN_CHILD, "done"]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for diagnostic in (
        UNKNOWN_CHILD,
        "RUNNER-UNFRAMED-STDOUT",
        "RUNNER-STDERR-AT-EXIT",
        "CLI-STDERR-DIAGNOSTIC",
    ):
        assert diagnostic in logged
