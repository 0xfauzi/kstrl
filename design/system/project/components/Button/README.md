A button runs one action and says what it will do. Use a `<button>` with class `k-button` plus at most one kind and one size.

**Markup**

```html
<button class="k-button k-button-primary">
  <span>Approve and merge</span>
  <span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">↵</span></span>
  <span class="k-spin" aria-hidden="true"></span>
</button>
```

The label is its own `<span>`, so the busy state can hide it and keep the width. Include `k-spin` in every button that can become busy. A leading mark goes before the label as `k-mk sm`.

**Kinds**

| class | use | rest | hover | pressed |
|---|---|---|---|---|
| (none) | any secondary action | `raised`, 1px `line-strong` edge | `selected` | `line` |
| `k-button-primary` | the one action you are about to take; one per surface | `you`, label `you-ink` | `you-hover` | `you-press` |
| `k-button-quiet` | Later, Cancel, anything that dismisses | transparent, `text-2` | `raised`, `text` | `selected`, `text` |
| `k-button-danger` | the one destructive action in a panel (Reject) | as neutral, label `fail` | `selected` | `selected`, edge `fail` |
| `k-button-inverse` | an action on an ink tile | `window` | `raised` | `selected` |

**Sizes**: md (default) is 32px tall, 14px inline padding, 14px/600 label (`body`), radius 10 (`radius-md`), 8px gap, 20px keycaps. `k-button-sm` is 28px, 10px padding, 13px label (`small`), radius 6 (`radius-sm`), 6px gap, 18px keycaps. Keycaps pull 6px (4px small) into the padding, so the inset beside them is 8px (6px small); both anatomies in the card measure it. Both sizes clear the 24px minimum target.

**Block**: `k-button-block` fills its container, puts the label at the start and pushes keys to the end, so every label in a stack of actions lines up (the part page's Tell the engineers, Read the review log, Open decision 1).

**States**
- Hover and pressed change the background only; the label, size and position never move.
- Pressed applies immediately (`transition-duration: 0s`) and releases over `dur-fast` (120ms) on `ease-standard`.
- Focus is a 2px `focus` outline, 2px off the button, following its radius, shown only for keyboard focus (`:focus-visible`). On an ink tile, put `k-ink` on the tile and the ring becomes `focus-on-ink`.
- Disabled uses `aria-disabled="true"`, not `disabled`, so the button stays focusable and can point at its reason with `aria-describedby`. It is `raised` with a `line` edge and `text-3` label, never rufous, and has no hover.
- Busy sets `aria-busy="true"`: the label goes invisible but keeps its width, and the work arc turns in its place (900ms per turn, linear). Clicking a busy button does nothing.

**Keyboard**: native. Tab reaches it, Enter or Space runs it. A keycap in the label documents a global shortcut; it is not what makes the button work.

**Motion and reduced motion**: colour changes only. Under `prefers-reduced-motion: reduce` they complete in 1ms and the busy arc stands still.

**Measured** (both themes, by the component audit): every label is at least 4.5:1 on its state's background. The tightest pair is the danger label on hover, `fail` on `selected`, at 4.65:1 (day). `fail` on `line` is 4.28:1, which is why a pressed danger button stays on `selected`. Focus rings are at least 4.89:1 on every light surface; `focus-on-ink` is 15.99:1 or better on ink.

**Do**: name the action with a verb ("Approve and merge", "Retry search-highlight"). Put the consequence in text beside or under it, never in the label.
**Don't**: put two primary buttons on one surface; make a destructive action primary; truncate a label (write it to fit); approve from a notification card.
