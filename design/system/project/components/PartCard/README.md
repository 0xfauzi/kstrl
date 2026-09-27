A part card is one part on the Spec level, laid out by the plan, which fixes its place for the whole run.

**Top to bottom**: its mark and name; who is working (a chip) or the state in a word when no agent is; its line (Steps, unlabelled, which the mark and word above make safe); one sentence of what is happening, written to fit; its tries as dots, a measured fact, and the ↵ hint when selected.

**States**: working (`k-card-work`, a 1px `work` edge), waiting for you (`k-card-ask`, `you-tint` and a 1px `you` edge), stopped (`k-card-stop`, a 1px `fail` edge), merged and waiting (no edge). Edges are 1px at card size and 1.5px on tiles. Selected, and focused, is the tile's 2px `text` ring.

**Tries**: 7px dots. A try that failed is filled `fail`; the try running now is a `work` ring; others a `line-input` ring. Shape, not colour alone, separates them.

**Anatomy**: 236px on the map, padding 10 by 14, radius 14 (`radius-lg`), `shadow-card`.
