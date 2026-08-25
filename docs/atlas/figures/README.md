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
| `layer-work.svg` | How a spec becomes a merged pull request: the forward path | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-work.svg |
| `layer-measure.svg` | Who checks what, and who never checks their own work | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-measure.svg |
| `layer-feedback.svg` | What comes back to the agent, and from where | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-feedback.svg |
| `layer-operator.svg` | Where you stand, and what reaches you | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-operator.svg |
| `layer-trust.svg` | How the factory earns and loses the right to act alone | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-trust.svg |
| `layer-learn.svg` | What carries from one run to the next | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-learn.svg |
| `layer-record.svg` | How you see what happened, live or after the fact | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/layer-record.svg |
| `journey-spec-to-merge.svg` | A spec becomes a merged pull request, numbered step by step | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-spec-to-merge.svg |
| `journey-failure-to-retry.svg` | A failure becomes the next attempt | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-failure-to-retry.svg |
| `journey-operator-steers.svg` | You steer the factory | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-operator-steers.svg |
| `journey-trust-earned-lost.svg` | The factory earns and loses autonomy | https://raw.githubusercontent.com/0xfauzi/kstrl/main/docs/atlas/figures/journey-trust-earned-lost.svg |

A layer figure draws only that layer's edges, each labelled with what it
carries, with the layer's question as its title and the components the layer
does not touch dimmed but present. A journey figure numbers its edges along
the path, outlines acting components in amber and measuring ones in steel,
and lists the steps under the map. The map inside every figure is the atlas's
own drawing at the atlas's own scale, so in a README column it is a preview;
open the image to read it at full size. The live, clickable version is
https://0xfauzi.github.io/kstrl/atlas/.
