Tabs switch between two or three views of the same thing: Graph, Text and Receipt for a spec; Stage and Grid for the step level; Log, Notes and Prompt for an agent. The views share a title and a subject; only the presentation changes. For an exclusive setting, use Segmented.

**Markup**

```html
<div class="k-tabs" role="tablist" aria-label="View">
  <span class="k-tabs-bar" aria-hidden="true"></span>
  <button class="k-tab" role="tab" aria-selected="true" id="t-graph" aria-controls="p-graph">Graph</button>
  <button class="k-tab" role="tab" aria-selected="false" id="t-text" aria-controls="p-text">Text</button>
</div>
<div role="tabpanel" id="p-graph" aria-labelledby="t-graph">…</div>
<div role="tabpanel" id="p-text" aria-labelledby="t-text" hidden>…</div>
```

Without a script, the selected tab draws its own straight 2px underline (a pseudo-element; an inset shadow would follow the tab's radius and curve). The reference behaviour (`kTabs` in this card) adds `k-tabs-js`, then slides one bar between tabs and shows the matching panel. The bar is placed again whenever a web font finishes loading; measured before Instrument Sans arrived, a bar came out up to 13px wider than its tab.

**Anatomy**: tabs 28px tall, 13px labels at 500 in `text-3`, the selected tab at 600 in `text`, underline 2px in `text`. Each tab has 2px inline padding and a 24px minimum width, so even "Log" clears the 24px target; the 12px gap plus that padding gives a 16px visual rhythm.

**Keyboard** (tab list, automatic activation, because showing a view costs nothing): Tab enters on the selected tab and leaves the list; ← → move and select, wrapping; Home and End jump to the ends.

**Motion**: the underline's position and width change over `dur-base` on `ease-standard`. The new label is bold from 0ms while the bar travels. Colour over `dur-fast`. Under reduced motion the bar jumps.

**Measured**: `text-3` labels are 5.04:1 on `window` by day and 5.20:1 at night; the focus ring is at least 4.89:1.

**Vertical** (`k-tabs k-tabs-v`, `aria-orientation="vertical"`): the sections of one long page, one shown at a time, as Settings shows its groups. A column of full-width tabs, 32px tall and 4px apart; the label is 13px at 500 in `text-2` (600 in `text` when selected), because a column of section names is read, not glanced at; a count at the end in 11px `measure` `text-3`, with its unit for screen readers (`<span class="k-sr"> settings</span>`). The bar is 2 by 20 on the left edge, centred on the tab, and slides on `dur-base`. ↑ ↓ move and select, Home and End jump, and ← → do nothing. One panel can serve every tab; move its `aria-labelledby` with the selection.

**Don't**: use tabs for navigation between different things (that is the zoom control and the pages); put more than three in a title row; use them for a setting.
