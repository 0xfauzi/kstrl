Proposal: the spec level of the map, where the plan the architect made is drawn as the live dependency graph of parts, each part showing where it is on the line.

**Why this shape.** A run's real structure is the dependency graph in `manifest.json`: a part starts only when every part it needs has merged. So the graph is the layout, and the edges explain what is blocked by what (search-api waits on three parts; search-cli is skipped because search-highlight stopped). The line every part moves along (Build, Check, Your approval, Merge) is drawn inside each card as four segments, so the same picture answers "what is blocked" and "how far along is each part".

**Cards.** A card's top right says who is working when an agent is (`engineer`, `reviewer`, as a role chip) and the state otherwise (merged, your approval, stopped, waiting, skipped). Verify runs no agent, so a part in verify reads "checking" with no role. The selected card carries the `text` selection ring and `↵`.

**Zoom.** Factory › Spec › Part › Step, in the control at the top right of the title row. `⌘+`, `⌘−`, scroll, or `↵` on a selected part moves one level. Graph and Text are two views of this level, as tabs beside it: the graph, and your spec with the architect's decisions.

**Built on** (all written by kstrl today): components, dependencies and status from `manifest.json`; the current phase, try and iteration from `events.jsonl`; blocking counts from `review_result`; spend from `component_usage`; slots from `max_parallel`.
