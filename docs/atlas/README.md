# The system atlas

A generated map of kstrl: what the parts are, how work travels between them,
and how much of each part exists. Open `index.html` in a browser; it fetches
nothing.

Two tiers, kept apart on purpose:

- `atlas.json` is read out of the code by `scripts/atlas/extract_atlas.py`
  with `ast`: every module under `kstrl/`, its public functions and their
  signatures, its classes, its error classes (the refusals), its module-level
  constants, what it imports by name, and the tests under `tests/` that
  import it. Nothing in it is typed by hand.
- `scripts/atlas/logical_model.py` is the hand-authored logical view: the
  components, the regions and containers they sit in, the flows between them
  with the artifact each carries, the invariants, and the mapping from each
  component to the modules and entry point that implement it.

`scripts/atlas/schematic.py` draws the model as one SVG, and every picture of
the system comes from that one generator, so no figure can drift from the
atlas it cites.

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
components are drawn as dashed ghosts in their own region; a page can show
them at full strength by adding the `endstate` class to the SVG root.

## Refresh

    scripts/atlas/refresh.sh

runs the extractor and the renderer when `extract_atlas.py --check` says the
committed atlas no longer matches the tree, and reports if the model names a
module that does not exist or places a card outside its region. Commit
`docs/atlas/` afterwards; the diff between two commits of `atlas.json` is a
record of what changed about the system.

## Citing the atlas from a lesson

Every logical component and every module has a deep link on the page:

    ../atlas/index.html#Pipeline
    ../atlas/index.html#kstrl.verify

Link to it instead of restating it. For a picture, run the generator rather
than drawing one by hand:

    uv run python scripts/atlas/lesson_svg.py --base origin/main --head HEAD \
        --interactive --out <scratch>/changemap.html

    uv run python scripts/atlas/lesson_svg.py --components Sense,Pipeline \
        --interactive --caption "..." --out <scratch>/reach.html

The first marks what a git range changed; the second marks what a plan
reaches without consulting git. `--mode system` draws the plain figure.
`--interactive` adds a panel and an inline script (plain DOM, no libraries):
clicking a component shows what it does, its interface, its build state, the
flows in and out with their artifacts, and the invariants it serves, and
lights its edges. Paste the whole `<figure>` into the lesson.

Edge labels are placed by a collision pass; a label that could not be seated
without overlapping a card or another label is reported on stderr and the
command exits 2. Fix it by moving cards in `logical_model.py`, not by
shrinking text.
