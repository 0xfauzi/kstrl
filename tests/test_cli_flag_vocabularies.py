"""A flag that sets a closed config field accepts exactly what the field accepts (#565).

Before #565 the CLI restated two vocabularies by hand. ``--agent-type`` on
``ks decompose`` and ``ks factory`` was ``click.Choice(["auto",
"claude-code", "claude-sdk", "codex"])``, so ``claude`` and ``custom``,
which ``[agent] type`` and ``KSTRL_AGENT_TYPE`` accept, were refused with
exit 2; ``ks config show --agent-type`` took any string and showed it as
the resolved type.

End to end through the real click tree. For every row of
``FLAG_FIELDS`` and every probe value, the ORACLE is the real config
loader reading a real kstrl.toml with that value
(``collect_config_problems``), and the flag must agree with it: a value
the loader accepts reaches the command body, and a value it refuses is
refused by click with exit 2, naming the option, before the body runs.
The probes are every value the ledger says the field accepts, each in
upper case, one padded with spaces, the empty string and ``BAD_VALUE``,
so a flag that is narrower OR wider than its field fails here.

Where each command stops once its options parsed, measured on b275c6a:
``ks factory`` with neither ``--spec`` nor ``--manifest`` exits 2 from its
body; ``ks decompose`` without ``--spec`` exits 2 on the missing option,
which click checks after the options given on the command line; ``ks
config show`` exits 0 and prints the resolved row.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from kstrl.agents import ClaudeCodeAgent, ClaudeSdkAgent, CodexAgent, canonical_agent_type
from kstrl.cli import cli
from kstrl.config_preflight import collect_config_problems
from kstrl.workqueue import ItemState
from tests.helpers.closed_vocabulary import BAD_VALUE, CLOSED, FLAG_FIELDS

#: What each command prints once every option given has parsed.
BODY_REACHED = {
    ("factory",): "Either --spec or --manifest is required",
    ("decompose",): "Missing option '--spec'",
}


def _flag_choices(path: tuple[str, ...], option: str) -> list[str]:
    """What the flag itself lists, read from the real click tree ([] if not a Choice)."""
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group), path
        command = command.commands[name]
    param = next(p for p in command.params if option in p.opts)
    if not isinstance(param.type, click.Choice):
        return []
    return [str(choice) for choice in param.type.choices]


def _probes(path: tuple[str, ...], option: str, key: tuple[str, str]) -> list[str]:
    """The field's values, the flag's own choices, and spellings around them.

    The flag's choices are probes too, so a flag WIDER than its field (a
    value the flag lists and the loader refuses) fails, not only a narrower one.
    """
    accepted = CLOSED[key].accepted
    upper = [value.upper() for value in accepted if value.upper() != value]
    probes = [*accepted, *_flag_choices(path, option), *upper, f" {accepted[0]} ", "", BAD_VALUE]
    return list(dict.fromkeys(probes))


_CASES = [
    (path, option, value)
    for (path, option), key in sorted(FLAG_FIELDS.items())
    for value in _probes(path, option, key)
]


def _door_accepts(root: Path, key: tuple[str, str], value: str) -> bool:
    """The oracle: does kstrl.toml accept ``value`` for this field?"""
    field = CLOSED[key]
    root.mkdir()
    (root / "kstrl.toml").write_text(
        f"[{field.section}]\n{field.key} = {json.dumps(value)}\n", encoding="utf-8"
    )
    warnings: list[str] = []
    problems = collect_config_problems(root, warn=warnings.append)
    return problems == []


def _invoke(path: tuple[str, ...], option: str, value: str, root: Path) -> Result:
    root.mkdir()
    return CliRunner().invoke(cli, [*path, option, value, "--root", str(root)])


@pytest.mark.parametrize(
    ("path", "option", "value"),
    _CASES,
    ids=[f"{' '.join(p)}-{o}-{v!r}" for p, o, v in _CASES],
)
def test_the_flag_agrees_with_the_config_field(
    tmp_path: Path, path: tuple[str, ...], option: str, value: str
) -> None:
    key = FLAG_FIELDS[(path, option)]
    accepted = _door_accepts(tmp_path / "door", key, value)

    result = _invoke(path, option, value, tmp_path / "cli")

    refusal = f"Invalid value for '{option}'"
    if not accepted:
        assert result.exit_code == 2, result.output
        assert refusal in result.output, result.output
        assert repr(value) in result.output, result.output
        return
    assert refusal not in result.output, (value, result.output)
    if path == ("config", "show"):
        assert result.exit_code == 0, result.output
        assert f"type = {value!r}  (flag)" in result.output, result.output
    else:
        assert result.exit_code == 2, result.output
        assert BODY_REACHED[path] in result.output, result.output


def test_the_oracle_accepts_and_refuses(tmp_path: Path) -> None:
    """Control: the loader is live for every row, so agreement is a measurement."""
    for n, key in enumerate(sorted(set(FLAG_FIELDS.values()))):
        assert _door_accepts(tmp_path / f"good{n}", key, CLOSED[key].accepted[0]), key
        assert not _door_accepts(tmp_path / f"bad{n}", key, BAD_VALUE), key


@pytest.mark.parametrize("state", [*(state.value for state in ItemState), BAD_VALUE])
def test_queue_ls_state_takes_exactly_the_item_states(tmp_path: Path, state: str) -> None:
    """``ks queue ls --state`` has no config field; its oracle is ``ItemState``.

    Before #565 the option took any string and its help wrote the seven
    states out by hand; the body refused an unknown one. Now click refuses
    it, naming the option, and every state the queue stores is accepted.
    """
    result = CliRunner().invoke(cli, ["queue", "ls", "--state", state, "--root", str(tmp_path)])

    if state == BAD_VALUE:
        assert result.exit_code == 2, result.output
        assert "Invalid value for '--state'" in result.output, result.output
    else:
        assert result.exit_code == 0, result.output
        assert "Queue is empty." in result.output, result.output


#: What the agent preflight says for each canonical type when no agent is
#: installed: the message names the agent the spelling selects.
_NOT_INSTALLED = {
    "claude-code": "claude not found in PATH",
    "claude-sdk": "claude-agent-sdk is not installed",
    "codex": "codex not found in PATH",
    "custom": 'Agent type "custom" is configured but no agent command is set',
    "auto": "No agent available",
}

_AGENT_SPELLINGS = [
    spelling
    for value in CLOSED[("KstrlConfig", "agent_type")].accepted
    for spelling in dict.fromkeys([value, value.upper(), f" {value} "])
]


@pytest.mark.parametrize("command", ["decompose", "factory"])
@pytest.mark.parametrize("spelling", _AGENT_SPELLINGS)
def test_an_accepted_agent_type_selects_its_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, spelling: str
) -> None:
    """A spelling the flag accepts reaches the agent it names, not only the body.

    With no agent installed, the preflight refuses with a message that
    names the agent the spelling selected, so ``" CODEX "`` must be
    reported as codex, never as an unknown type or as another agent.
    """
    for adapter in (ClaudeCodeAgent, ClaudeSdkAgent, CodexAgent):
        monkeypatch.setattr(adapter, "is_available", staticmethod(lambda: False))
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    args = [command, "--agent-type", spelling, "--spec", str(spec), "--root", str(tmp_path)]
    if command == "decompose":
        args += ["--project-name", "p"]

    result = CliRunner().invoke(cli, args)

    expected = _NOT_INSTALLED[canonical_agent_type(spelling) or "unknown"]
    assert result.exit_code == 2, result.output
    assert expected in result.output, result.output
    assert "Unknown agent type" not in result.output, result.output
