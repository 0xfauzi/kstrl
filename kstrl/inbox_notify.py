"""The one place an opened inbox item reaches ``[notify] on_inbox_item`` (#600).

``Inbox.add`` calls :func:`push_opened_item` after it appends a NEW item,
so every filing path fires the hook without doing anything itself. Before
#600 only the pipeline fired it, and the architect's escalation, an
autonomy demotion and everything ``ks serve`` files opened items that
``notifiable()`` selects and pushed nothing.

An occurrence bump of a still-open item does not fire. ``ks serve`` polls
every 60s by default and files the same condition again on each poll, and
a notifier built per call has no memory of the last one, so firing on a
bump would page once a minute for one condition. A repeat filed while
the row is still snoozed opens a fresh row (the operator's earlier
decision does not swallow new information) but does not push either:
the operator already deferred this one.

A caller that holds a run-scoped ``NotifyHooks`` passes it (only the
pipeline does), which keeps once-per-kind-per-run and that caller's
``capture_output``. This is a distinction between CALLERS, not between
running inside or outside a factory run: an autonomy demotion or a
health breach raised while a run is active does not hold that run's
hooks and still falls through here. Every other path gets hooks built
from ``[notify]`` with ``capture_output=True``: the decompose that
raises an escalation can run inside the TUI process, and hook output on
inherited fds corrupts the alternate screen, so a path that does not
know who owns the terminal must not write to it.
"""

from __future__ import annotations

import sys

from kstrl.inbox import Inbox, InboxItem, notifiable
from kstrl.observability import NotifyConfig, NotifyHooks


def _warn_stderr(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def push_opened_item(
    inbox: Inbox,
    item: InboxItem,
    notify: NotifyHooks | None,
    existing: InboxItem | None = None,
) -> None:
    """Fire ``on_inbox_item`` for a just-opened item. Never raises.

    ``existing`` is the row ``add`` found under the same dedupe key
    before deciding to open ``item`` fresh rather than bump it (``None``
    when there was none). A repeat filed while ``existing`` is still
    snoozed opens a new row - the decided generation must not swallow
    new information - but must not page again: the operator already
    deferred this, and ``existing.snooze_active`` is False once the
    snooze has lapsed, so an expired one still pages.

    ``except Exception`` because the item is already on disk: anything
    that escaped would reach the caller's own guard, which reports a
    write that succeeded as failed, and ``ks serve`` would lose the item
    id. ``NotifyConfig.load`` raises ``ValueError`` on a non-numeric
    ``KSTRL_NOTIFY_HOOK_TIMEOUT``, and it is not the only thing it raises.
    """
    if existing is not None and existing.snooze_active:
        return
    if not inbox.config.notify_action_required or not notifiable([item]):
        return
    try:
        hooks = notify
        if hooks is None:
            hooks = NotifyHooks(
                NotifyConfig.load(inbox.root_dir),
                run_id=item.run_id,
                warn=_warn_stderr,
                capture_output=True,
            )
        hooks.fire_inbox_item(str(item.kind), item.title, component_id=item.component)
    except Exception as exc:  # noqa: BLE001 - the item is already on disk
        _warn_stderr(f"inbox item {item.id[:8]} recorded; its notify hook did not run: {exc}")
