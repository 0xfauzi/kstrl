Proposal: one event, every place it reaches. search-index parking at 21:28:04 is followed from the record kstrl files to each channel that can carry it, with when it arrives and where it lands you.

**Why this shape.** The record sits in the ink tile because it is the one true thing; each channel is a delivery of that record, and answering it anywhere answers it everywhere. The GitHub channel is drawn dashed because it does not apply to this spec. It is shown anyway, so the path is visible.

**Channels, as kstrl has them:**
- **In the app** and **on your desktop** come from the app reading `inbox.jsonl`; they exist while the app is open. The desktop banner is drawn generically, not as any one operating system's.
- **Your hook** is kstrl's `[notify]` section: a shell command run on the machine running kstrl, with `KSTRL_NOTIFY_EVENT`, `_RUN_ID`, `_PROJECT`, `_COMPONENT` and `_DETAIL` in its environment. `on_inbox_item` fires only when `[inbox] notify_action_required` is on (the default), at most once per kind per run, and its detail is kstrl's raw title.
- **The GitHub issue** applies only to specs that came from an issue. `report_outcome` sets `kstrl:<state>` and comments when the run ends, not when the part parks.

**Rules** (the strip at the bottom): asks interrupt once; a repeat adds to its item and stays quiet; notices never interrupt; with kstrl closed only hooks and GitHub reach you.
