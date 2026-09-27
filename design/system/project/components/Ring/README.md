A ring shows a part's iterations: one segment per iteration, up to the part's limit (kstrl's `[run] max_iterations`, default 10, `kstrl/config.py`). Finished iterations are `text-3`, the running one `work`, the rest `selected`. The count sits beside it (`8/10`).

**Markup**: drawn by the helper (`ring(done, total, size, stroke, running, label)`): a track, a done arc and a running arc, cut into segments by a mask. With a `label` the ring is `role="img"` and says "Iteration 8 of up to 10"; without one it is hidden from assistive technology and the count beside it says the same.

**Segments**: a 2px gap between iterations whenever each has at least 8px of arc, so the count can be read, not estimated. Below that (30 iterations at 64px is 6.1px each) the arcs are whole. Caps are butt: a zero-length arc paints nothing. The old ring used round caps, and its zero-length first dash painted a `work` dot at 12 o'clock on every ring.

**Sizes** (diameter / stroke): 44/5 in a dense tile, 56/6 in an agent tile, 64/6 on its own.

**Motion**: when an iteration finishes, the done arc grows by one segment and the running arc moves one segment on, both over `dur-ring` (600ms) on `ease-standard`. Under reduced motion they jump.

**Don't**: show a ring for a part with no iterations yet counted; use it for anything but iterations.
