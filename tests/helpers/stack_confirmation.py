"""Confirm a project's ``[stack]`` the way `ks inbox approve` records it (#696 slice 3).

A ``[stack]`` runs nothing until a person confirms its exact text. A test
that runs a stack's checks for a reason unrelated to confirmation confirms
the stack first, rather than measuring the refusal instead.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.inbox import Inbox, InboxConfig
from kstrl.stack import file_stack_item, load_stack


def confirm_stack(root: Path) -> str:
    """File and approve the stack_confirmation item for ``root``'s ``[stack]``.

    Returns the confirmed digest. Call it after kstrl.toml holds the stack.
    """
    stack = load_stack(root)
    assert stack is not None, f"{root}/kstrl.toml has no [stack] to confirm"
    item = file_stack_item(root, stack)
    Inbox(root, InboxConfig.load(root)).approve(
        item.id, actor="test", comment="confirmed by tests/helpers/stack_confirmation.py"
    )
    return stack.digest
