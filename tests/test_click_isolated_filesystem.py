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

Delete this guard when uv.lock carries click>=9; the helper is gone then
and a reintroduction fails as AttributeError.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers import astwalk

#: The deprecated attribute, spelled once so the net and the message
#: cannot drift apart.
DEPRECATED_HELPER = "isolated_filesystem"


class TestNoTestUsesTheDeprecatedIsolatedFilesystem:
    def test_no_test_module_even_spells_it(self) -> None:
        astwalk.assert_census(
            sources=astwalk.test_sources(exclude=Path(__file__)),
            sees=astwalk.spells(DEPRECATED_HELPER),
            expected={},
            control=(
                "runner.isolated_filesystem()",
                "with CliRunner().isolated_filesystem() as fs:\n    pass",
                "getattr(runner, 'isolated_filesystem')()",
                "enter = runner.isolated_filesystem",
                "stack.enter_context(runner.isolated_filesystem(temp_dir=d))",
            ),
            message=(
                "Click 8.5 deprecates CliRunner.isolated_filesystem and 9.0 removes "
                "it. Every test already runs with its cwd set to its own empty "
                "tmp_path by conftest.isolate_kstrl_state, so write the files there "
                "and drop the context manager."
            ),
        )
