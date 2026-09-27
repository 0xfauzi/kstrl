Two readouts of an amount. A meter says how much of a whole is used; a bar says how big one amount is beside others. Neither stands alone: the numbers are always written beside them, so both are hidden from screen readers.

**Meter** (`k-meter`)

```html
<span class="k-meter" aria-hidden="true" style="--k-meter:78%"><i></i></span>
<span>≥$31.10 of $40.00. At the cap the queue pauses.</span>
```

The same bar as a step: 5px, fully round, the used part in `text-3` on a `selected` track, so a meter and a step sequence side by side read as one language. The page sets the share as `--k-meter`. Measured: `text-3` on `selected` is 4.56:1 by day and 4.58:1 at night, above the 3:1 a graphic that carries information needs. The track is only 1.19:1 on `window` by day and 1.24:1 at night, which is why the whole is always said in words.

**Bar** (`k-bar`)

```html
<i class="k-bar" style="--k-bar:80.6%"></i> <span class="v-measure">$33.20</span>
```

One magnitude in a chart: 8px, `text-3`, anchored at its baseline with a 4px rounded data end. Every bar in one chart shares one scale, set against the largest, and its value is written beside it.

**A count toward a threshold** (9 of 15 clean merges toward L3) is neither: it is a step sequence, one step per unit (Steps).

**Changed from the frames**: eight pages drew a meter or a count themselves, at 4, 5, 6 and 8px, with radii of 2, 3 and 4, and fills in `text-3` on some pages and `text-2` on others. The Queue's part line painted merged parts `pass` green, where the rule is colour only where work is live; it is now a step sequence. Receipt drew a ninth, unnamed mark (a 9px green square) for CI; it now uses the `pass` and `work` marks. The Part level drew a tally of iterations above the words that already counted them; the cell now reads as every other cell does, a measurement over a duration.

**Don't**: draw a meter without its numbers; give one chart two scales; colour a meter by state (a state is a mark and a word).
