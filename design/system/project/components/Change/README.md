The only motion in the system: a changed value marked where it changed, ages that tick, and the typing dots. Everything else in a live view changes in place without moving (the layout never moves).

**Fresh**: add `k-fresh` to exactly the value an event changed (a spend total, a state word) and remove it on `animationend`. It starts as a `selected` ground with a 3px spread and fades to nothing over `dur-fresh` (2s), linear, because the fade measures time. For an item with its own ground (a Needs-you item, a row), use `k-arrive`, which fades from `--k-arrive` back to the item's own colour. Both keyframes define only a start, so they end exactly on the element's own style: nothing snaps.

**Ages**: "output 4s ago", ticking once a second. Past 60s it reads "no output for 2m", plainly, because kstrl does not act on silence.

**Typing**: `k-typing`, three 4px `work` dots, each brightening in turn over `dur-typing` (1200ms, 200ms apart), only while an agent's output is growing.

**What kstrl emits** decides what can change: `iteration_started` and `iteration_completed` per iteration, `worker_heartbeat` every 15s, `phase_started` and `phase_completed`, `finding_recorded` per finding, and `component_usage` when a phase ends, so spend jumps rather than ticks.

**Reduced motion**: the mark holds for the same 2 seconds and then goes at once; the dots stand still at three brightnesses.

**Don't**: mark a whole card when one value changed; animate position or size; show an age in colour.
