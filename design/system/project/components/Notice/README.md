A notice is an ask arriving while you work somewhere else. Only asks get one.

**The card**: its kind (`Merge approval`, in `you` for an ask) and age; one line saying what it is; one line saying what waits on it; and two ways out: Review (primary) lands on the item beside its evidence, Later leaves it in Needs you. It never offers Approve: you approve where the evidence is. It never dismisses itself.

**Compact** (`k-notice-compact`): the notice as a page shows it rather than as it arrives, on the Notifications page: head, title and body, no actions, padding 10 by 12, `radius-md`, title 13.5, body 12. The page sets its width. It replaced a copy the page had drawn itself, whose title and body had been shrunk by different amounts (0.79 and 0.92 of the notice's).

**Focus**: arriving, it does not take focus (whatever you were typing keeps it) and is announced politely. F6 moves focus to Review; inside the card, esc means Later and ↵ activates the focused action. This is why the keycaps on its buttons are safe: ↵ also zooms in on the map, and a notice that took ↵ on arrival would hijack it.

**Motion**: enters over `dur-enter` (220ms) on `ease-enter`, rising 8px as it fades in, most of the way there by 73ms; leaves over `dur-exit` (160ms) on `ease-exit`. Under reduced motion it only fades.

**Anatomy**: 372px wide, padding 16 by 18, radius 14 (`radius-lg`), `shadow-panel`. Title 17px/24px at 600 (`input`); body 13px (`small`) `text-2`; footer 12px (`label` at 400) `text-3`.

**What it says must be true.** "ks serve starts no new spec until you answer" is `serve.check_parked_merges`: while a merge is parked, serve admits nothing, because a new item would overwrite the manifest that records the park.
