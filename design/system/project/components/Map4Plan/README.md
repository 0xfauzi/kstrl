Proposal: the spec level as text, your spec in your voice beside what the architect decided about it and the parts it made.

**Why.** The spec is what you write and the only answer path kstrl has for its architect: to change a decision you change the sentence it came from, and the next run plans the spec again. Each decision is pinned to the words it resolved.

**Layout.** This view keeps the shape of a document rather than the tiles of the other levels: your spec on one sheet at reading size, the decisions as cards beside it, and the parts it made at the foot of the sheet. It shares the title row (same meta as the Graph view, Text tab on), the Needs you strip, and a dashed tile where an escalated question would wait.

**Built on**: the spec file; `decisions.json` (question, resolution, reason, alternative, component); the architect's spec issues (their `location` places the pins); components from `manifest.json`. An escalation (a question the architect will not decide) stops the run with exit 2 and files a `spec_escalation` inbox item; it would appear in the dashed Questions tile, in `you`.

**Assumption to check**: that an issue's `location` quotes the spec's words. If it names a section instead, the pin goes on the section heading.
