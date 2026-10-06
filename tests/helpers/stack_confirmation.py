"""Confirm a project's ``[stack]`` the way `ks inbox approve` records it (#696 slice 3).

A ``[stack]`` runs nothing until a person confirms its exact text, and since
the flag day (slice 4) a run with no ``[stack]`` refuses. A test that runs a
stack's checks for a reason unrelated to confirmation confirms the stack
first, rather than measuring the refusal instead.

Two ways in. A test that drives a real entry point on a real repository
writes the table into kstrl.toml with :func:`write_stack` and confirms it
with :func:`confirm_stack`. A test that builds its configs in process (a
``FactoryConfig(...)`` handed to ``run_factory``) takes :func:`in_process_stack`,
the value ``stack.confirmed_stack`` returns for a confirmed table, and hands
the SAME object to every config that reads one: ``FactoryConfig``,
``VerifyConfig`` and ``ContractConfig``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from kstrl.inbox import Inbox, InboxConfig
from kstrl.stack import CONFIRMED_IN_INBOX, Stack, file_stack_item, load_stack, stack_toml

#: What a test that is not about verification runs Phase 1 with: three checks
#: that exit 0. It replaces the retired ``test_command="true"``,
#: ``typecheck_command="true"``, ``lint_command="true"`` trio.
TRUE_CHECKS: dict[str, str] = {"tests": "true", "typecheck": "true", "lint": "true"}

#: The instructions every stack built here carries.
TEST_INSTRUCTIONS = "Built by a kstrl test. Run the checks below."


def in_process_stack(
    checks: Mapping[str, str] | None = None,
    *,
    setup: str = "",
    env: tuple[str, ...] = (),
    writable: tuple[str, ...] = (),
    readable: tuple[str, ...] = (),
) -> Stack:
    """A confirmed :class:`Stack`, as ``stack.confirmed_stack`` returns one.

    ``checks`` defaults to :data:`TRUE_CHECKS`. Check names become row names
    ``stack:<name>``. Under the #700 rung a check writes only its worktree
    and ``writable``: a check that appends to a marker file under
    ``tmp_path`` needs that directory in ``writable``.
    """
    stack = Stack(
        instructions=TEST_INSTRUCTIONS,
        setup=setup,
        checks=tuple((checks if checks is not None else TRUE_CHECKS).items()),
        env=env,
        writable=writable,
        readable=readable,
    )
    return replace(stack, unconfirmed="", confirmed_by=CONFIRMED_IN_INBOX)


def write_stack(
    root: Path,
    checks: Mapping[str, str] | None = None,
    *,
    setup: str = "",
    env: tuple[str, ...] = (),
    writable: tuple[str, ...] = (),
    readable: tuple[str, ...] = (),
) -> None:
    """Append a ``[stack]`` with ``checks`` (default :data:`TRUE_CHECKS`) to
    ``root``'s kstrl.toml, creating the file when there is none. Not confirmed:
    call :func:`confirm_stack` after it."""
    stack = in_process_stack(checks, setup=setup, env=env, writable=writable, readable=readable)
    path = root / "kstrl.toml"
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(before + ("\n" if before else "") + stack_toml(stack), encoding="utf-8")


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
