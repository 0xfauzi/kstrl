A keycap shows the key or chord that does the thing it sits beside. It is a hint, not a control: the row or button it sits in is what responds.

**Markup**

```html
<span class="k-keys">
  <span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span>
  <span class="k-key">↵</span>
</span>
```

A chord is keys side by side in `k-keys`, 4px apart, never joined by "+". Modifier order follows the Mac: Control, Option, Shift, Command.

**Legends**: Geist Mono (the full 1.7 build, 889 glyphs) draws every legend except Command, Option and Control, which no shipped font has. Those three are drawn: `k-kg-cmd`, `k-kg-opt`, `k-kg-ctrl`, masks on a 0.72em box, stroked at Geist Mono's own weight-500 stem (0.096em), each with `role="img"` and its spoken name. Command is drawn at 1.4 units instead of 1.6: its four closed loops concentrate ink, so at keycap size an equal stem read heavier than the letters beside it. Measured at 66px per em: Option's stroke is 6px, Command's 5px, Geist's own Shift and Delete 4 to 6px; Command, Option, Shift and Delete all span the cap height to within 1px. Control sits in the upper half, as on the Mac.

**Sizes**: 20px (min width 20, 5px padding, radius 6); `k-key-sm` 18px (4px padding), also used inside a small button; `k-keys-inline` for keys inside a sentence, 18px and 3px apart, centred on the line's lowercase.

**Colour**: `raised` with a 1px `line-strong` edge and a `text-2` legend. Inside a primary button: transparent, edge and legend `you-ink`. On an ink tile (`k-ink` on the tile): transparent, edge and legend `window`.

**Measured**: legend on `raised` 7.26:1 (day) and 7.50:1 (night); on primary 6.09:1 and 8.17:1.

**Do**: right-align keys in the row they trigger, as in the command window. **Don't**: write a key symbol in running text without a keycap (the text fonts do not draw them); invent a shortcut kstrl does not answer to.
