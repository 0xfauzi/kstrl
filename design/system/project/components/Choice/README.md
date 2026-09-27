A choice is one of a few decisions in a list, each with its keys and, under it, its consequence, written before you take it. This is how every kstrl screen asks you to decide: the checkpoint (Approve, Retry, Reject, Later), an inbox item, the feature run's gate.

**Markup**: `<div class="k-choices" role="radiogroup">` of `k-choice` with `role="radio"`, each holding `k-choice-title`, its `k-keys`, and `k-choice-then`. Roving tabindex: only the chosen one is in the tab order.

**In two columns**: `k-choices-grid`, as on an inbox item with four ways out; each rest choice then takes a `line` hairline so the cells read apart.

**Keyboard**: arrows move the choice (wrapping) and Space chooses. One action button, or the command window's action bar, always names the chosen decision and carries its keys, and ↵ presses it. Each decision's own chord acts on it directly. This settles an ambiguity the earlier frames had: with ↵ drawn on Approve and the highlight on Retry, it was not clear what ↵ did. Now ↵ always does what the button says.

**States**: rest is a plain row; hover `raised`; chosen `you-tint` with a 1px `you` edge and the title in `you`, because a decision is yours; a destructive decision (`k-choice-danger`) keeps its title in `fail`, and its button turns `k-button-danger`; focus is the 2px `focus` ring.

**Measured**: chosen title `you` on `you-tint` 5.24:1 (day) and 6.26:1 (night); a chosen destructive title `fail` on `you-tint` 4.77:1 and 5.31:1; consequences `text-2` on `you-tint` 6.75:1 and 6.57:1.

**Consequences are kstrl's**, not paraphrase. At the in-run checkpoint Approve pushes the branch and merges its PR; a merge that `ks serve` parked is decided in the inbox, and `ks inbox approve` starts the run that merges it (`kstrl/cli.py`, `inbox_approve`). Retry uses one of the part's retries and does not pass your words on. Reject fails the part and skips its dependents.

**Don't**: offer a decision without its consequence; truncate a consequence; show the chosen state in anything but `you`.
