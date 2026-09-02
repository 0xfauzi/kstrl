"""Positive controls for layer 3: source the helper's own guard MUST flag.

Split out of ``tests/test_settle_shapes.py`` for the same reason its
subject was split out of ``tests/test_settle_discipline.py``: the file
crossed the 800-line ratchet, and layer 3 is a different job from
layers 1 and 2. Those controls are about a test racing its app; these
are about the helper every one of those tests now waits on.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from tests.test_settle_discipline import parsed
from tests.test_settle_predicates import constant_predicates, predicate_calls_under_a_handler


def predicate_handlers(tmp_path: Path, body: str) -> list[str]:
    """What the layer-3a matcher says about one settled() body."""
    path = tmp_path / "settlecheck.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return predicate_calls_under_a_handler(parsed(path))


def constants(tmp_path: Path, body: str) -> list[str]:
    """What the layer-3b matcher says about one snippet."""
    path = tmp_path / "constcheck.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return constant_predicates(parsed(path))


class TestThePredicateIsNeverCaught:
    """Layer 3a. Ten plants passed the helper's own contract tests, and
    three of them were the same shape: the predicate called under a
    handler. The matcher enumerates no exception type, so these are the
    spellings it has to see anyway.
    """

    def test_every_way_of_catching_the_predicate_is_seen(self, tmp_path: Path) -> None:
        for handler in (
            ["except Exception:", "    pass"],
            ["except AssertionError:", "    pass"],
            ["except NoMatches:", "    pass"],
            ["except (KeyError, ValueError):", "    pass"],
            ["except BaseException:", "    raise"],
            ["except Exception:", "    if polls:", "        raise"],
            ["except Exception as exc:", "    if exc.__class__.__name__ != 'X':", "        raise"],
        ):
            # Built rather than interpolated: a multi-line handler
            # dropped into an f-string template carries its own
            # indentation and stops being parseable.
            body = (
                "async def settled(pilot, predicate, *, what, timeout=5.0):\n"
                "    polls = 0\n"
                "    while True:\n"
                "        try:\n"
                "            if predicate():\n"
                "                return\n"
                + textwrap.indent("\n".join(handler), "        ")
                + "\n        await pilot.pause(0.02)\n"
            )
            assert predicate_handlers(tmp_path, body), handler

    def test_a_bare_try_finally_around_the_predicate_is_seen(self, tmp_path: Path) -> None:
        """No handler, but the predicate is still inside a `try`, and a
        `finally` is one edit away from a handler."""
        body = """
        async def settled(pilot, predicate, *, what, timeout=5.0):
            while True:
                try:
                    if predicate():
                        return
                finally:
                    pass
                await pilot.pause(0.02)
        """
        assert predicate_handlers(tmp_path, body)

    def test_the_uncaught_predicate_is_not_reported(self, tmp_path: Path) -> None:
        """The false-positive side. Without this, a matcher that
        returned every predicate call would pass every test above."""
        body = """
        async def settled(pilot, predicate, *, what, timeout=5.0):
            while True:
                if predicate():
                    return
                await pilot.pause(0.02)
        """
        assert predicate_handlers(tmp_path, body) == []

    def test_a_try_elsewhere_in_the_function_is_not_reported(self, tmp_path: Path) -> None:
        """A handler that does not contain the predicate call is not
        this defect, so the matcher must locate the CALL rather than
        notice a `try` anywhere in the function."""
        body = """
        async def settled(pilot, predicate, *, what, timeout=5.0):
            try:
                import time
            except ImportError:
                pass
            while True:
                if predicate():
                    return
                await pilot.pause(0.02)
        """
        assert predicate_handlers(tmp_path, body) == []


class TestAPredicateThatObservesNothing:
    """Layer 3b. The hole demonstrated end to end: replace both
    predicates in `test_escape_closes_the_panel` with `lambda: True`,
    plant the production defect that test names, and the whole suite
    stays green. The await count does not move, so layer 1 is silent,
    and layer 2 sees an enrolled settle.
    """

    def test_a_constant_predicate_is_caught_in_every_spelling(self, tmp_path: Path) -> None:
        for predicate in ("lambda: True", "lambda: 1", "lambda: 'yes'", "lambda: (1, 2)"):
            body = f"""
            from tests.helpers.settle import settled

            async def f(pilot):
                await settled(pilot, {predicate}, what='x')
            """
            assert constants(tmp_path, body), predicate

    def test_a_predicate_that_reads_something_is_left_alone(self, tmp_path: Path) -> None:
        """The false-positive side, and the reason the rule is "reads no
        name" rather than "is a lambda"."""
        for predicate in (
            "lambda: app.screen",
            "lambda: node().query('#x')",
            "lambda: app._safe_mode_reasons is not None",
            "ready",
        ):
            body = f"""
            from tests.helpers.settle import settled

            async def f(pilot, app, node, ready):
                await settled(pilot, {predicate}, what='x')
            """
            assert constants(tmp_path, body) == [], predicate

    def test_every_way_of_reaching_settled_is_seen(self, tmp_path: Path) -> None:
        """The first version of this rule matched a bare `settled` that
        came from a `from` import, and nothing else. Measured, that saw
        one of five ways to reach the same function: `settle.settled`,
        `s.settled`, `settled as wait` and a local `w = settled` all
        walked past it with the hole intact.
        """
        for label_, preamble, call in (
            ("from-import", "from tests.helpers.settle import settled", "settled"),
            ("module alias", "from tests.helpers import settle", "settle.settled"),
            ("package attribute", "import tests.helpers.settle as s", "s.settled"),
            ("renamed on import", "from tests.helpers.settle import settled as wait", "wait"),
        ):
            body = f"""
            {preamble}

            async def f(pilot):
                await {call}(pilot, lambda: True, what='x')
            """
            assert constants(tmp_path, body), label_

    def test_a_local_rebinding_of_settled_is_followed(self, tmp_path: Path) -> None:
        """`w = settled` then `w(...)`, swept to a fixed point so a
        second hop is covered too."""
        body = """
        from tests.helpers.settle import settled

        async def f(pilot):
            first = settled
            second = first
            await second(pilot, lambda: True, what='x')
        """
        assert constants(tmp_path, body)

    def test_an_unrelated_local_settled_is_over_reported_on_purpose(
        self,
        tmp_path: Path,
    ) -> None:
        """The cost of the blanket rule, pinned so it is a decision
        rather than a surprise.

        A module with its own unrelated `settled` is flagged, because
        the alternative is resolving the receiver in order to CLEAR, and
        clearing on a resolution the walk cannot be sure of is the
        direction #324 records guards being holed in. The answer to a
        false positive here is a row in EXPECTED_CONSTANT_PREDICATES
        saying so, which somebody writes in a diff.
        """
        body = """
        async def settled(pilot, predicate, *, what):
            return None

        async def f(pilot):
            await settled(pilot, lambda: True, what='x')
        """
        assert constants(tmp_path, body)
