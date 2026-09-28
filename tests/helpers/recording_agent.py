"""``RecordingAgent``: a fake agent that records prompts and cwds.

Moved out of ``tests/test_review_payload.py`` (#266) when that file
folded into ``tests/test_review_coverage.py``, so the three other
modules that imported it (``tests/test_delivered_prompts.py``,
``tests/test_prompt_versions.py``, ``tests/test_spec_issue_routing.py``)
have one shared home instead of a test module as their dependency
(``tests/test_helper_import_direction.py`` forbids the reverse).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path


class RecordingAgent:
    """Agent that records prompts and replies with a fixed output."""

    def __init__(
        self,
        output: str,
        name: str = "recording-agent",
        on_prompt: Callable[[str], None] | None = None,
    ):
        self._output = output
        self._name = name
        # Fires when the agent is invoked, which is the window between
        # the harness's own measurement and the reviewer's. A hook
        # rather than a reply-computing callback: the reply is known
        # before the call, and assertions inside a callback would be
        # HIDDEN - run_review catches every exception, so a failed
        # precondition surfaces as "Reviewer agent failed" and reads as
        # the feature under test breaking.
        self._on_prompt = on_prompt
        self.calls = 0
        self.prompts: list[str] = []
        self.cwds: list[Path | None] = []

    @property
    def name(self) -> str:
        return self._name

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        self.calls += 1
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        if self._on_prompt is not None:
            self._on_prompt(prompt)
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return None
