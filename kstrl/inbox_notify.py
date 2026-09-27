"""The one place an opened inbox item reaches ``[notify] on_inbox_item`` (#600).

``Inbox.add`` calls :func:`push_opened_item` after it appends a NEW item,
so every filing path fires the hook without doing anything itself. Before
#600 only the pipeline fired it, and the architect's escalation, an
autonomy demotion and everything ``ks serve`` files opened items that
``notifiable()`` selects and pushed nothing.

An occurrence bump of a still-open item does not fire. ``ks serve`` polls
every 60s by default and files the same condition again on each poll, and
a notifier built per call has no memory of the last one, so firing on a
bump would page once a minute for one condition.

A caller that holds a run-scoped ``NotifyHooks`` passes it (only the
pipeline does), which keeps once-per-kind-per-run and that caller's
``capture_output``. Every other path gets hooks built from ``[notify]``
with ``capture_output=True``: the decompose that raises an escalation can
run inside the TUI process, and hook output on inherited fds corrupts the
alternate screen, so a path that does not know who owns the terminal must
not write to it.
"""

from __future__ import annotations

import sys

from kstrl.inbox import Inbox, InboxItem, notifiable
from kstrl.observability import NotifyConfig, NotifyHooks


def _warn_stderr(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def push_opened_item(inbox: Inbox, item: InboxItem, notify: NotifyHooks | None) -> None:
    """Fire ``on_inbox_item`` for a just-opened item. Never raises.

    ``except Exception`` because the item is already on disk: anything
    that escaped would reach the caller's own guard, which reports a
    write that succeeded as failed, and ``ks serve`` would lose the item
    id. ``NotifyConfig.load`` raises ``ValueError`` on a non-numeric
    ``KSTRL_NOTIFY_HOOK_TIMEOUT``, and it is not the only thing it raises.
    """
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
