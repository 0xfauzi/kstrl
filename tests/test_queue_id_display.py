"""Every place ``kstrl/`` cuts a queue item id is pinned (#706).

Queue item ids are ``q-YYYYMMDD-HHMMSS.ffffff-<nonce>``. Their first
twelve characters are ``q-YYYYMMDD-H``, so 25 sites that printed
``item_id[:12]`` showed one id for every item minted in the same ten-hour
window, and ``ks queue`` refused that id as ambiguous. The fix prints the
full id, or :func:`kstrl.workqueue.short_item_id` where a one-line surface
has no room, and :meth:`kstrl.workqueue.Queue.get` accepts both.

THE NET. Every expression that reads a name or attribute called
``item_id`` and then cuts it: a slice, a format spec, or a ``split``-family
call. Keyed by module and expression, so a new cut is a new row and a
moved one is not. The guard FLAGS, so an over-match costs a reader one
row; each pinned row below says why it is not a queue id shown to an
operator.

WHAT IT DOES NOT SEE: an id copied into a name that is not ``item_id``
first, as the TUI's ``ActiveRow.label`` is. That is disclosed below as a
strict xfail rather than left unstated, and the TUI's own end-to-end test
in ``tests/test_tui_433_inc5.py`` covers that row.

The last test drives ``ks serve --once`` and types the ids it printed and
filed back into ``ks queue show``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from kstrl.cli import cli
from kstrl.inbox import Inbox, InboxConfig
from kstrl.serve import RunOutcome, RunSpend
from kstrl.workqueue import ItemState, Queue, QueueConfig, short_item_id
from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    label,
    package_sources,
    parse,
)

#: The ``str`` methods that cut a string into pieces.
_SPLITTERS = frozenset({"split", "rsplit", "partition", "rpartition"})


def _reads_an_item_id(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "item_id") or (
        isinstance(node, ast.Name) and node.id == "item_id"
    )


def cuts_an_item_id(node: ast.AST) -> bool:
    """Is this node a slice, a format spec or a split of an ``item_id``?"""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        return _reads_an_item_id(node.value)
    if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
        return _reads_an_item_id(node.value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SPLITTERS
    ):
        return _reads_an_item_id(node.func.value)
    return False


def _row(source_file: Path, node: ast.AST) -> str:
    return f"{label(source_file)}: {ast.unparse(node)}"


#: Every cut of an ``item_id`` in ``kstrl/``, and why each is allowed.
EXPECTED_ITEM_ID_CUTS: dict[str, int] = {
    # The project name serve derives for an item that named none: a
    # branch-safe word, not an id an operator types back.
    "serve.py: item.item_id.split('-')": 1,
    # Inbox approval ids (uuid4 hex), which `ks inbox` resolves by prefix.
    "waivers.py: match.item_id[:8]": 1,
    "waivers.py: other.item_id[:8]": 1,
    # The short form itself, which Queue.get accepts.
    "workqueue.py: item_id.rsplit('-', 1)": 1,
}


def test_every_cut_of_an_item_id_is_pinned() -> None:
    assert_census(
        sources=package_sources(),
        sees=cuts_an_item_id,
        expected=EXPECTED_ITEM_ID_CUTS,
        key=_row,
        control=(
            'x = f"{item.item_id[:12]}"\n',
            'x = f"{item.item_id:.12}"\n',
            'x = item_id.rsplit("-", 1)\n',
        ),
        message=(
            "A queue item id is cut somewhere new. Print the full id, or "
            "kstrl.workqueue.short_item_id where a one-line surface has no room; "
            "both are ids every `ks queue` command accepts (#706). If this cut is "
            "not a queue id shown to an operator, add the row with the reason."
        ),
    )


def _cut_anywhere(source: str) -> bool:
    return any(cuts_an_item_id(node) for node in all_nodes(parse(source)))


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_an_id_carried_under_another_name_is_a_disclosed_miss() -> None:
    blind_spot(_cut_anywhere, "label = item.item_id\nshown = label[:12]\n")


def _invoke(args: list[str], root: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--root", str(root), "--no-color"])


@pytest.mark.usefixtures("no_open_prs")
def test_a_poisoned_item_is_named_by_ids_queue_show_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ks serve`'s line and the inbox title it files both name the poisoned item
    in a form `ks queue show` resolves to it, with a second item minted in the same
    microsecond beside it."""
    monkeypatch.setattr("kstrl.serve.read_run_spend", lambda root, run_id: RunSpend())
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n\nDo the thing.\n", encoding="utf-8")
    moment = datetime.now(UTC)
    with monkeypatch.context() as clock:
        clock.setattr("kstrl.workqueue._utc_now", lambda: moment)
        for _ in range(2):
            assert _invoke(["queue", "add", str(spec)], tmp_path).exit_code == 0
    with patch(
        "kstrl.serve.subprocess_factory_runner",
        return_value=RunOutcome(returncode=1),
    ):
        result = _invoke(["serve", "--once"], tmp_path)
    assert result.exit_code == 1, result.output
    (poisoned,) = Queue(tmp_path, QueueConfig()).items((ItemState.POISON,))
    assert f"{poisoned.item_id} poisoned:" in result.output, result.output
    titles = [entry.title for entry in Inbox(tmp_path, InboxConfig.load(tmp_path)).items()]
    named = [title.split()[2].rstrip(":") for title in titles if title.startswith("Queue item ")]
    assert named, titles
    # The title carries the short form: the full id cut the title's reason in
    # the TUI's needs-you row at 80 columns.
    assert f"Queue item {short_item_id(poisoned.item_id)} poisoned" in titles, titles
    for shown in named:
        show = _invoke(["queue", "show", shown], tmp_path)
        assert show.exit_code == 0, show.output
        assert poisoned.item_id in show.output, show.output
