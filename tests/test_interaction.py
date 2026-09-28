"""Stage 3 PR A (TUI rewrite): the interaction seam, through run_factory.

The E6 checkpoint request the factory hands its interaction channel
carries the diff excerpt, the usage and the branch, not just the review
summary string. A recording channel stands in for the UI so the request
the factory built is asserted as a whole.
"""

from __future__ import annotations

import ast
import io
from collections import defaultdict
from pathlib import Path

import pytest

from kstrl.interaction import (
    CheckpointContext,
    PromptKind,
    PromptRequest,
    PromptResponse,
)
from kstrl.ui.plain import PlainUI
from tests.helpers.astwalk import (
    Sites,
    assignment_parts,
    blind_spot,
    calls_to,
    dotted,
    label,
    module_name,
    package_sources,
    parse,
    parsed,
    resolved_calls,
    scope_of,
)
from tests.test_unanswered_gate_parks import EXPECTED_UNDECIDED_PROMPT_SITES


class TestCheckpointContext:
    def test_pipeline_builds_full_context(self, tmp_path: Path) -> None:
        """The E6 request carries diff/findings/usage - not just the
        review summary string (verified through a recording channel)."""
        from unittest.mock import patch

        from kstrl.factory import ComponentResult, run_factory
        from tests.test_event_stream import (
            _component,
            _factory_config,
            _make_base_config,
            _make_manifest,
            _setup_project,
            _usage,
        )

        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(
            root,
            create_prs=True,
            pause_before_pr_merge=True,
        )
        result = ComponentResult(
            "comp-a",
            success=True,
            iterations=1,
            usage=_usage(1234),
        )

        requests: list[PromptRequest] = []

        class Recorder:
            def can_prompt(self) -> bool:
                return True

            def request(self, req: PromptRequest) -> PromptResponse:
                requests.append(req)
                return PromptResponse(request_id=req.request_id, choice=0)

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=result,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="+real diff\n",
            ),
            patch("kstrl.pr.is_gh_available", return_value=False),
        ):
            run_factory(
                manifest,
                config,
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                interaction=Recorder(),
            )

        assert len(requests) == 1
        req = requests[0]
        assert req.kind == PromptKind.CHECKPOINT
        assert req.component_id == "comp-a"
        ctx = req.checkpoint
        assert isinstance(ctx, CheckpointContext)
        assert "+real diff" in ctx.diff_excerpt
        assert ctx.usage is not None
        assert ctx.usage.total_tokens == 1234
        assert ctx.branch == "kstrl/factory/comp-a"


#: Every `PromptResponse(...)` kstrl builds, by module and enclosing scope,
#: with the `choice=` it passes. An unanswered producer passes the literal
#: None, never `req.default`, which is Start or Approve at five of the six
#: core prompts (#647). A new producer, or one that passes the default
#: again, changes this table. Re-derived by running the walk.
EXPECTED_RESPONSE_CHOICES = {
    "interaction.py::QueueInteractionChannel.request": ["None", "None", "pending.choice"],
    "interaction.py::UiInteractionChannel.request": ["None", "None", "None", "choice"],
}

PROMPT_RESPONSE_TARGET = frozenset({"kstrl.interaction.PromptResponse"})

#: The module that defines the class calls it by a bare name, which
#: astwalk does not bind (a `class` statement is not a binding), so the
#: walk reports those calls undecided and a rebind of that name in
#: neither half. The census reads that module through its own alias table.
DEFINING_MODULE = "interaction.py"


def _choice_passed(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg == "choice":
            return ast.unparse(keyword.value)
    return ast.unparse(call.args[1]) if len(call.args) > 1 else "<no choice>"


def _local_aliases(tree: ast.Module) -> set[str]:
    """`PromptResponse` and every name or attribute bound to it, to a fixed point."""
    aliases = {"PromptResponse"}
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            targets, value = assignment_parts(node)
            if value is None or dotted(value) not in aliases:
                continue
            for target in targets:
                if target is not None and target not in aliases:
                    aliases.add(target)
                    grew = True
    return aliases


def _alias_calls(tree: ast.Module) -> list[ast.Call]:
    """Calls in the defining module through `PromptResponse` or a name bound to it."""
    aliases = _local_aliases(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted(node.func) in aliases
    ]


def _response_choices() -> tuple[dict[str, list[str]], tuple[str, ...]]:
    """Each producer's `choice=` by module and scope, and every row the
    walk could not decide other than the defining module's own calls."""
    found: dict[str, list[str]] = defaultdict(list)
    combined = Sites()
    read_by_alias: set[str] = set()
    for source in package_sources():
        tree = parsed(source)
        rel = label(source)
        module = module_name(source)
        owner = scope_of(tree, lambdas=True)
        combined += calls_to(tree, PROMPT_RESPONSE_TARGET, where=rel, module=module, owner=owner)
        calls = {
            id(node): node
            for node, _ in resolved_calls(tree, PROMPT_RESPONSE_TARGET, module=module)
        }
        for node in _alias_calls(tree) if rel == DEFINING_MODULE else []:
            calls[id(node)] = node
            read_by_alias.add(f"{rel}::{owner.get(id(node), '<module>')} {dotted(node.func)}")
        for node in calls.values():
            found[f"{rel}::{owner.get(id(node), '<module>')}"].append(_choice_passed(node))
    undecided = tuple(row for row in combined.sorted().undecided if row not in read_by_alias)
    return {site: sorted(choices) for site, choices in found.items()}, undecided


class TestAnUnansweredResponseCarriesNoChoice:
    def test_every_producer_passes_none_or_an_answer(self) -> None:
        choices, undecided = _response_choices()
        assert undecided == EXPECTED_UNDECIDED_PROMPT_SITES, undecided
        assert choices == EXPECTED_RESPONSE_CHOICES, choices

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="dataclasses.replace calls `replace`, not `PromptResponse`",
    )
    def test_a_response_copied_through_dataclasses_replace_is_missed(self) -> None:
        source = (
            "import dataclasses\n"
            "from kstrl.interaction import PromptResponse\n"
            "def f(r: PromptResponse, d: int) -> PromptResponse:\n"
            "    return dataclasses.replace(r, choice=d)\n"
        )
        blind_spot(
            lambda text: resolved_calls(parse(text), PROMPT_RESPONSE_TARGET),
            source,
        )
