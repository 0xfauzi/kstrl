A segmented control picks exactly one of two to five short options that are all visible at once: the zoom level, Stage or Grid, a setting's value. For switching between two views of one thing, use Tabs; for a single on or off, use Toggle.

**Markup**

```html
<div class="k-seg" role="radiogroup" aria-label="Zoom level">
  <span class="k-seg-thumb" aria-hidden="true"></span>
  <button class="k-seg-item" role="radio" aria-checked="false">Factory</button>
  <button class="k-seg-item" role="radio" aria-checked="true">Spec</button>
  <button class="k-seg-item" role="radio" aria-checked="false" aria-disabled="true">Step</button>
</div>
```

Without a script, the checked option paints its own ground. The reference behaviour (`kSeg` in this card) adds `k-seg-js`, measures the checked option and slides one thumb element between options, which is what makes selection feel continuous. The thumb does not draw until it has been placed, and it is placed again whenever a web font finishes loading: measured before Instrument Sans arrived, every thumb came out 4 to 6px wider than its option.

**Anatomy**: track 32px tall (3px padding, 2px gap, radius 10 `radius-md`); options 26px (13px inline padding, radius 7), 13px labels (`small`) at 500, checked at 600. At 13px Instrument Sans sets 46 characters to the same 284px at 500 and at 600 (measured in the audit's Chromium), so the checked weight moves nothing; at 12px, in the small track, it is 258 against 262, about 1px on a 12-character label. The control is `max-content` wide and never stretches to its container.

**Small**: `k-seg-sm` is a 28px track (2px padding around 24px options, 12px labels, radius 8 around 6) for dense rows: a queue item's options, a setting's values. In both sizes the radii are concentric: track = option + padding (10 = 7 + 3, 8 = 6 + 2).

**Colour**: track `selected`; thumb `thumb`, which is `window` by day and a step lighter than the track at night, because `window` is darker than `selected` at night and a thumb in it reads as a hole. Labels `text-2`, the hovered and checked label `text`, an unavailable option `text-3`.

**Keyboard** (radio group): Tab enters on the checked option and leaves the group. ← → or ↑ ↓ move to the next available option and select it, wrapping at the ends; Home and End jump to the first and last. Unavailable options are skipped. On the map the zoom control also answers ⌘+ and ⌘− from anywhere.

**Motion**: the thumb's position and width change over `dur-base` (180ms) on `ease-standard`. Selection flips at 0ms, so the new label is already bold while the thumb travels: 61% of the way at 45ms, 88% at 90ms. Label colour changes over `dur-fast`. Under reduced motion the thumb jumps.

**Focus**: the 2px `focus` ring sits 1px off the option (the track's 3px padding holds it), on the checked option.

**Measured**: every label is at least 4.5:1 on its ground in both themes (the lowest is `text-3` on `selected`, 4.56:1 by day, for an unavailable option). The focus ring is 5.10:1 or better on the track.

**Don't**: use it for more than five options or for long labels; use it for actions (it chooses, it does not do); put two segmented controls side by side without a label on one of them.
