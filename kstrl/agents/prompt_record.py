"""The record of the exact prompt each agent call was given (#532).

Every adapter that pipes a prompt to an agent CLI calls
:func:`record_prompt` immediately before it spawns the CLI. The record is
one JSON file per call under the run's own directory::

    .kstrl/runs/<run_id>/prompts/<component>/<role>-a<attempt>-c<call>.json

WHO THE CALL IS FOR comes from :func:`recording_prompts`, which the code
that knows the identity (the factory worker, the pipeline's review,
security and distill phases, the integration review, the architect, the
understand and feature loops) opens around the work that runs the agent.
The adapter knows none of it: ``Agent.run`` carries a prompt, a cwd and a
timeout, and changing that protocol would touch every fake agent in the
suite. Outside a scope nothing is written, which is the state of every
call made outside a run (the liveness probe, a test
driving an adapter directly). ``tests/test_prompt_record_census.py`` is
what stops a new call site inside a run from landing outside a scope.

A FAILED WRITE FAILS THE CALL. The write happens before the spawn, so a
call whose record cannot be written raises ``OSError`` out of
``Agent.run`` having spent nothing. The alternative, an infrastructure
finding and a call that proceeds, leaves #508's scorer and #217's
attribution reading "no record" for a call that happened, which is the
state this module exists to end.

NOTHING IS REDACTED. The record is the exact text the CLI received,
because a scorer that asks whether a fact reached the engineer needs the
exact text. What a prompt carries is already on disk in the project
(``CLAUDE.md``, the PRD, the knowledge store, the operator's memory
file), in the diff the reviewer reads, or in the transcript; the record
lands under ``.kstrl/``, which ``ks init`` gitignores, with the mode
every other ``.kstrl/`` file gets (``kstrl/atomicio.py`` has no mode
parameter by design).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kstrl.atomicio import atomic_write_json
from kstrl.version import kstrl_version

#: Bumped when a field changes meaning; the reader refuses any other value.
RECORD_SCHEMA: Final = 1

#: The adapter that piped the prompt, one spelling per adapter.
AGENT_CLIS: Final = frozenset({"claude-code", "claude-sdk", "codex", "custom"})

#: Every field a record carries, with its type: the writer's keys and the
#: reader's checks come from this one mapping.
_FIELDS: Final[dict[str, type]] = {
    "schema": int,
    "run_id": str,
    "kstrl_version": str,
    "component": str,
    "role": str,
    "attempt": int,
    "call": int,
    "agent_cli": str,
    "prompt": str,
}


@dataclass(frozen=True)
class AgentCall:
    """Who the agent calls inside one :func:`recording_prompts` scope are for."""

    run_root: Path  # <project>/.kstrl/runs/<run_id>
    run_id: str
    component: str
    role: str
    attempt: int


_CURRENT: ContextVar[AgentCall | None] = ContextVar("kstrl_agent_call", default=None)


@contextmanager
def recording_prompts(call: AgentCall) -> Iterator[None]:
    """Record every agent call made inside this block as ``call``.

    A scope always carries an identity (#567). A call made outside a run
    opens no scope at all; ``None`` was accepted here until #567, and a
    scope opened with it recorded nothing while looking recorded to
    ``tests/test_prompt_record_census.py``.
    """
    token = _CURRENT.set(call)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def prompts_dir(run_root: Path, component: str) -> Path:
    """Where one component's records live."""
    return run_root / "prompts" / component


def record_prompt(prompt: str, *, agent_cli: str) -> Path | None:
    """Write the record for the call about to be spawned.

    Returns the record's path, or None outside a scope. Raises ``OSError``
    when the write fails, and ``ValueError`` for an ``agent_cli`` outside
    :data:`AGENT_CLIS`; both before any spawn, so the call fails with
    nothing spent.
    """
    if agent_cli not in AGENT_CLIS:
        raise ValueError(f"unknown agent_cli {agent_cli!r}; expected one of {sorted(AGENT_CLIS)}")
    call = _CURRENT.get()
    if call is None:
        return None
    directory = prompts_dir(call.run_root, call.component)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{call.role}-a{call.attempt}-c"
    number = 1 + sum(1 for _ in directory.glob(f"{stem}*.json"))
    path = directory / f"{stem}{number}.json"
    atomic_write_json(
        path,
        {
            "schema": RECORD_SCHEMA,
            "run_id": call.run_id,
            "kstrl_version": kstrl_version(),
            "component": call.component,
            "role": call.role,
            "attempt": call.attempt,
            "call": number,
            "agent_cli": agent_cli,
            "prompt": prompt,
        },
    )
    return path
