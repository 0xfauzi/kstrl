# Generated figures

Static pictures of the system, drawn by `scripts/atlas/figures.py` from
`docs/atlas/atlas.json` and the atlas's hand-authored model. Do not edit
them by hand; run `scripts/atlas/refresh.sh` (or the generator) and commit.
`figures.py --check` fails when a committed figure differs from what would be
generated, and the atlas workflow runs it on every pull request.

Each figure is a self-contained SVG: no script, no external reference, system
font stacks, the atlas's dark panel as its own background. Embed one anywhere
with its raw URL:

    https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/<name>.svg

| Figure | What it shows | Raw URL |
|---|---|---|
| `system.svg` | The whole map: every component, every flow in its layer's colour, fill is build state | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/system.svg |
| `loops.svg` | The seven control loops as nested bands, innermost fastest: what acts, what measures, the set point, closed or open | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/loops.svg |
| `layer-work.svg` | How a spec becomes a merged pull request: the forward path (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-work.svg |
| `layer-measure.svg` | Who checks what, and who never checks their own work (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-measure.svg |
| `layer-feedback.svg` | What comes back to the agent, and from where (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-feedback.svg |
| `layer-operator.svg` | Where you stand, and what reaches you (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-operator.svg |
| `layer-trust.svg` | How the factory earns and loses the right to act alone (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-trust.svg |
| `layer-learn.svg` | What carries from one run to the next (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-learn.svg |
| `layer-record.svg` | How you see what happened, live or after the fact (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-record.svg |
| `journey-spec-to-merge.svg` | A spec becomes a merged pull request, numbered step by step (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-spec-to-merge.svg |
| `journey-failure-to-retry.svg` | A failure becomes the next attempt (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-failure-to-retry.svg |
| `journey-operator-steers.svg` | You steer the factory (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-operator-steers.svg |
| `journey-trust-earned-lost.svg` | The factory earns and loses autonomy (compact) | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-trust-earned-lost.svg |

Two kinds of figure. `system.svg` and `loops.svg` are the full topology:
the system map is every component and every flow at the atlas's own scale
(a preview in a README column; open it to read), and the loops figure is
its own drawing.

Every `layer-*.svg` and `journey-*.svg` is compact: it draws only the
components that take part (for a layer, every component with an edge in
the layer; for a journey, every component a step acts with, measures or
traces), laid out as one column per region in the atlas's forward order,
at 13px card names on a panel at most 1398 units wide, so a name renders at
8px or more in a 900px column. A layer figure draws that layer's edges,
each labelled with what it carries, with the layer's question as its
title. A journey figure numbers each edge at its start with the step that
traces it, outlines acting components in amber and measuring ones in
steel, and lists the steps under the map. Each compact figure is verified
as it is generated: every participant present, no edge through a card it
does not connect, no label on a card, a strip, a badge or another label,
nothing under 11px. The live, clickable version of everything is
https://0xfauzi.github.io/kstrl/atlas/.
