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


class TestFamilyResolverMatchesTheAdapter:
    """`_cli_family` decides the ENGINEER's family and R7.1 then picks a
    reviewer from the OTHER family. If it disagrees with `get_agent`, the
    run gets SAME-family review while the audit trail claims otherwise -
    the correlated-blind-spot failure R7.1 exists to prevent.

    Review regression: `get_agent` learned the "claude" alias while this
    mirror kept its own literal tuple, so a Claude engineer resolved as
    codex and the "cross-family" reviewer came back claude-code. The two
    had previously agreed by both being wrong, which is why fixing only
    one side made the safety property fail rather than merely mislabel.
    """

    @staticmethod
    def _expected_family(agent_type: str | None) -> str:
        return (
            "codex"
            if isinstance(get_agent(agent_type=agent_type), CodexAgent)
            else "claude-code"
        )

    @pytest.mark.parametrize(
        "agent_type",
        ["claude", "CLAUDE", " claude-code ", "claude-code", "claude-sdk",
         "codex", "auto", None],
    )
    def test_family_agrees_with_the_constructed_adapter(
        self, agent_type: str | None,
    ) -> None:
        """Both sides must read the SAME environment.

        `auto`/None auto-detect, so `get_agent` consults
        `ClaudeCodeAgent.is_available()` for real. Passing a hardcoded
        `claude_available=True` made this pass on a machine with the
        claude CLI installed and fail in CI without it - an
        environment-dependent test, which is its own defect. The flag is
        taken from the same source the adapter uses.
        """
        from kstrl.factory import _cli_family

        claude_available = ClaudeCodeAgent.is_available()
        assert _cli_family(
            None, agent_type, claude_available,
        ) == self._expected_family(agent_type)

    def test_claude_alias_is_the_claude_family(self) -> None:
        # The regression in one line: this returned "codex".
        from kstrl.factory import _cli_family

        # An explicit type never auto-detects, so the availability flag
        # is irrelevant here - assert under both to prove it.
        assert _cli_family(None, "claude", True) == "claude-code"
        assert _cli_family(None, "claude", False) == "claude-code"

    def test_unset_type_still_autodetects(self) -> None:
        """None means auto-detect, not unknown.

        `canonical_agent_type` returns None for BOTH "unset" and
        "unrecognized", so splitting them is load-bearing: without it the
        ordinary no-type-configured case raises.
        """
        from kstrl.factory import _cli_family

        assert _cli_family(None, None, True) == "claude-code"
        assert _cli_family(None, None, False) == "codex"

    def test_unknown_type_raises_like_get_agent(self) -> None:
        from kstrl.factory import _cli_family

        with pytest.raises(UnknownAgentTypeError):
            _cli_family(None, "typo-here", True)

    def test_custom_command_is_an_unknown_family(self) -> None:
        # Pre-existing contract: a custom command's family cannot be known.
        from kstrl.factory import _cli_family

        assert _cli_family("echo hi", "claude", True) is None

    def test_rotation_picks_a_genuinely_different_family(self) -> None:
        """End of the chain: a "claude" engineer must not get a Claude
        reviewer when both CLIs are installed."""
        from kstrl.factory import _cli_family

        engineer_family = _cli_family(None, "claude", ClaudeCodeAgent.is_available())
        assert engineer_family == "claude-code"
        # The rotation's job is to choose the OTHER family; with the old
        # behavior engineer_family was "codex" and it chose claude-code -
        # the same family the engineer actually used.
        assert engineer_family != "codex"
