"""Every claude session that kstrl starts loads no MCP server, in every role
and with the sandbox on or off (#700).

End to end: the real ``ks`` with a stub ``claude`` that records the argv of
each call. The helpers are the ones of tests/test_sandbox_engineer_e2e.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_sandbox_engineer_e2e import (
    _FACTORY,
    _UNDERSTAND,
    CLAUDE_TOML,
    SANDBOX_OFF_TOML,
    SANDBOX_TOML,
    _ks,
    _runs,
)

_FLAG = "--strict-mcp-config"

#: (id, ks argv). The factory with both review gates on runs the engineer, the
#: code reviewer and the security reviewer; `ks understand` runs the
#: understand role.
_ROLES = [
    ("factory", (*_FACTORY, "--review-mode", "hard", "--security-mode", "hard")),
    ("understand", _UNDERSTAND),
]


@pytest.mark.parametrize(
    "sandbox", [SANDBOX_TOML, SANDBOX_OFF_TOML], ids=["sandbox-on", "sandbox-off"]
)
@pytest.mark.parametrize("argv", [row[1] for row in _ROLES], ids=[row[0] for row in _ROLES])
def test_every_claude_session_kstrl_starts_gets_strict_mcp_config(
    tmp_path: Path, sandbox: str, argv: tuple[str, ...]
) -> None:
    ran = _ks(tmp_path, CLAUDE_TOML + sandbox, argv, {}, clis=("claude",))
    runs = _runs(tmp_path, "bin/claude")
    assert runs, ran
    missing = [call for call in runs if _FLAG not in call]
    assert not missing, (missing, ran)
    if argv is _ROLES[0][1]:
        # The engineer, and at least one reviewer, which has no write guard.
        assert len(runs) >= 2, (runs, ran)
