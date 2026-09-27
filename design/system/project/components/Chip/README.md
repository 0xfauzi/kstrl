A chip names the agent that is working, beside the thing it is working on: a part's title, an agent tile. No chip when no agent is working.

**Markup**: `<span class="k-chip">engineer</span>`, `<span class="k-chip k-chip-checker">reviewer</span>`, `<span class="k-chip k-chip-you">you</span>`.

**Kinds** follow kstrl's roles. A maker writes something (the engineer writes code, the architect the plan, the distiller facts): `work` on `work-tint`. A checker judges what a maker wrote (the reviewer, security): ink, `window` on `text`. You, at a human checkpoint: `you-ink` on `you`. kstrl's mechanical verifier and contract tester are not agents; they run commands and get no chip. On an ink tile a checker chip turns to `text` on `window`.

**Anatomy**: 18px tall, 7px inline padding, radius 5, 11px/600 agent voice. Width follows the word.

**Measured**: `work` on `work-tint` 4.85:1 (day) and 6.92:1 (night); ink 18.26:1 and 15.99:1; `you-ink` on `you` 6.09:1 and 8.17:1.

**Don't**: use a chip for a state (that is a mark and a word) or for anything but who is working.
