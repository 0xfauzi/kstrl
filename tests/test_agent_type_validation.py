"""Agent-type resolution: one vocabulary, no silent substitution.

Two layers used to disagree. The CLI preflight canonicalized "claude" to
"claude-code" via a private alias table; `get_agent` did not, and its
final fallthrough returned the codex adapter. A caller that did not go
through the CLI - a test, a programmatic embedder, any future entry
point - therefore got a DIFFERENT MODEL than the documented spelling
names, silently.
"""

from __future__ import annotations

import pytest

from kstrl.agents import (
    AGENT_TYPE_ALIASES,
    VALID_AGENT_TYPES,
    ClaudeCodeAgent,
    ClaudeSdkAgent,
    CodexAgent,
    CustomAgent,
    UnknownAgentTypeError,
    canonical_agent_type,
    get_agent,
)


class TestDocumentedSpellingsResolve:
    @pytest.mark.parametrize(
        "agent_type,expected",
        [
            ("claude", ClaudeCodeAgent),      # the alias that used to give Codex
            ("claude-code", ClaudeCodeAgent),
            ("claude-sdk", ClaudeSdkAgent),
            ("codex", CodexAgent),
        ],
    )
    def test_type_selects_its_adapter(
        self, agent_type: str, expected: type,
    ) -> None:
        assert isinstance(get_agent(agent_type=agent_type), expected)

    def test_claude_alias_is_not_codex(self) -> None:
        # The regression in one line: this returned CodexAgent.
        assert isinstance(get_agent(agent_type="claude"), ClaudeCodeAgent)

    @pytest.mark.parametrize("spelling", ["CLAUDE", " claude ", "Claude-Code"])
    def test_case_and_whitespace_tolerated(self, spelling: str) -> None:
        assert isinstance(get_agent(agent_type=spelling), ClaudeCodeAgent)

    def test_auto_and_none_autodetect(self) -> None:
        for value in ("auto", None):
            agent = get_agent(agent_type=value)
            assert isinstance(agent, (ClaudeCodeAgent, CodexAgent))


class TestUnknownTypesRaise:
    @pytest.mark.parametrize("bad", ["typo-here", "claude4", "gpt", "sonnet"])
    def test_unknown_type_is_an_error_not_a_fallback(self, bad: str) -> None:
        with pytest.raises(UnknownAgentTypeError, match="unknown"):
            get_agent(agent_type=bad)

    def test_error_lists_the_valid_spellings(self) -> None:
        with pytest.raises(UnknownAgentTypeError) as exc:
            get_agent(agent_type="nope")
        message = str(exc.value)
        for spelling in ("claude-code", "codex", "auto"):
            assert spelling in message

    def test_error_does_not_advertise_the_empty_alias(self) -> None:
        # "" maps to auto internally; showing it in a list of valid
        # values would be nonsense to read.
        assert "" not in VALID_AGENT_TYPES
        with pytest.raises(UnknownAgentTypeError) as exc:
            get_agent(agent_type="nope")
        assert "one of ," not in str(exc.value)

    def test_custom_without_a_command_raises(self) -> None:
        # Previously fell through to the codex adapter.
        with pytest.raises(UnknownAgentTypeError, match="no agent command"):
            get_agent(agent_type="custom")

    def test_custom_with_a_command_works(self) -> None:
        assert isinstance(get_agent("echo hi", agent_type="custom"), CustomAgent)

    def test_an_explicit_command_wins_regardless_of_type(self) -> None:
        # agent_cmd short-circuits before any type validation, which is
        # the pre-existing contract.
        assert isinstance(get_agent("echo hi", agent_type="nonsense"), CustomAgent)


class TestOneVocabulary:
    def test_cli_uses_the_shared_table(self) -> None:
        from kstrl.cli import _AGENT_TYPE_ALIASES

        assert _AGENT_TYPE_ALIASES is AGENT_TYPE_ALIASES

    @pytest.mark.parametrize("spelling", sorted(AGENT_TYPE_ALIASES))
    def test_every_alias_resolves_or_raises_deliberately(
        self, spelling: str,
    ) -> None:
        """No alias may reach the fallthrough by accident.

        "custom" raises without a command; everything else constructs.
        """
        canonical = canonical_agent_type(spelling)
        assert canonical is not None
        if canonical == "custom":
            with pytest.raises(UnknownAgentTypeError):
                get_agent(agent_type=spelling)
        else:
            assert get_agent(agent_type=spelling) is not None

    def test_canonical_agent_type_rejects_unknown(self) -> None:
        assert canonical_agent_type("typo") is None
        assert canonical_agent_type(None) is None
