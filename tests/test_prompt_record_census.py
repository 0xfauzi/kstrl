"""#532's guard: no agent call inside a run can go out without a prompt record.

Two layers, each closed over what it counts rather than over a list of
known offenders.

LAYER 1, THE SEAM. Every ``DeadlineStreamer`` built anywhere in ``kstrl/``
is counted per function, and so is every raw process spawn inside
``kstrl/agents/``. A construction that pipes a prompt must be preceded, in
the same function, by exactly one ``record_prompt`` call. A new adapter,
or a new spawn in an existing one, is an unexplained census delta.

LAYER 2, THE IDENTITY. ``record_prompt`` writes only inside a
``recording_prompts`` scope, so a call site that runs an agent outside one
sends a prompt with no record. Every call that hands a prompt towards an
agent (``run_loop``, ``collect_agent_output``, ``<agent>.run``, and the
role entries the pipeline calls) is counted per function, and each must be
lexically inside ``with recording_prompts(...)`` in its own function, or be
enrolled below with the name of the scope that covers it.

Both pins are DERIVED BY RUNNING: empty the dict, run the test, and read
the ``Found:`` dict out of the failure. Never edit a count by hand.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

KSTRL = Path(__file__).resolve().parent.parent / "kstrl"

STREAMER = "DeadlineStreamer"
RECORDER = "record_prompt"
SCOPE = "recording_prompts"

#: ``.run(...)`` receivers that are not agents: the stdlib process runner,
#: the event loop runner, and the Textual app. Exact names only, because
#: this set CLEARS a site and must not match anything wider.
NOT_AGENTS = frozenset({"subprocess", "asyncio", "app"})

#: Calls that hand a prompt towards an agent under a name other than ``run``.
AGENT_ENTRIES = frozenset(
    {
        "run_loop",
        "collect_agent_output",
        "run_review",
        "run_security_review",
        "distill_facts",
        "_run_reviewer",
        "review_commit",
    }
)

RAW_SPAWN_MODULES = {
    "subprocess": frozenset({"Popen", "run", "call", "check_call", "check_output"}),
    "os": frozenset(
        {"system", "popen", "posix_spawn", "posix_spawnp", "execv", "execvp", "execve"}
        | {"execl", "execlp", "execle", "execvpe", "spawnv", "spawnvp", "spawnl", "spawnlp"}
    ),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
}


@dataclass(frozen=True)
class Site:
    key: str  # "<file relative to kstrl/>:<qualname>"
    kind: str  # streamer | record | agent-call | raw-spawn
    lineno: int
    scoped: bool


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else ""
    return ""


def _callee(call: ast.Call) -> str:
    """The called name's last part: ``x.y.run`` -> ``run``, ``f`` -> ``f``."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _kinds(call: ast.Call, in_agents: bool) -> list[str]:
    name = _callee(call)
    kinds: list[str] = []
    if name == STREAMER:
        kinds.append("streamer")
    if name == RECORDER:
        kinds.append("record")
    if name in AGENT_ENTRIES:
        kinds.append("agent-call")
    if (
        name == "run"
        and isinstance(call.func, ast.Attribute)
        and _dotted(call.func.value) not in NOT_AGENTS
    ):
        kinds.append("agent-call")
    if (
        in_agents
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in RAW_SPAWN_MODULES.get(_dotted(call.func.value), frozenset())
    ):
        kinds.append("raw-spawn")
    return kinds


def _opens_scope(node: ast.With | ast.AsyncWith) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call) and _callee(item.context_expr) == SCOPE
        for item in node.items
    )


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _scope_name(node: ast.AST) -> str:
    return "<lambda>" if isinstance(node, ast.Lambda) else getattr(node, "name", "")


def sites(source: str, rel: str) -> list[Site]:
    """Every counted call in ``source``, attributed to its innermost function.

    ``scoped`` is lexical and stops at a function boundary: a helper defined
    inside a ``with recording_prompts(...)`` block and called elsewhere is
    not credited with the scope, because at run time it may not be in it.
    """
    in_agents = rel.startswith("agents/")
    found: list[Site] = []
    stack: list[tuple[ast.AST, tuple[str, ...], bool]] = [(ast.parse(source), (), False)]
    while stack:
        node, qual, scoped = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                stack.append((child, (*qual, _scope_name(child)), False))
                continue
            key = f"{rel}:{'.'.join(qual) or '<module>'}"
            if isinstance(child, ast.Call):
                found.extend(Site(key, k, child.lineno, scoped) for k in _kinds(child, in_agents))
            opens = isinstance(child, ast.With | ast.AsyncWith) and _opens_scope(child)
            stack.append((child, qual, scoped or opens))
    return sorted(found, key=lambda site: (site.key, site.lineno, site.kind))


def _census() -> list[Site]:
    out: list[Site] = []
    for path in sorted(KSTRL.rglob("*.py")):
        rel = path.relative_to(KSTRL).as_posix()
        out.extend(sites(path.read_text(encoding="utf-8"), rel))
    return out


def _counts(kind: str) -> dict[str, int]:
    return dict(sorted(Counter(s.key for s in _census() if s.kind == kind).items()))


# --- layer 1: the seam ------------------------------------------------------

EXPECTED_STREAMERS: dict[str, int] = {
    "agents/claude_code.py:ClaudeCodeAgent.run": 1,
    "agents/claude_sdk.py:ClaudeSdkAgent.run": 1,
    "agents/codex.py:CodexAgent.run": 1,
    "agents/custom.py:CustomAgent.run": 1,
    "agents/liveness.py:_stream": 1,
}

EXPECTED_RECORDERS: dict[str, int] = {
    "agents/claude_code.py:ClaudeCodeAgent.run": 1,
    "agents/claude_sdk.py:ClaudeSdkAgent.run": 1,
    "agents/codex.py:CodexAgent.run": 1,
    "agents/custom.py:CustomAgent.run": 1,
}

EXPECTED_RAW_SPAWNS: dict[str, int] = {
    "agents/codex.py:CodexAgent._codex_supports_output_last_message": 1,
    "agents/proc.py:DeadlineStreamer.__init__": 1,
}

#: Streamer constructions that pipe no agent-call prompt, with the reason.
UNRECORDED_STREAMERS: dict[str, str] = {
    "agents/liveness.py:_stream": (
        "the liveness probe: a fixed one-line probe sent while choosing an "
        "agent, before any run exists; not an agent call of a run"
    ),
}


def test_the_streamer_census_has_not_moved() -> None:
    found = _counts("streamer")
    assert found == EXPECTED_STREAMERS, f"Found: {found}"


def test_the_recorder_census_has_not_moved() -> None:
    found = _counts("record")
    assert found == EXPECTED_RECORDERS, f"Found: {found}"


def test_the_raw_spawn_census_in_the_adapters_has_not_moved() -> None:
    found = _counts("raw-spawn")
    assert found == EXPECTED_RAW_SPAWNS, f"Found: {found}"


def test_every_streamer_that_pipes_a_prompt_records_it_first() -> None:
    census = _census()
    records = {
        s.key: [r.lineno for r in census if r.kind == "record" and r.key == s.key] for s in census
    }
    offenders = [
        f"{s.key} line {s.lineno}: records {records[s.key]}"
        for s in census
        if s.kind == "streamer"
        if s.key not in UNRECORDED_STREAMERS
        if len(records[s.key]) != 1 or records[s.key][0] > s.lineno
    ]
    assert offenders == []
    # Control: the rule above passes vacuously on an empty census.
    assert len([s for s in census if s.kind == "streamer"]) >= 5


# --- layer 2: the identity --------------------------------------------------

EXPECTED_AGENT_CALLS: dict[str, int] = {
    "agents/logging.py:LoggingAgent.run": 1,
    "cli.py:_understand_core": 1,
    "decompose.py:_decompose_spec_impl": 1,
    "decompose.py:collect_agent_output": 1,
    "factory.py:_run_component": 1,
    "feature_cmd.py:run_feature": 3,
    "gepa_adapter.py:ReflectionModel.__call__": 1,
    "integration_phase.py:_review_round": 1,
    "integration_phase.py:_run_reviewer": 1,
    "integration_phase.py:review_commit": 1,
    "knowledge.py:distill_facts": 1,
    "loop.py:run_loop": 1,
    "pipeline.py:ComponentPipeline._phase_distill": 1,
    "pipeline.py:ComponentPipeline._phase_review": 1,
    "pipeline.py:ComponentPipeline._phase_security": 1,
    "review.py:run_review": 1,
    "security.py:run_security_review": 1,
}

#: Agent-call sites whose scope is opened by a CALLER, named here so the
#: e2e test that proves it can be found. Everything else must be scoped
#: lexically.
CALLER_SCOPED: dict[str, str] = {
    "agents/logging.py:LoggingAgent.run": "a tee around the adapter it wraps; its caller's scope",
    "decompose.py:collect_agent_output": "the drain helper; review, security and distill call it",
    "gepa_adapter.py:ReflectionModel.__call__": "offline prompt search outside any run (#530)",
    "integration_phase.py:_run_reviewer": "integration_phase._review_round",
    "integration_phase.py:review_commit": "integration_phase._review_round, via _run_reviewer",
    "knowledge.py:distill_facts": "pipeline.ComponentPipeline._phase_distill",
    "loop.py:run_loop": "every run_loop caller, each scoped lexically",
    "review.py:run_review": "pipeline._phase_review and integration_phase._review_round",
    "security.py:run_security_review": "pipeline.ComponentPipeline._phase_security",
}


#: CALLER_SCOPED sites that run outside any run, so no caller opens a scope.
NO_RUN_CALLERS = frozenset({"gepa_adapter.py:ReflectionModel.__call__"})


def test_the_agent_call_census_has_not_moved() -> None:
    found = _counts("agent-call")
    assert found == EXPECTED_AGENT_CALLS, f"Found: {found}"


def test_every_agent_call_is_inside_a_recording_scope_or_names_the_caller_that_opens_it() -> None:
    census = [s for s in _census() if s.kind == "agent-call"]
    unscoped = sorted({s.key for s in census if not s.scoped and s.key not in CALLER_SCOPED})
    assert unscoped == []
    stale = sorted(set(CALLER_SCOPED) - {s.key for s in census})
    assert stale == [], "enrolled sites that no longer exist"
    # Control: some sites really are lexically scoped, so the walk sees `with`.
    assert len([s for s in census if s.scoped]) >= 6


def test_a_site_cleared_by_its_caller_has_its_callers_counted() -> None:
    """CALLER_SCOPED clears a site on the claim that its caller opens the
    scope. That claim is checked only if every call OF that function is
    itself counted, so its name must be an agent entry (or ``run``, which
    every ``<receiver>.run(`` call counts). Otherwise a new caller of it,
    outside any scope, would be invisible."""
    uncounted = sorted(
        key
        for key in CALLER_SCOPED
        if key not in NO_RUN_CALLERS
        if key.rsplit(":", 1)[1].rsplit(".", 1)[-1] not in AGENT_ENTRIES | {"run"}
    )
    assert uncounted == []
    assert NO_RUN_CALLERS <= set(CALLER_SCOPED)


# --- controls on synthetic source -------------------------------------------


def _one(source: str, rel: str = "x.py") -> list[tuple[str, str, bool]]:
    return [(s.key, s.kind, s.scoped) for s in sites(source, rel)]


def test_an_agent_run_outside_a_scope_is_counted_unscoped() -> None:
    assert _one("def f(agent, p):\n    agent.run(p)\n") == [("x.py:f", "agent-call", False)]


def test_an_agent_run_inside_a_scope_is_counted_scoped() -> None:
    source = "def f(agent, p, c):\n    with recording_prompts(c):\n        agent.run(p)\n"
    assert _one(source) == [
        ("x.py:f", "agent-call", True),
    ]


def test_some_other_with_does_not_scope() -> None:
    source = "def f(agent, p):\n    with open(p):\n        agent.run(p)\n"
    assert _one(source) == [("x.py:f", "agent-call", False)]


def test_a_helper_defined_inside_a_scope_is_not_credited_with_it() -> None:
    source = (
        "def f(agent, p, c):\n"
        "    with recording_prompts(c):\n"
        "        def later():\n"
        "            return agent.run(p)\n"
        "    return later\n"
    )
    assert ("x.py:f.later", "agent-call", False) in _one(source)


def test_subprocess_run_is_not_an_agent_call_but_is_a_raw_spawn_in_the_adapters() -> None:
    source = "import subprocess\ndef f():\n    subprocess.run(['codex'])\n"
    assert _one(source, "agents/new.py") == [("agents/new.py:f", "raw-spawn", False)]


def test_a_streamer_with_no_record_is_an_offender_shape() -> None:
    source = "def run(self, prompt):\n    s = DeadlineStreamer(['x'], stdin_text=prompt)\n"
    assert _one(source, "agents/new.py") == [("agents/new.py:run", "streamer", False)]


# --- disclosed blind spots: each XPASSes loudly the day the walk widens -----


@pytest.mark.xfail(strict=True, reason="a call through getattr is not a named call")
def test_blind_spot_getattr_run() -> None:
    assert _one("def f(agent, p):\n    getattr(agent, 'run')(p)\n") != []


@pytest.mark.xfail(strict=True, reason="a bound-method alias is called by another name")
def test_blind_spot_aliased_run() -> None:
    assert _one("def f(agent, p):\n    go = agent.run\n    go(p)\n") == [
        ("x.py:f", "agent-call", False)
    ]


@pytest.mark.xfail(strict=True, reason="raw spawns are counted inside kstrl/agents/ only")
def test_blind_spot_raw_agent_spawn_outside_the_adapters() -> None:
    source = "import subprocess\ndef f(p):\n    subprocess.Popen(['claude', '--print'])\n"
    assert _one(source, "elsewhere.py") != []
