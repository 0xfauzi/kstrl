# The system atlas

A generated map of kstrl: what the parts are, what they are to each other,
and how much of each part exists. Open `index.html` in a browser (or the
published copy at https://0xfauzi.github.io/kstrl/atlas/); it fetches nothing.

## How to read it

One map, seven readings. The layer switch above the map shows one layer at a
time: Work (the forward path from a spec to a merged pull request),
Measurement, Feedback, You (the operator's channels), Trust, Learning and
Record. Each layer answers one question, printed under the switch. "All"
shows every flow in the colour of its layer.

The map opens at Fit: the whole map across the window, scaled down (about
0.6 on a 1440px window). Scroll or pinch to zoom about the pointer, drag to
pan; the Fit and 100% buttons are the two named views and + and - step the
zoom by 1.25 between 0.4 and 2.5. Text is drawn at 11px and never smaller;
at Fit it is small, and zoom brings it back. Double-click a region's name
to frame that region. Print renders at Fit.
The forward path reads left to right across the upper band (intake, plan,
build, decide, measure, ship: decide sits between build and measure because
the pipeline is the hub both sides report to); trust, learning and
observation sit in a lower band under it; the agent's own process and tree
sit below the factory, under build. Every edge is a right-angled path
through the gutters between cards, routed so it passes through no card it
does not connect and no region it neither starts nor ends in, with its
label on its longest run. Above the map a slim strip carries the active
layer's question and the components it touches.

Click a component. The view frames the component and every neighbour at
the largest zoom that shows them all (capped at 1.4), everything else dims,
each connected edge thickens in its layer's colour with its label, and each
neighbour is tagged with the verb that relates it, read from the clicked
component ("measures", "feeds back to", "governs"). The focus panel opens
as a drawer over the map, docked on the side away from the component, and
the neighbourhood is framed into the part of the map the drawer leaves
(Close shuts it and leaves the view; Escape shuts it and returns the view
to where it was before the selection; on a window under 1000px the drawer
sits under the map instead). It leads with the plain word for the component, then draws the
relationship wheel: the component at the centre, one spoke per flow, spokes
grouped by layer, each carrying an arrowhead for direction and its verb.
Hover or tap a spoke (or a neighbour on the map) to read the one sentence
that says what the relationship is; click a spoke to move to that
neighbour. Under the wheel: build state, the invariants the component
serves, and the file it lives in. The URL hash tracks the selection, so
`index.html#EngineerLoop` opens focused.

Journeys step through the map one edge at a time: each step frames its
acting and measuring components and the edge it traces, the acting
components light in amber, the measuring components in steel, and the strip
under the map says what happens. A step nothing measures says so; that
absence is the lesson.

## Three sources, kept apart

- `atlas.json` is read out of the code by `scripts/atlas/extract_atlas.py`
  with `ast`: every module under `kstrl/`, its public surface, and the tests
  that import it. Nothing in it is typed by hand. The page shows none of the
  counts; they exist for the change detector and for build state.
- `scripts/atlas/logical_model.py` is the hand-authored topology: the
  components, the regions and containers they sit in, the flows between them
  with the artifact each carries, the invariants, and the mapping from each
  component to the modules and entry point that implement it.
- `scripts/atlas/relations.py` is the hand-authored meaning: the plain word
  per component, the seven layers and which layer each flow belongs to, one
  sentence per flow, the four journeys, and the verb each spoke prints.

`scripts/atlas/schematic.py` draws the model as one SVG and carries the
focus interaction as one script, and every picture of the system comes from
that one generator, so no figure can drift from the atlas it cites.
`scripts/atlas/route.py` routes every flow orthogonally through the gutters
of the card grid (lanes derived from the card positions, a shortest-first
Dijkstra with costs for bends, shared lanes and shared ports) and seats
each label on its path. `scripts/atlas/theme.py` is the one table of
colours, kstrl's own from `DESIGN.md`.

## Build state is derived

A component's fill is not declared. `logical_model.build_state` looks the
component's `entry` up in the modules named in `implemented_by`, in
`atlas.json`:

- `built`: every named module exists and one of them defines the entry
- `partial`: modules exist but the entry is missing, or some modules are gone
- `planned`: no module exists yet, or the component carries a `tracker`
  (a roadmap item) and its entry has not landed

So a planned component turns built only when its entry appears in the tree,
and a built one turns partial the moment a rename removes its entry. Planned
components are drawn as dashed ghosts; the Today / End state switch shows
them at full strength.

## Refresh and check

    scripts/atlas/refresh.sh

re-extracts `atlas.json` when it no longer matches the tree, runs the layout
check, renders `index.html`, and regenerates the two lesson figures. Commit
`docs/atlas/` afterwards. The workflow in `.github/workflows/atlas.yml` runs
the three checks on every pull request and push to main:

    uv run python scripts/atlas/extract_atlas.py --check
    uv run python scripts/atlas/check_layout.py
    uv run python scripts/atlas/render_html.py --check

`check_layout.py` verifies the hand-placed layout (every card in its band,
no overlaps, nothing set below 11px), the routing (it prints two counts,
edges that pass through a card they do not connect and edges that cross a
region they neither start nor end in, and lists every offender with the
router's reason; both must be zero), the labels (every edge label seated
clear of cards and other labels), the meaning tables (every flow has a
layer and a sentence, every component a plain word, every journey step a
real edge), and the relationship wheel of every component (labels inside
the drawing and off each other). Fix a failure by moving cards in
`logical_model.py` or correcting `relations.py`, not by shrinking text.

## Citing the atlas from a lesson

Every logical component has a deep link on the page:

    ../atlas/index.html#Pipeline

Link to it instead of restating it. For a picture, run the generator rather
than drawing one by hand:

    uv run python scripts/atlas/lesson_svg.py --base origin/main --head HEAD \
        --interactive --out <scratch>/changemap.html

    uv run python scripts/atlas/lesson_svg.py --components Sense,Pipeline \
        --interactive --caption "..." --out <scratch>/reach.html

The first marks what a git range changed; the second marks what a plan
reaches without consulting git. `--mode system` draws the plain figure.
`--interactive` adds the same focus interaction the atlas page has (dim,
thick edges with labels, verb tags, the wheel in a panel under the figure)
and the same pan and zoom (Fit by default, wheel, pinch, drag, selection
framing) as a self-contained fragment. Paste the whole `<figure>` into the lesson.
`system.html` and `r10-reach.html` here are the two figures the control-loop
design cites.
