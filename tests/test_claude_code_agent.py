"""``ClaudeCodeAgent.run`` against a real child process.

A fake ``claude`` executable goes on PATH (``tests.helpers.executables``),
so what is asserted is what the agent does with a real pipe, a real
result event and a real hanging child, not what a mocked ``Popen`` was
called with. The former contents of this file (name/default/is_available/
final_message and the argv-shape tests over a mocked Popen) were unit
tests on a mocked collaborator and were removed in the consolidation.
Kept as its own file rather than folded into ``test_prompt_record.py``
because the fold took that file past the 800-line ratchet.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kstrl.agents.claude_code import ClaudeCodeAgent
from tests.helpers.executables import put_on_path


class TestClaudeCodeAgentRealSubprocess:
    def test_run_yields_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        put_on_path(
            tmp_path,
            monkeypatch,
            "claude",
            "#!/bin/sh\ncat > /dev/null\necho hello\necho world\n",
        )

        agent = ClaudeCodeAgent()
        lines = list(agent.run("test prompt", cwd=tmp_path))

        assert lines == ["hello", "world"]
        assert agent.final_message == "world"

    def test_result_event_breaks_stdout_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a result event arrives, ``run`` stops reading stdout and
        sets ``final_message`` - the line the fake CLI prints after the
        result event must never reach the caller."""
        put_on_path(
            tmp_path,
            monkeypatch,
            "claude",
            "#!/bin/sh\n"
            "cat > /dev/null\n"
            'echo \'{"type":"assistant","message":{"content":'
            '[{"type":"text","text":"working..."}]}}\'\n'
            'echo \'{"type":"result","subtype":"success","result":'
            '"All done","duration_ms":1000}\'\n'
            'echo "THIS SHOULD NOT BE YIELDED"\n',
        )

        agent = ClaudeCodeAgent()
        lines = list(agent.run("test", cwd=tmp_path))

        assert "working..." in lines
        assert "THIS SHOULD NOT BE YIELDED" not in lines
        assert agent.final_message == "All done"

    def test_proc_wait_timeout_terminates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child that emits its result event and then hangs is killed
        rather than waited out forever. Bounded by
        ``DeadlineStreamer``'s own ``finish(timeout=10)``: measured at
        10.0s wall time against the real child (a plain ``sleep 30``
        dies on the first SIGTERM), well under the 30s the fake CLI
        sleeps for."""
        put_on_path(
            tmp_path,
            monkeypatch,
            "claude",
            '#!/bin/sh\ncat > /dev/null\necho \'{"type":"result","result":"done"}\'\nsleep 30\n',
        )

        agent = ClaudeCodeAgent()
        started = time.monotonic()
        lines = list(agent.run("test", cwd=tmp_path))
        elapsed = time.monotonic() - started

        assert lines == []
        assert agent.final_message == "done"
        assert elapsed < 20, elapsed

    def test_run_handles_broken_pipe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fake CLI closes its stdin before reading it, so the
        writer thread's ``stdin.write`` gets a real ``BrokenPipeError``.
        ``run`` must swallow it and still deliver the CLI's stdout - a
        prompt bigger than the pipe buffer (measured on this machine:
        reliable at 5MB, flaky at prompt sizes that fit in one write)
        is what makes the write actually hit the closed pipe rather
        than complete before the child gets there."""
        put_on_path(
            tmp_path,
            monkeypatch,
            "claude",
            "#!/bin/sh\nexec 0<&-\necho partial output\n",
        )

        agent = ClaudeCodeAgent()
        lines = list(agent.run("x" * (5 * 1024 * 1024), cwd=tmp_path))

        assert lines == ["partial output"]
