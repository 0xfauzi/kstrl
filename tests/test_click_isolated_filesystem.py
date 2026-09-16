"""No test reaches Click's deprecated ``CliRunner.isolated_filesystem``.

Click 8.5 deprecates it and Click 9.0 removes it, so a call that is a
DeprecationWarning today is an ``AttributeError`` on the next bump. #363
brought 8.5.0 in and the suite went from 14 warnings to 124; eight call
sites across three modules produced 110 of them.

Nothing needed the helper. ``conftest.isolate_kstrl_state`` is autouse
and function-scoped and already chdirs every test into its own empty
``tmp_path``, so the context manager was a second temporary directory
stacked on an already isolated cwd. The replacement is that cwd.

The direction is FLAGGING, per ``tests/helpers/astwalk``: this guard may
over-match, and a false positive costs a reader one line.
``astwalk.spells`` enumerates no node type and no field name, so it sees
the attribute call, a ``getattr`` spelling, a bare reference stored for
later, an ``import`` of the name and a keyword argument called it,
without naming any of those shapes. It compares by EQUALITY rather than
substring, so prose that mentions the helper (this docstring included) is
not a site.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers import astwalk

#: The deprecated attribute, spelled once so the net and the message
#: cannot drift apart.
DEPRECATED_HELPER = "isolated_filesystem"

#: Every module under ``tests/`` that spells it, and it has to stay
#: empty. ``assert_census`` proves the net still fires against a control
#: and that the corpus is non-empty before it compares, so ``{}`` here is
#: an answer rather than the silence a switched-off walk returns.
#:
#: Measured at revision 45b0deb, which carried #363's click bump:
#: ``{"tests/test_cli.py": 1, "tests/test_config_preflight.py": 2,
#: "tests/test_usage_meter.py": 5}``, eight in total.
EXPECTED_SITES: dict[str, int] = {}

#: Spellings the net must see, and the one it must not. The negative row
#: is paired with the positive ones in a single assertion on purpose: a
#: lone "no hits" check passes just as well when the net has been
#: switched off, which is the failure mode this suite keeps logging.
SPELLINGS: tuple[tuple[str, bool], ...] = (
    ("runner.isolated_filesystem()", True),
    ("with CliRunner().isolated_filesystem() as fs:\n    pass", True),
    ("getattr(runner, 'isolated_filesystem')()", True),
    ("enter = runner.isolated_filesystem", True),
    ("stack.enter_context(runner.isolated_filesystem(temp_dir=d))", True),
    ('"""isolated_filesystem is not a repo."""', False),
)


def _scannable_sources() -> list[Path]:
    """Every module in ``tests/`` but this one, which names what it forbids."""
    return astwalk.test_sources(exclude=Path(__file__))


def _hits(source: str) -> list[ast.AST]:
    sees = astwalk.spells(DEPRECATED_HELPER)
    return [node for node in astwalk.all_nodes(astwalk.parse(source)) if sees(node)]


class TestNoTestUsesTheDeprecatedIsolatedFilesystem:
    def test_no_test_module_even_spells_it(self) -> None:
        astwalk.assert_census(
            sources=_scannable_sources(),
            sees=astwalk.spells(DEPRECATED_HELPER),
            expected=EXPECTED_SITES,
            control="runner.isolated_filesystem()",
            message=(
                "Click 8.5 deprecates CliRunner.isolated_filesystem and 9.0 removes "
                "it. Every test already runs with its cwd set to its own empty "
                "tmp_path by conftest.isolate_kstrl_state, so write the files there "
                "and drop the context manager."
            ),
        )

    def test_the_net_sees_every_spelling_and_leaves_prose_alone(self) -> None:
        """Both directions in one assertion, for the reason SPELLINGS
        carries: the negative row alone cannot tell a narrow net from a
        dead one."""
        found = {source: bool(_hits(source)) for source, _ in SPELLINGS}

        assert found == dict(SPELLINGS), found
