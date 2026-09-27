Proposal: the queue. What runs next and why in that order, what is running, what finished, and adding a spec in your own words. It opens from the Factory level's Next tile and from ⌘K.

**Why this shape.** The board's columns are kstrl's queue states. kstrl stores an item as a directory under `.kstrl/queue/<state>/`, so the layout is the storage: Next (`queued`, in the order `ks serve` takes them: priority, then oldest), Running (`leased`/`running`), Waiting for you (`awaiting_approval`, when a run ends with a merge parked), and Finished (`done`, `failed`, `poison`). A spec's name and first sentence are your words, so they are set in `human`.

**Adding a spec** is `ks queue add`: the spec text, priority, what happens when it is built (`stop_at_pr`, the default, or `auto_merge`), and attempts (3 by default). The screen says what kstrl will do:
- Priority is set once. kstrl has no command that changes it afterwards, so the order cannot be dragged.
- Merge when green is a request. The autonomy ladder can withhold it, and at L2 every merge still waits for you.
- A failed run is retried with backoff (60 s, doubling, capped at 30 min) until its attempts are used up, then the item is poisoned.
- A poisoned item comes back with `ks queue retry --reset-attempts`.

**From GitHub issues** is off by default. When on, `ks serve` polls every cycle for open issues labelled `kstrl:queued`, takes up to 5 per sync, and refuses an issue edited after it was labelled, or labelled by someone outside `allowed_actors`. Linear is not an intake source; kstrl only posts to Linear.

**Pause** stops admission only. The running spec continues. A daily-budget pause clears itself at midnight; a poisoned-streak pause does not.

**Built on**: `.kstrl/queue/<state>/<id>/meta.json` and `journal.jsonl`; `serve.lock` for whether `ks serve` is running; the intake ledger for the last sync.
