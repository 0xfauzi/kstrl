The tile is the surface of every screen: the map's parts, the Grid's agents, the numbers on a page. Tiles sit on `canvas`.

**Markup**

```html
<div class="k-tile k-tile-work k-tile-selected" tabindex="0">
  <span class="k-label">search-query</span>
  ...
</div>
```

**Kinds**
- Default: `window`, radius 18 (`radius-xl`), `shadow-card`, padding 18 by 20.
- `k-tile-ink`: `text` fill, `window` type. One per screen, for the statement that most needs reading (a checker's latest words, the finding that sent a part back). Controls inside it take `focus-on-ink`; labels inside it are `window` at 72%.
- `k-tile-idle`: nothing is happening (the queue, a part nobody is working on). No fill and a 1px dashed `line-input` edge, the system's sign for "not happening", as in the `wait` and `absent` marks. The border takes a pixel, so the padding is 17 by 19 and content lines up with every other tile.

**Sizes**: the default pads 18 by 20. `k-tile-dense` pads 14 by 16 with `radius-lg` (radius follows size), for a grid of six or more or a narrow side column; `k-tile-hero` pads 24 by 28, for the lead tile of a stage or a document; `k-tile-flush` pads nothing, for content that pads itself (a log that scrolls to the edges, a board of columns); `k-tile-cell` pads 6 by 10 with `radius-md`, for one cell of a grid of runs (the Part level's tries by stations: two short lines at 50px). The card shows all five with the same content, each stating its padding and radius as rendered. An idle tile gives its 1px dashed border back, so its content lines up. A page picks one of these and never sets a tile's padding: before this rule the frames used 28 different paddings.

**State edges**: `k-tile-ask` (`you`), `k-tile-alert` (`fail`), `k-tile-work` (`work`), each a 1.5px inset edge. Measured, each is at least 4.45:1 on every ground in both themes (lowest: `fail` on `work-tint` by day). A state edge always travels with its mark and a word inside the tile.

**Selection**: `k-tile-selected` is a 2px `text` ring, 2px off the tile, drawn as an outline so it stacks on any state edge. Keyboard focus on a selectable tile is the same ring, because on the map focus and selection are the same thing, and where you are is never rufous (rufous means something waits for you). It is 15.74:1 on `canvas` by day.

**Changed from the frames**: the frames drew idle tiles with a solid edge although the brand book said dashed, drew state edges at 1px on some tiles and 1.5px on others, and drew selection as a 1.5px ring that a focus ring would have replaced with rufous. The component settles all three. Three more page-drawn copies moved onto it after the audit learned to find them: the Part level's cells (a cell not yet run was a solid ring at 50% opacity, a failed review a 3px bar on the left), the Trust ladder's levels (now dense tiles on the canvas, the level in force the ink tile, the one that cannot happen yet idle) and the Notifications rows (the channel that does not fire for this event idle).

**Don't**: put a tile on `window` (the shadow disappears and the selection ring's gap is wrong); use more than one ink tile on a screen; use an edge without a mark and a word.
