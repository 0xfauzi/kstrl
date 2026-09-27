Proposal: every event against every channel, with the gaps marked. This is the Every event tab of the Notifications page.

**Why this shape.** Notifications connect every other screen, so the person needs one place that says, for each thing kstrl records, whether it will reach them and how. A cell says what arrives, in words; the dot says whether it interrupts. Where kstrl files an ask but no hook fires, the cell says "not sent" with the absent mark.

**Measured from the code, not assumed:**
- `on_inbox_item` is called only from the pipeline's own `_inbox_add` (`pipeline.py:2315`). The architect's escalation (`decisions.py`), a demotion (`autonomy.py:1086`) and every item `ks serve` files call `Inbox.add` directly, so no hook fires for them.
- `notifiable()` in `inbox.py` includes `DEMOTION_NOTICE`: kstrl means a lowered trust level to interrupt, and the path that files it never calls the hook.
- Each hook fires at most once per condition per run (`observability.py`, `_fired`), and a hook that fails is not retried.
- A stopped part fires `on_first_failure` and also `on_inbox_item` when both are set; they are separate commands so one event is not sent twice through the same command.
- `hook_timeout` defaults to 30 s.
- The "digest" that `inbox.py` mentions is not implemented. The app's inbox is the digest.

**The fix these gaps point to** (not made): call the hook from `Inbox.add`, so that every filing path reaches it.
